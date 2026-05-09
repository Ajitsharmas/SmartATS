# ------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: Application Configuration and Env Vars
# ------------------------------------------------------------------------------------------------------------------------------------------\
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # App Config
    APP_NAME: str = "SmartATS"
    DEBUG: bool = True

    # Database Config
    DATABASE_URL: str = "postgresql://resume_user:resume_pass@localhost:5432/resume_db"

    # AI Config
    # Modes: "gemini" or "local"
    AI_MODE: str = "gemini"

    # Gemini Config (Get key from aistudio.google.com)
    GEMINI_API_KEY: str = "fake-key-for-dev"

    # Local Config
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434/api/generate"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

# Initialize settings
settings = Settings()
