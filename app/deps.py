"""Authentication dependencies: dashboard login tokens and client API keys."""
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import ApiKey, Subscription, UsageLog, User, utcnow
from .security import hash_api_key, read_token
from .services import active_subscription


class Limiter:
    """Small in-memory sliding-window limiter (per server process)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits = defaultdict(deque)

    def allow(self, key) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


api_limiter = Limiter(settings.rate_limit_per_min, 60)
login_limiter = Limiter(10, 600)


def _bearer(request: Request):
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


# ---------- dashboard (login token) ----------
def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _bearer(request)
    uid = read_token(token, settings.secret_key) if token else None
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        raise HTTPException(401, "Please sign in again.")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin only.")
    return user


def _use_quota(db: Session, sub):
    if sub is None:
        return
    fresh = db.get(Subscription, sub.id)
    if fresh.request_limit and fresh.requests_used >= fresh.request_limit:
        raise HTTPException(429, "The AI request quota of your plan is used up. Renew or upgrade your plan.")
    fresh.requests_used += 1
    db.commit()


def dash_ai(user: User = Depends(current_user), db: Session = Depends(get_db)) -> User:
    if user.role != "admin":
        sub = active_subscription(db, user.id)
        if not sub:
            raise HTTPException(402, "An active plan is required for this feature. Open Billing to activate one.")
        _use_quota(db, sub)
    return user


def dash_chat(user: User = Depends(current_user), db: Session = Depends(get_db)) -> User:
    """Playground chat: paid plan, or a few free trial messages."""
    if user.role == "admin":
        return user
    sub = active_subscription(db, user.id)
    if sub:
        _use_quota(db, sub)
        return user
    used = db.scalar(select(func.count(UsageLog.id)).where(UsageLog.user_id == user.id, UsageLog.endpoint == "playground"))
    if used >= settings.trial_messages:
        raise HTTPException(402, "Your free trial messages are finished. Activate a plan in Billing to continue.")
    db.add(UsageLog(user_id=user.id, api_key_id=None, endpoint="playground"))
    db.commit()
    return user


# ---------- client API (API key) ----------
@dataclass
class ApiCtx:
    user: User
    key: ApiKey
    sub: object


def api_ctx(request: Request, db: Session = Depends(get_db)) -> ApiCtx:
    raw = request.headers.get("x-api-key") or _bearer(request)
    if not raw or not raw.startswith("sk_"):
        raise HTTPException(401, "Missing or invalid API key. Send it as 'Authorization: Bearer sk_live_...' or in the 'X-API-Key' header.")
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw), ApiKey.revoked.is_(False)))
    if not key:
        raise HTTPException(401, "Invalid or revoked API key.")
    user = db.get(User, key.user_id)
    if not user or not user.is_active:
        raise HTTPException(403, "This account is disabled.")
    sub = None
    if user.role != "admin":
        sub = active_subscription(db, user.id)
        if not sub:
            raise HTTPException(402, "Your subscription has expired or is not active. Renew it in your dashboard and this key works again.")
    if not api_limiter.allow(key.id):
        raise HTTPException(429, "Too many requests. Slow down and retry in a minute.", headers={"Retry-After": "60"})
    key.last_used_at = utcnow()
    db.add(UsageLog(user_id=user.id, api_key_id=key.id, endpoint=request.url.path[:100]))
    db.commit()
    return ApiCtx(user, key, sub)


def v1_user(ctx: ApiCtx = Depends(api_ctx)) -> User:
    return ctx.user


def v1_ai(ctx: ApiCtx = Depends(api_ctx), db: Session = Depends(get_db)) -> User:
    _use_quota(db, ctx.sub)
    return ctx.user
