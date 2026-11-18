"""Owner (admin) control panel API (/api/admin)."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import services
from .database import get_db
from .deps import admin_user
from .models import Payment, Plan, Subscription, UsageLog, User, utcnow
from .routes_dashboard import plan_out

router = APIRouter(prefix="/api/admin", include_in_schema=False, dependencies=[Depends(admin_user)])


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    months: int = Field(ge=1, le=36)
    price_usd: float = Field(ge=0, le=100000)
    description: str = Field("", max_length=300)
    requests_per_month: int = Field(3000, ge=0, le=100_000_000)
    is_active: bool = True


class ActiveIn(BaseModel):
    is_active: bool


class GrantIn(BaseModel):
    plan_id: int
    months: int | None = Field(None, ge=1, le=36)


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    since = utcnow() - timedelta(days=30)
    revenue = db.scalar(select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(Payment.status == "approved")) or 0
    return {
        "clients": db.scalar(select(func.count(User.id)).where(User.role == "client")) or 0,
        "active_subscriptions": db.scalar(select(func.count(func.distinct(Subscription.user_id))).where(Subscription.status == "active", Subscription.expires_at > utcnow())) or 0,
        "pending_payments": db.scalar(select(func.count(Payment.id)).where(Payment.status == "pending")) or 0,
        "revenue_usd": revenue / 100,
        "requests_30d": db.scalar(select(func.count(UsageLog.id)).where(UsageLog.created_at >= since)) or 0,
    }


@router.get("/clients")
def clients(db: Session = Depends(get_db)):
    out = []
    for u in db.scalars(select(User).order_by(User.id.desc()).limit(500)).all():
        sub = services.sub_out(services.active_subscription(db, u.id))
        out.append({
            "id": u.id, "email": u.email, "name": u.name, "business_name": u.business_name, "role": u.role,
            "is_active": u.is_active, "created_at": u.created_at.isoformat(),
            "plan": sub["plan"] if sub else None, "expires_at": sub["expires_at"] if sub else None,
        })
    return out


@router.patch("/clients/{uid}")
def set_active(uid: int, body: ActiveIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "Client not found.")
    if u.id == admin.id:
        raise HTTPException(400, "You cannot disable your own account.")
    u.is_active = body.is_active
    db.commit()
    return {"ok": True}


@router.post("/clients/{uid}/grant")
def grant(uid: int, body: GrantIn, db: Session = Depends(get_db)):
    u, plan = db.get(User, uid), db.get(Plan, body.plan_id)
    if not u or not plan:
        raise HTTPException(404, "Client or plan not found.")
    return {"subscription": services.sub_out(services.activate_plan(db, u.id, plan, body.months))}


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)):
    return [plan_out(p) for p in db.scalars(select(Plan).order_by(Plan.months, Plan.id)).all()]


def _apply(p: Plan, b: PlanIn):
    p.name, p.months, p.price_cents = b.name.strip(), b.months, int(round(b.price_usd * 100))
    p.description, p.requests_per_month, p.is_active = b.description, b.requests_per_month, b.is_active


@router.post("/plans")
def create_plan(body: PlanIn, db: Session = Depends(get_db)):
    p = Plan()
    _apply(p, body)
    db.add(p)
    db.commit()
    return plan_out(p)


@router.put("/plans/{pid}")
def update_plan(pid: int, body: PlanIn, db: Session = Depends(get_db)):
    p = db.get(Plan, pid)
    if not p:
        raise HTTPException(404, "Plan not found.")
    _apply(p, body)
    db.commit()
    return plan_out(p)


@router.get("/payments")
def payments(status: str | None = None, db: Session = Depends(get_db)):
    q = select(Payment).order_by(Payment.id.desc()).limit(300)
    if status:
        q = q.where(Payment.status == status)
    plans = {p.id: p.name for p in db.scalars(select(Plan)).all()}
    emails = {u.id: u.email for u in db.scalars(select(User)).all()}
    return [
        {"id": p.id, "email": emails.get(p.user_id, "?"), "plan": plans.get(p.plan_id, "?"), "amount_usd": p.amount_cents / 100,
         "method": p.method, "reference": p.reference, "status": p.status, "created_at": p.created_at.isoformat()}
        for p in db.scalars(q).all()
    ]


def _review(pid: int, db: Session):
    p = db.get(Payment, pid)
    if not p:
        raise HTTPException(404, "Payment not found.")
    if p.status != "pending":
        raise HTTPException(400, "This payment was already reviewed.")
    return p


@router.post("/payments/{pid}/approve")
def approve(pid: int, db: Session = Depends(get_db)):
    p = _review(pid, db)
    plan = db.get(Plan, p.plan_id)
    p.status, p.reviewed_at = "approved", utcnow()
    services.activate_plan(db, p.user_id, plan)  # commits
    return {"ok": True}


@router.post("/payments/{pid}/reject")
def reject(pid: int, db: Session = Depends(get_db)):
    p = _review(pid, db)
    p.status, p.reviewed_at = "rejected", utcnow()
    db.commit()
    return {"ok": True}
