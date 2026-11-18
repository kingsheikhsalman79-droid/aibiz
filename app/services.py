"""Business logic shared by the dashboard (/api) and the public client API (/v1)."""
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import ads_engine, llm, mailer
from .config import settings
from .models import (AdCampaign, AdEvent, ChatMessage, Conversation, EmailLog, Product, Subscription, User, utcnow)
from .security import decrypt


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- subscriptions ----------
def plan_days(months: int) -> int:
    return 365 * (months // 12) + 30 * (months % 12)


def active_subscription(db: Session, user_id: int):
    return db.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status == "active", Subscription.expires_at > utcnow())
        .order_by(Subscription.expires_at.desc())
    )


def activate_plan(db: Session, user_id: int, plan, months=None):
    months = months or plan.months
    days = plan_days(months)
    quota = plan.requests_per_month * months
    cur = active_subscription(db, user_id)
    if cur:  # renewing early extends the current period instead of wasting paid time
        cur.expires_at = cur.expires_at + timedelta(days=days)
        cur.request_limit += quota
        cur.plan_name = plan.name
        sub = cur
    else:
        now = utcnow()
        sub = Subscription(
            user_id=user_id, plan_name=plan.name, starts_at=now, expires_at=now + timedelta(days=days),
            status="active", request_limit=quota, requests_used=0,
        )
        db.add(sub)
    db.commit()
    return sub


def sub_out(sub):
    if not sub:
        return None
    seconds = max(0, int((sub.expires_at - utcnow()).total_seconds()))
    return {
        "plan": sub.plan_name,
        "starts_at": sub.starts_at.isoformat(),
        "expires_at": sub.expires_at.isoformat(),
        "days_left": -(-seconds // 86400),
        "requests_used": sub.requests_used,
        "request_limit": sub.request_limit,
    }


def user_out(u: User):
    return {
        "id": u.id, "email": u.email, "name": u.name, "role": u.role, "business_name": u.business_name,
        "business_info": u.business_info, "language": u.language, "gmail_address": u.gmail_address,
        "gmail_connected": bool(u.gmail_secret),
    }


def product_out(p: Product):
    return {
        "id": p.id, "name": p.name, "sku": p.sku, "description": p.description, "details": p.details,
        "price": p.price, "currency": p.currency, "stock": p.stock, "active": p.active,
    }


# ---------- chat ----------
def _history(db: Session, conv_id: str, limit=12):
    rows = db.scalars(
        select(ChatMessage).where(ChatMessage.conversation_id == conv_id).order_by(ChatMessage.id.desc()).limit(limit)
    ).all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows)]


def _generate(system, history, products, message):
    """Return (reply, mode). Falls back to the demo answer if the AI provider fails."""
    if settings.llm_provider in ("anthropic", "openai") and settings.llm_api_key:
        try:
            return llm.complete(system, history + [{"role": "user", "content": message}], settings), settings.llm_provider
        except llm.LLMError:
            return llm.demo_reply(products, message), "fallback"
    return llm.demo_reply(products, message), "demo"


def chat_reply(db: Session, user: User, message: str, conversation_id=None, customer_ref=None, channel="api"):
    conv = None
    if conversation_id:
        conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user.id))
    if not conv:
        conv = Conversation(id=uuid.uuid4().hex, user_id=user.id, customer_ref=customer_ref, channel=channel)
        db.add(conv)
        db.flush()
    products = [product_out(p) for p in db.scalars(select(Product).where(Product.user_id == user.id, Product.active.is_(True))).all()]
    system = llm.build_system_prompt(user.business_name, user.business_info, products, message)
    reply, mode = _generate(system, _history(db, conv.id), products, message)
    db.add(ChatMessage(conversation_id=conv.id, role="user", content=message))
    db.add(ChatMessage(conversation_id=conv.id, role="assistant", content=reply))
    db.commit()
    return {"conversation_id": conv.id, "reply": reply, "mode": mode}


# ---------- email ----------
def gmail_credentials(user: User):
    if not (user.gmail_address and user.gmail_secret):
        return None
    pw = decrypt(user.gmail_secret, settings.secret_key)
    return (user.gmail_address, pw) if pw else None


def _log_email(db, user, direction, who, subject, status):
    db.add(EmailLog(user_id=user.id, direction=direction, counterpart=(who or "")[:254], subject=(subject or "")[:300], status=status))
    db.commit()


def send_user_email(db: Session, user: User, to: str, subject: str, body: str, in_reply_to=None):
    creds = gmail_credentials(user)
    if not creds:
        raise ServiceError(400, "Gmail is not connected. Connect it first (Gmail tab) with an App Password.")
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    sent = db.scalar(
        select(func.count(EmailLog.id)).where(
            EmailLog.user_id == user.id, EmailLog.direction == "out", EmailLog.status == "sent", EmailLog.created_at >= today
        )
    )
    if sent >= settings.email_daily_limit:
        raise ServiceError(429, f"Daily email limit reached ({settings.email_daily_limit}). Try again tomorrow.")
    try:
        mailer.send_email(creds[0], creds[1], to, subject, body, in_reply_to)
    except mailer.MailError as exc:
        _log_email(db, user, "out", to, subject, "failed")
        raise ServiceError(502, str(exc)) from None
    _log_email(db, user, "out", to, subject, "sent")


def email_reply_text(db: Session, user: User, sender: str, subject: str, body: str):
    products = [product_out(p) for p in db.scalars(select(Product).where(Product.user_id == user.id, Product.active.is_(True))).all()]
    question = f"Subject: {subject}\n\n{body}"
    system = llm.build_system_prompt(user.business_name, user.business_info, products, question, channel="email")
    prompt = f"Write the reply to this customer email (from {sender or 'a customer'}):\n\n{question}"
    reply, mode = _generate(system, [], products, prompt if settings.llm_provider in ("anthropic", "openai") else question)
    return reply, mode


def process_inbox(db: Session, user: User, limit=5, send=True):
    creds = gmail_credentials(user)
    if not creds:
        raise ServiceError(400, "Gmail is not connected. Connect it first (Gmail tab).")
    try:
        mails = mailer.fetch_unseen(creds[0], creds[1], limit)
    except mailer.MailError as exc:
        raise ServiceError(502, str(exc)) from None
    results, done = [], []
    for m in mails:
        item = {"from": m["from_addr"], "subject": m["subject"]}
        if mailer.should_skip(m, creds[0]):
            item["status"] = "skipped (automatic or no-reply sender)"
            results.append(item)
            continue
        reply, mode = email_reply_text(db, user, m["from_addr"], m["subject"], m["body"])
        item["reply"] = reply
        if send:
            subject = m["subject"] if m["subject"].lower().startswith("re:") else f"Re: {m['subject']}"
            try:
                send_user_email(db, user, m["from_addr"], subject or "Re: your message", reply, m["message_id"] or None)
                item["status"] = "replied"
                done.append(m["uid"].encode())
            except ServiceError as exc:
                item["status"] = f"not sent: {exc.message}"
        else:
            item["status"] = "draft only (not sent)"
        results.append(item)
    if done:
        try:
            mailer.mark_seen(creds[0], creds[1], done)
        except mailer.MailError:
            pass
    return results


# ---------- ads ----------
def _ad_suggestion(user: User, campaign: AdCampaign, problem: dict) -> str:
    if settings.llm_provider not in ("anthropic", "openai") or not settings.llm_api_key:
        return ""
    system = (
        "You are a performance-marketing copywriter. Write 3 short ad headlines and 2 descriptions that follow "
        "typical ad-platform policies: honest claims, no guarantees, no misleading urgency."
    )
    prompt = (
        f"Business: {user.business_name}\nAbout: {(user.business_info or '')[:800]}\nCampaign: {campaign.name} on {campaign.platform}\n"
        f"Problem: {problem['diagnosis']}\nWrite improved ad copy."
    )
    try:
        return llm.complete(system, [{"role": "user", "content": prompt}], settings)[:1500]
    except llm.LLMError:
        return ""


def ads_ingest(db: Session, user: User, campaign: AdCampaign, metrics: dict):
    camp = {"daily_budget": campaign.daily_budget, "max_daily_budget": campaign.max_daily_budget, "target_cpa": campaign.target_cpa}
    problems = ads_engine.analyze(camp, metrics)
    adapter = ads_engine.NoopAdapter()
    out = []
    for p in problems:
        status = ads_engine.execute(adapter, p)
        suggestion = _ad_suggestion(user, campaign, p) if p["action"] in ("rewrite_and_resubmit", "refresh_creative") else ""
        db.add(AdEvent(
            user_id=user.id, campaign_id=campaign.id, code=p["code"], severity=p["severity"], diagnosis=p["diagnosis"],
            action=p["action"], action_status=status, suggestion=suggestion,
        ))
        out.append({**p, "status": status, "suggestion": suggestion})
    campaign.last_metrics = metrics
    campaign.status = str(metrics.get("status", "active"))[:20]
    db.commit()
    return {"healthy": not problems, "problems": out}
