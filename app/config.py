"""Settings, read from environment variables (or a local .env file)."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    f = BASE_DIR / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))


def _load_secret() -> str:
    env = os.getenv("SECRET_KEY", "")
    if len(env) >= 16:
        return env
    # Local development fallback: create one once and keep it in data/secret.key.
    # On a host with a temporary disk (Render free, etc.) set SECRET_KEY yourself,
    # otherwise every restart would sign everyone out and lose saved Gmail logins.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = DATA_DIR / "secret.key"
    if f.exists():
        return f.read_text().strip()
    s = secrets.token_urlsafe(48)
    f.write_text(s)
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return s


class Settings:
    def __init__(self) -> None:
        e = os.getenv
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.app_name = e("APP_NAME", "BizPilot AI")
        self.secret_key = _load_secret()
        self.database_url = e("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}")
        self.admin_email = e("ADMIN_EMAIL", "").strip().lower()
        # AI provider: demo | anthropic | openai (any OpenAI-compatible endpoint)
        self.llm_provider = e("LLM_PROVIDER", "demo").strip().lower()
        self.llm_api_key = e("LLM_API_KEY", "")
        self.llm_model = e("LLM_MODEL", "")
        self.llm_base_url = e("LLM_BASE_URL", "")
        self.cors_origins = [o.strip() for o in e("CORS_ORIGINS", "").split(",") if o.strip()]
        self.token_ttl_hours = int(e("TOKEN_TTL_HOURS", "168"))
        self.rate_limit_per_min = int(e("RATE_LIMIT_PER_MIN", "60"))
        self.email_daily_limit = int(e("EMAIL_DAILY_LIMIT", "100"))
        self.trial_messages = int(e("TRIAL_MESSAGES", "10"))
        self.payment_instructions = e(
            "PAYMENT_INSTRUCTIONS",
            "Send the plan price in USD using the payment details shared by the site owner, "
            "then submit your transaction ID below. Your plan is activated after the owner confirms the payment.",
        )


settings = Settings()
