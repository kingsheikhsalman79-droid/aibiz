"""Endpoints that exist twice: under /api (dashboard, login token) and /v1 (client API key)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services
from .database import get_db
from .models import AdCampaign, AdEvent, ChatMessage, Conversation, Order, Product, User

EMAIL_PATTERN = r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$"


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(None, max_length=32)
    customer_id: str | None = Field(None, max_length=100)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sku: str | None = Field(None, max_length=64)
    description: str = Field("", max_length=2000)
    details: str = Field("", max_length=8000)
    price: float = Field(0, ge=0, le=1_000_000_000)
    currency: str = Field("USD", min_length=1, max_length=8)
    stock: int = Field(0, ge=0, le=1_000_000_000)
    active: bool = True


class OrderIn(BaseModel):
    product_id: int
    quantity: int = Field(1, ge=1, le=10000)
    customer_name: str = Field(min_length=1, max_length=160)
    customer_email: str = Field(pattern=EMAIL_PATTERN, max_length=254)


class OrderStatusIn(BaseModel):
    status: str = Field(pattern="^(new|confirmed|shipped|cancelled)$")


class EmailSendIn(BaseModel):
    to: str = Field(pattern=EMAIL_PATTERN, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class AutoReplyIn(BaseModel):
    from_email: str | None = Field(None, pattern=EMAIL_PATTERN, max_length=254)
    subject: str = Field("", max_length=300)
    body: str = Field(min_length=1, max_length=10000)
    send: bool = False


class InboxIn(BaseModel):
    limit: int = Field(5, ge=1, le=20)
    send: bool = True


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    platform: str = Field("", max_length=40)
    daily_budget: float = Field(0, ge=0)
    max_daily_budget: float = Field(0, ge=0)
    target_cpa: float = Field(0, ge=0)


class MetricsIn(BaseModel):
    spend: float = Field(0, ge=0)
    impressions: float = Field(0, ge=0)
    clicks: float = Field(0, ge=0)
    conversions: float = Field(0, ge=0)
    status: str = Field("active", max_length=30)
    disapproval_reason: str = Field("", max_length=500)
    days_running: int = Field(0, ge=0)


def _own_product(db: Session, user: User, pid: int) -> Product:
    p = db.scalar(select(Product).where(Product.id == pid, Product.user_id == user.id))
    if not p:
        raise HTTPException(404, "Product not found.")
    return p


def _campaign_out(c: AdCampaign):
    return {
        "id": c.id, "name": c.name, "platform": c.platform, "status": c.status, "daily_budget": c.daily_budget,
        "max_daily_budget": c.max_daily_budget, "target_cpa": c.target_cpa, "last_metrics": c.last_metrics,
    }


def _order_out(o: Order):
    return {
        "id": o.id, "product_id": o.product_id, "product_name": o.product_name, "customer_name": o.customer_name,
        "customer_email": o.customer_email, "quantity": o.quantity, "total": o.total, "currency": o.currency,
        "status": o.status, "created_at": o.created_at.isoformat(),
    }


def build_router(user_dep, ai_dep, chat_dep, channel: str) -> APIRouter:
    r = APIRouter()

    # ----- chat -----
    @r.post("/chat", tags=["Chat"], summary="Customer chat: the AI answers using your products and business info")
    def chat(body: ChatIn, user: User = Depends(chat_dep), db: Session = Depends(get_db)):
        return services.chat_reply(db, user, body.message, body.conversation_id, body.customer_id, channel)

    @r.get("/conversations/{cid}", tags=["Chat"], summary="Read a conversation")
    def conversation(cid: str, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        conv = db.scalar(select(Conversation).where(Conversation.id == cid, Conversation.user_id == user.id))
        if not conv:
            raise HTTPException(404, "Conversation not found.")
        rows = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == cid).order_by(ChatMessage.id)).all()
        return {"id": cid, "messages": [{"role": m.role, "content": m.content, "at": m.created_at.isoformat()} for m in rows]}

    # ----- products -----
    @r.get("/products", tags=["Products"], summary="List products")
    def list_products(user: User = Depends(user_dep), db: Session = Depends(get_db)):
        rows = db.scalars(select(Product).where(Product.user_id == user.id).order_by(Product.id.desc())).all()
        return [services.product_out(p) for p in rows]

    @r.post("/products", tags=["Products"], summary="Add a product")
    def add_product(body: ProductIn, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        p = Product(user_id=user.id, **body.model_dump())
        p.currency = p.currency.upper()
        db.add(p)
        db.commit()
        return services.product_out(p)

    @r.get("/products/{pid}", tags=["Products"], summary="Get one product")
    def get_product(pid: int, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        return services.product_out(_own_product(db, user, pid))

    @r.put("/products/{pid}", tags=["Products"], summary="Update a product (details, price, stock)")
    def update_product(pid: int, body: ProductIn, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        p = _own_product(db, user, pid)
        for k, v in body.model_dump().items():
            setattr(p, k, v)
        p.currency = p.currency.upper()
        db.commit()
        return services.product_out(p)

    @r.delete("/products/{pid}", tags=["Products"], summary="Delete a product")
    def delete_product(pid: int, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        db.delete(_own_product(db, user, pid))
        db.commit()
        return {"ok": True}

    # ----- orders -----
    @r.post("/orders", tags=["Sales"], summary="Create an order (reduces stock)")
    def create_order(body: OrderIn, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        p = _own_product(db, user, body.product_id)
        if not p.active:
            raise HTTPException(400, "This product is not for sale.")
        if p.stock < body.quantity:
            raise HTTPException(409, f"Only {p.stock} in stock.")
        p.stock -= body.quantity
        o = Order(
            user_id=user.id, product_id=p.id, product_name=p.name, customer_name=body.customer_name,
            customer_email=body.customer_email, quantity=body.quantity, total=round(p.price * body.quantity, 2), currency=p.currency,
        )
        db.add(o)
        db.commit()
        return _order_out(o)

    @r.get("/orders", tags=["Sales"], summary="List orders")
    def list_orders(user: User = Depends(user_dep), db: Session = Depends(get_db)):
        rows = db.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(500)).all()
        return [_order_out(o) for o in rows]

    @r.patch("/orders/{oid}", tags=["Sales"], summary="Change order status")
    def order_status(oid: int, body: OrderStatusIn, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        o = db.scalar(select(Order).where(Order.id == oid, Order.user_id == user.id))
        if not o:
            raise HTTPException(404, "Order not found.")
        if body.status == "cancelled" and o.status != "cancelled":
            p = db.scalar(select(Product).where(Product.id == o.product_id, Product.user_id == user.id))
            if p:
                p.stock += o.quantity
        o.status = body.status
        db.commit()
        return _order_out(o)

    # ----- email -----
    @r.post("/email/send", tags=["Gmail"], summary="Send an email from the connected Gmail account")
    def email_send(body: EmailSendIn, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        services.send_user_email(db, user, body.to, body.subject, body.body)
        return {"ok": True}

    @r.post("/email/auto-reply", tags=["Gmail"], summary="Write (and optionally send) an AI reply to an email you give us")
    def email_auto_reply(body: AutoReplyIn, user: User = Depends(ai_dep), db: Session = Depends(get_db)):
        reply, mode = services.email_reply_text(db, user, body.from_email or "", body.subject, body.body)
        sent = False
        if body.send:
            if not body.from_email:
                raise HTTPException(400, "from_email is required when send is true.")
            subject = body.subject if body.subject.lower().startswith("re:") else f"Re: {body.subject or 'your message'}"
            services.send_user_email(db, user, body.from_email, subject, reply)
            sent = True
        return {"reply": reply, "sent": sent, "mode": mode}

    @r.post("/email/inbox/process", tags=["Gmail"], summary="Read new Gmail messages, write AI replies, send them")
    def inbox_process(body: InboxIn, user: User = Depends(ai_dep), db: Session = Depends(get_db)):
        return {"results": services.process_inbox(db, user, body.limit, body.send)}

    # ----- ads -----
    @r.post("/ads/campaigns", tags=["Ads"], summary="Register an ad campaign")
    def add_campaign(body: CampaignIn, user: User = Depends(user_dep), db: Session = Depends(get_db)):
        c = AdCampaign(user_id=user.id, **body.model_dump())
        db.add(c)
        db.commit()
        return _campaign_out(c)

    @r.get("/ads/campaigns", tags=["Ads"], summary="List ad campaigns")
    def list_campaigns(user: User = Depends(user_dep), db: Session = Depends(get_db)):
        rows = db.scalars(select(AdCampaign).where(AdCampaign.user_id == user.id).order_by(AdCampaign.id.desc())).all()
        return [_campaign_out(c) for c in rows]

    @r.post("/ads/campaigns/{cid}/metrics", tags=["Ads"], summary="Send campaign metrics; get problems and fixes back")
    def ads_metrics(cid: int, body: MetricsIn, user: User = Depends(ai_dep), db: Session = Depends(get_db)):
        c = db.scalar(select(AdCampaign).where(AdCampaign.id == cid, AdCampaign.user_id == user.id))
        if not c:
            raise HTTPException(404, "Campaign not found.")
        return services.ads_ingest(db, user, c, body.model_dump())

    @r.get("/ads/events", tags=["Ads"], summary="Problems found and actions taken")
    def ads_events(user: User = Depends(user_dep), db: Session = Depends(get_db)):
        rows = db.scalars(select(AdEvent).where(AdEvent.user_id == user.id).order_by(AdEvent.id.desc()).limit(100)).all()
        return [
            {"id": e.id, "campaign_id": e.campaign_id, "code": e.code, "severity": e.severity, "diagnosis": e.diagnosis,
             "action": e.action, "status": e.action_status, "suggestion": e.suggestion, "at": e.created_at.isoformat()}
            for e in rows
        ]

    return r
