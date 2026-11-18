"""Dashboard API used by the website (/api). Login-token authentication."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import mailer, services
from .config import settings
from .database import get_db
from .deps import current_user, dash_ai, dash_chat, login_limiter
from .models import ApiKey, Payment, Plan, UsageLog, User, utcnow
from .routes_account import EMAIL_PATTERN, build_router
from .security import encrypt, hash_password, make_token, new_api_key, verify_password

router = APIRouter(prefix="/api", include_in_schema=False)
router.include_router(build_router(current_user, dash_ai, dash_chat, "dashboard"), include_in_schema=False)


class RegisterIn(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field("", max_length=120)
    business_name: str = Field("", max_length=160)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class MeIn(BaseModel):
    name: str | None = Field(None, max_length=120)
    business_name: str | None = Field(None, max_length=160)
    business_info: str | None = Field(None, max_length=8000)
    language: str | None = Field(None, max_length=8)


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class CheckoutIn(BaseModel):
    plan_id: int
    method: str = Field(min_length=2, max_length=40)
    reference: str = Field(min_length=3, max_length=120)


class GmailIn(BaseModel):
    address: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    app_password: str = Field(min_length=8, max_length=64)


def plan_out(p: Plan):
    return {
        "id": p.id, "name": p.name, "months": p.months, "price_usd": p.price_cents / 100, "description": p.description,
        "requests_per_month": p.requests_per_month, "is_active": p.is_active,
    }


def _session(user: User):
    token = make_token(user.id, settings.secret_key, settings.token_ttl_hours * 3600)
    return {"token": token, "user": services.user_out(user)}


@router.get("/config")
def config():
    return {"app_name": settings.app_name, "ai_mode": settings.llm_provider}


# ----- auth -----
@router.post("/auth/register")
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    if not login_limiter.allow(f"reg:{ip}"):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    email = body.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "This email is already registered. Please sign in.")
    if settings.admin_email:
        is_admin = email == settings.admin_email
    else:  # no ADMIN_EMAIL configured: the very first account becomes the admin (set ADMIN_EMAIL in production!)
        is_admin = db.scalar(select(func.count(User.id))) == 0
    user = User(
        email=email, password_hash=hash_password(body.password), name=body.name.strip(),
        business_name=body.business_name.strip(), role="admin" if is_admin else "client",
    )
    db.add(user)
    db.commit()
    return _session(user)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "?"
    if not login_limiter.allow(f"login:{ip}:{email}"):
        raise HTTPException(429, "Too many attempts. Please wait a few minutes.")
    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(body.password, user.password_hash if user else "scrypt$16384$8$1$00$00")
    if not user or not ok:
        raise HTTPException(401, "Wrong email or password.")
    if not user.is_active:
        raise HTTPException(403, "This account is disabled.")
    return _session(user)


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"user": services.user_out(user), "subscription": services.sub_out(services.active_subscription(db, user.id))}


@router.patch("/me")
def update_me(body: MeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(user, k, v.strip() if k != "business_info" else v)
    db.commit()
    return {"user": services.user_out(user)}


# ----- plans & billing -----
@router.get("/plans")
def plans(db: Session = Depends(get_db)):
    rows = db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.months)).all()
    return [plan_out(p) for p in rows]


@router.get("/billing")
def billing(user: User = Depends(current_user), db: Session = Depends(get_db)):
    pays = db.scalars(select(Payment).where(Payment.user_id == user.id).order_by(Payment.id.desc()).limit(50)).all()
    names = {p.id: p.name for p in db.scalars(select(Plan)).all()}
    return {
        "subscription": services.sub_out(services.active_subscription(db, user.id)),
        "instructions": settings.payment_instructions,
        "payments": [
            {"id": p.id, "plan": names.get(p.plan_id, "?"), "amount_usd": p.amount_cents / 100, "method": p.method,
             "reference": p.reference, "status": p.status, "created_at": p.created_at.isoformat()}
            for p in pays
        ],
    }


@router.post("/billing/checkout")
def checkout(body: CheckoutIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    plan = db.get(Plan, body.plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(404, "Plan not found.")
    pending = db.scalar(select(func.count(Payment.id)).where(Payment.user_id == user.id, Payment.status == "pending"))
    if pending >= 5:
        raise HTTPException(429, "You already have several payments waiting for review.")
    pay = Payment(user_id=user.id, plan_id=plan.id, amount_cents=plan.price_cents, method=body.method.strip(), reference=body.reference.strip())
    db.add(pay)
    db.commit()
    return {"ok": True, "message": "Payment submitted. Your plan starts as soon as the owner confirms it."}


# ----- API keys -----
@router.get("/keys")
def list_keys(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.id.desc())).all()
    return [
        {"id": k.id, "name": k.name, "prefix": k.prefix, "revoked": k.revoked,
         "created_at": k.created_at.isoformat(), "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
        for k in rows
    ]


@router.post("/keys")
def create_key(body: KeyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    count = db.scalar(select(func.count(ApiKey.id)).where(ApiKey.user_id == user.id, ApiKey.revoked.is_(False)))
    if count >= 10:
        raise HTTPException(400, "You can have at most 10 active keys. Revoke one first.")
    raw, prefix, digest = new_api_key()
    k = ApiKey(user_id=user.id, name=body.name.strip(), prefix=prefix, key_hash=digest)
    db.add(k)
    db.commit()
    return {"id": k.id, "name": k.name, "prefix": prefix, "key": raw}  # the full key is shown only now


@router.delete("/keys/{kid}")
def revoke_key(kid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    k = db.scalar(select(ApiKey).where(ApiKey.id == kid, ApiKey.user_id == user.id))
    if not k:
        raise HTTPException(404, "Key not found.")
    k.revoked = True
    db.commit()
    return {"ok": True}


# ----- Gmail connection -----
@router.get("/gmail")
def gmail_status(user: User = Depends(current_user)):
    return {"address": user.gmail_address, "connected": bool(user.gmail_secret)}


@router.put("/gmail")
def gmail_connect(body: GmailIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    pw = body.app_password.replace(" ", "")
    try:
        mailer.check_login(body.address, pw)
    except mailer.MailError as exc:
        raise HTTPException(400, str(exc)) from None
    user.gmail_address = body.address.strip().lower()
    user.gmail_secret = encrypt(pw, settings.secret_key)
    db.commit()
    return {"address": user.gmail_address, "connected": True}


@router.delete("/gmail")
def gmail_disconnect(user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.gmail_address = None
    user.gmail_secret = None
    db.commit()
    return {"ok": True}


# ----- usage -----
@router.get("/usage")
def usage(user: User = Depends(current_user), db: Session = Depends(get_db)):
    since = utcnow() - timedelta(days=30)
    n = db.scalar(select(func.count(UsageLog.id)).where(UsageLog.user_id == user.id, UsageLog.created_at >= since))
    return {"last30": n or 0}
