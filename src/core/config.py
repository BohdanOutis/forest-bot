import os
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    bot_token: str
    groq_api_key: str
    groq_model: str = "openai/gpt-oss-20b"
    database_url: str

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()