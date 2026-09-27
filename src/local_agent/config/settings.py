"""Настройки хранят изменяемые параметры вне исходного кода приложения."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    lm_studio_base_url: str = "http://127.0.0.1:1234/v1"
    default_model: str = ""
    database_path: Path = Path("data/agent.sqlite3")
    memory_path: Path = Path("data/memory")
    log_level: str = "INFO"
    max_context_messages: int = Field(default=20, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
