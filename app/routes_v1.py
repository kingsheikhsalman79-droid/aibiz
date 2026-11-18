"""Public client API (/v1). Authenticated with an API key; works only while the subscription is active."""
from fastapi import APIRouter, Depends

from .deps import ApiCtx, api_ctx, v1_ai, v1_user
from .routes_account import build_router
from .services import sub_out

router = APIRouter(prefix="/v1")


@router.get("/me", tags=["Account"], summary="Check your key and see how many days are left")
def me(ctx: ApiCtx = Depends(api_ctx)):
    return {
        "business_name": ctx.user.business_name,
        "key_name": ctx.key.name,
        "subscription": sub_out(ctx.sub),
    }


router.include_router(build_router(v1_user, v1_ai, v1_ai, "api"))
