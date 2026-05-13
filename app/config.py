# ------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: Application Configuration and Env Vars
# ------------------------------------------------------------------------------------------------------------------------------------------\
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # App Config
    APP_NAME: str = "SmartATS"
    DEBUG: bool = True

    # Database Config
    DATABASE_URL: str = "postgresql://username:password@localhost:5432/db_name"

    # AI Config
    # Modes: "gemini" or "local"
    AI_MODE: str = "gemini"

    # Gemini Config (Get key from aistudio.google.com)
    GEMINI_API_KEY: str = "fake-key-for-dev"

    # Local Config
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434/api/generate"

    # Storage Config (MinIO)
    # In the production, these would come from .env files
    MINIO_ENDPOINT: str = "http://localhost:9000"
    MINIO_ACCESS_KEY: str = "dummy"
    MINIO_SECRET_KEY: str = "dummy"
    MINIO_BUCKET_NAME: str = "resumes"

    # Celery & Redis Config
    #The URL for the Broker (TThe Queue)
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    # The URL for the Backend (where the results are stored)
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"
    # Redis URL for SlowAPI rate-limit counters (separate concern from Celery)
    RATE_LIMITER_STORAGE_URL: str = "redis://localhost:6379/0"

    # Security Configs
    # Run "openssl rand -hex 32" in terminal to generate a real key
    # We pasted an example Dummy key below
    SECRET_KEY: str = "79f0da0c3f80646ad690a44e39706380c40d0d777f5df57ad531c218f86bb270"
    ALGORITHM: str ="HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Email Config (Resend)
    RESEND_API_KEY: str = "re_fake_key_for_dev"
    FROM_EMAIL: str = "SmartATS <onboarding@resend.dev>"
    APP_BASE_URL: str = "http://localhost:8000"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

# Initialize settings
settings = Settings()
