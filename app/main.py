import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .config import settings
from .database import Base, SessionLocal, engine
from .models import Plan, User
from .routes_admin import router as admin_router
from .routes_dashboard import router as dashboard_router
from .routes_v1 import router as v1_router
from .services import ServiceError

log = logging.getLogger("bizpilot")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

DEFAULT_PLANS = [
    ("Starter", 1, 1900, "Everything you need to start. Ideal for one shop.", 3000),
    ("Growth", 2, 3500, "Two months at a lower monthly price.", 3000),
    ("Pro Year", 12, 19000, "Best value: a full year of access.", 3000),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if not db.scalar(select(Plan.id).limit(1)):
            for name, months, cents, desc, reqs in DEFAULT_PLANS:
                db.add(Plan(name=name, months=months, price_cents=cents, description=desc, requests_per_month=reqs))
            db.commit()
        if settings.admin_email:
            u = db.scalar(select(User).where(User.email == settings.admin_email))
            if u and u.role != "admin":
                u.role = "admin"
                db.commit()
        else:
            log.warning("ADMIN_EMAIL is not set: the first account that registers becomes the admin. Set ADMIN_EMAIL before going public.")
    yield


app = FastAPI(
    title=f"{settings.app_name} API",
    version="1.0.0",
    description=(
        "Client API for products, sales, AI customer chat, Gmail replies and ads monitoring.\n\n"
        "**Authentication:** send your key as `Authorization: Bearer sk_live_...` (or `X-API-Key`). "
        "Keys work only while your subscription is active; after it ends every call returns HTTP 402 until you renew."
    ),
    lifespan=lifespan,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path not in ("/docs", "/redoc", "/openapi.json"):  # Swagger UI needs its own CDN scripts
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
    return response


@app.exception_handler(ServiceError)
async def service_error(request: Request, exc: ServiceError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Something went wrong. Please try again."}, status_code=500)


app.include_router(dashboard_router)
app.include_router(admin_router)
app.include_router(v1_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}
