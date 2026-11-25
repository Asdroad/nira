from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator, Field, SecretStr, AliasChoices

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NIRA_", env_file=str(ROOT / ".env"), extra="ignore")
    env: str = "local"
    database_url: str = "sqlite:///.data/nira.db"
    storage_path: Path = ROOT / ".data/files"
    cookie_secure: bool = False
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    max_file_mb: int = 25
    session_hours: int = 12
    ocr_enabled: bool = True
    ocr_languages: str = "rus+eng"
    ocr_max_pages: int = 30
    ocr_page_timeout: int = 40
    document_timeout: int = 180
    tesseract_path: str = ""
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices("OPENROUTER_API_KEY", "NIRA_OPENROUTER_API_KEY"))
    ai_model: str = "openai/gpt-6-luna"
    ai_enabled: bool = True
    ai_auto_run: bool = True
    ai_timeout: int = Field(default=120, ge=10, le=180)
    ai_max_context_chars: int = Field(default=100000, ge=10000, le=300000)
    ai_max_output_tokens: int = Field(default=8000, ge=1000, le=24000)
    ai_max_input_price: float = Field(default=0.1, gt=0, le=100)  # USD / million tokens
    ai_max_output_price: float = Field(default=0.5, gt=0, le=500)
    ai_request_budget_usd: float = Field(default=0.02, gt=0, le=10)
    ai_task_budget_usd: float = Field(default=0.10, gt=0, le=100)

    @model_validator(mode="after")
    def validate_deployment(self):
        if self.env not in {"local", "test", "production"}:
            raise ValueError("NIRA_ENV must be local, test or production")
        if self.env == "production" and (not self.cookie_secure or "sqlite" in self.database_url):
            raise ValueError("Production requires PostgreSQL and secure HTTPS cookies")
        if not self.storage_path.is_absolute():
            self.storage_path = ROOT / self.storage_path
        return self


@lru_cache
def get_settings():
    return Settings()
