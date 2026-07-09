from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from pathlib import Path

# Point to .env at project root (c:\python\os\.env)
ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

class Settings(BaseSettings):
    # App
    APP_NAME: str = "Sarmaya OS"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True
    
    # CORS Configuration. Includes the Next.js frontend dev port (9002) plus the
    # common Vite/CRA ports. Override via the CORS_ORIGINS env var in production.
    CORS_ORIGINS: list = [
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
    
    # Security
    SECRET_KEY: str = "change-me-in-production-min-32-chars"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    
    # Email (for approval notifications)
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "noreply@sarmaya.com"
    
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
    
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        case_sensitive=True,
    )

@lru_cache()
def get_settings():
    return Settings()

settings = get_settings()