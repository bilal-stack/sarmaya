from pydantic_settings import BaseSettings, SettingsConfigDict, NoDecode
from pydantic import field_validator, model_validator
from functools import lru_cache
from pathlib import Path
from typing import Annotated, List
import json

# Point to .env at project root (c:\python\os\.env)
ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

#: The value shipped in the repository. Checked by name so the refusal below
#: cannot drift from the default above.
PLACEHOLDER_SECRET = "change-me-in-production-min-32-chars"


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Sarmaya OS"
    APP_VERSION: str = "1.0.0"
    #: Off by default. The 500 handler includes the exception type and message
    #: when this is on, so a deployment that inherited a True default would
    #: hand internals to anyone who could provoke an error. Turn it on locally
    #: via .env, never on a server.
    DEBUG: bool = False
    
    # CORS Configuration. Includes the Next.js frontend dev port (9002) plus the
    # common Vite/CRA ports. Override via the CORS_ORIGINS env var in production.
    #: NoDecode because pydantic-settings JSON-decodes list fields inside the
    #: settings source, before any validator runs — so `CORS_ORIGINS=https://x`
    #: died with a JSON parse error and the tolerant parser below never got a
    #: look. Which is the exact failure it was written to prevent, and the sort
    #: a hosting provider's environment box produces by default.
    CORS_ORIGINS: Annotated[List[str], NoDecode] = [
        "http://localhost:9002", "http://127.0.0.1:9002",
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:5173", "http://127.0.0.1:5173",
    ]
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: list = ["*"]
    CORS_ALLOW_HEADERS: list = ["*"]
    
    # Database
    # Runtime connection. For Postgres Row Level Security to actually isolate
    # tenants, this MUST be a non-superuser, non-BYPASSRLS role (e.g. os_app).
    # Superusers and BYPASSRLS roles silently bypass every RLS policy.
    DATABASE_URL: str = "postgresql://postgres:root@localhost:5432/os"
    # Privileged connection used ONLY for schema migrations / DDL (Alembic).
    # May be the table owner or a superuser; the running app must never use it.
    ADMIN_DATABASE_URL: str = "postgresql://postgres:root@localhost:5432/os"

    SQLALCHEMY_ECHO: bool = False
    
    # Whether strangers may sign themselves into an existing tenant by naming
    # its slug. Off by default: even at the default clerk role a self-registered
    # user can create vendors, raise invoices, prepare payment runs and import
    # bank statements. An accounts-payable system enrols staff; it does not take
    # walk-ins. Administrators create accounts through POST /users.
    ALLOW_SELF_REGISTRATION: bool = False

    #: How long after approval a vendor's new bank details must wait before a
    #: payment may use them. The window is the control: it gives the real
    #: vendor time to notice a change they never requested, and the approver
    #: time to confirm it on a number they already had.
    #:
    #: An operator setting rather than tenant configuration — a fraud control
    #: whose timing the tenant can edit is one an attacker with a tenant login
    #: can set to zero.
    VENDOR_BANK_CHANGE_COOLING_HOURS: int = 24

    #: How long something may sit with an approver before they are nudged, and
    #: the minimum gap between nudges. Distinct from the per-state SLA, which
    #: says when lateness becomes somebody else's problem: this is the quiet
    #: prod beforehand, so the escalation never has to fire. Daily gets read;
    #: hourly gets filtered to a folder.
    REMINDER_INTERVAL_HOURS: int = 24

    #: How far into an item's SLA a reminder fires, as a fraction of it. Halfway
    #: by default: late enough that the nudge is warranted, early enough that
    #: acting on it still avoids the breach.
    #:
    #: A fraction rather than a fixed delay because the delay has to be shorter
    #: than the deadline it is protecting, and the deadlines differ per state —
    #: a fixed 24h reminder against a 24h SLA fires exactly when escalation
    #: takes over, which is to say never.
    REMINDER_AT_SLA_FRACTION: float = 0.5

    # Security
    #: Every access token is signed with this. The placeholder below is in a
    #: public repository, so a deployment that keeps it has tokens anyone who
    #: reads the source can forge — including one claiming an admin's id. The
    #: validator at the bottom of this class refuses to start with it unless
    #: DEBUG is on.
    SECRET_KEY: str = "change-me-in-production-min-32-chars"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    
    # Email (for approval notifications)
    #: Delivery is opt-in. Every notification is sent synchronously inside the
    #: request that triggered it, so an unreachable mail server does not fail
    #: quietly — it stalls the action. That was tolerable while notifications
    #: fired only on invoice submit/approve/reject; the change watchlist fires
    #: on ordinary vendor edits, which put a socket connect in the path of a
    #: routine save. Defaulting to off means a deployment that has not
    #: configured SMTP pays nothing, rather than paying a timeout per write and
    #: swallowing the error.
    SMTP_ENABLED: bool = False
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "noreply@sarmaya.com"
    #: Seconds. Short on purpose: this is on the request path.
    SMTP_TIMEOUT: int = 5
    
    # AI Services
    GOOGLE_CLOUD_VISION_CREDENTIALS: str = ""
    
    # AI Configuration
    AI_PROVIDER: str = "openai"  # 'openai', 'claude', 'gemini', 'grok'
    AI_ENHANCED_OCR: bool = True  # Use AI to enhance OCR results
    # HITL trigger (Build Book): extraction below this confidence routes to
    # human review instead of straight to validation.
    AI_EXTRACTION_REVIEW_THRESHOLD: int = 70

    # Grok Configuration
    GROK_API_KEY: str = ""

    # OpenAI Configuration
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4"  # or 'gpt-3.5-turbo' for cost savings
    
    # Anthropic Configuration
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-4-8"  # override in .env (e.g. claude-haiku-4-5 for cheaper)

    # Google Gemini Configuration
    GOOGLE_AI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"  # override in .env (e.g. gemini-2.5-pro)

    # File Storage
    UPLOAD_DIR: str = "./uploads"
    MAX_FILE_SIZE_MB: int = 10
    
    # OCR Configuration
    OCR_PROVIDER: str = "ocr_space"  # 'ocr_space', 'aws_textract', 'document_ai'
    OCR_SPACE_API_KEY: str = ""
    OCR_SPACE_API_URL: str = "https://api.ocr.space/parse/image"
    
    # AWS Textract (for future)
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    
    # Google Document AI
    GOOGLE_CLOUD_PROJECT_ID: str = ""
    GOOGLE_CLOUD_LOCATION: str = "us"  # 'us', 'eu', etc.
    GOOGLE_DOCUMENT_AI_PROCESSOR_ID: str = ""
    GOOGLE_APPLICATION_CREDENTIALS: str = ""  # Path to service account JSON
    
    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_origins(cls, v):
        """Accept a comma-separated list as well as JSON.

        pydantic-settings parses a `list` field from the environment as JSON,
        so `CORS_ORIGINS=https://app.example.com` fails to start with a parse
        error — and a comma-separated string is what a person actually types
        into a hosting provider's environment box.
        """
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                return json.loads(text)
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return v

    @model_validator(mode="after")
    def _refuse_insecure_production(self):
        """Fail at startup rather than serve something forgeable.

        A misconfiguration that only shows up as "tokens are forgeable" shows
        up as nothing at all — the app works perfectly, right up until someone
        who has read the public repo mints an admin token. So it is refused
        loudly, at the only moment anyone is watching: the deploy.
        """
        if self.DEBUG:
            return self

        if self.SECRET_KEY == PLACEHOLDER_SECRET:
            raise ValueError(
                "SECRET_KEY is still the placeholder from the repository. "
                "Every access token would be forgeable by anyone who can read "
                "the source. Set SECRET_KEY to a random value "
                "(python -c \"import secrets; print(secrets.token_urlsafe(48))\")."
            )
        if len(self.SECRET_KEY) < 32:
            raise ValueError(
                "SECRET_KEY must be at least 32 characters."
            )
        if any("localhost" in o or "127.0.0.1" in o for o in self.CORS_ORIGINS):
            # Not fatal on its own, but it means CORS_ORIGINS was never set for
            # this environment, and the browser will refuse the real frontend.
            raise ValueError(
                "CORS_ORIGINS still lists localhost, so the deployed frontend "
                "would be refused by the browser. Set it to your frontend's "
                "origin, e.g. CORS_ORIGINS=https://sarmaya.vercel.app"
            )
        return self

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        case_sensitive=True,
    )

@lru_cache()
def get_settings():
    return Settings()

settings = get_settings()