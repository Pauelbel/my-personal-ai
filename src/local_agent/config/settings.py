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
    sessions_path: Path = Path("data/sessions")
    conversations_path: Path = Path("data/conversations")
    tool_settings_path: Path = Path("data/settings/tools.json")
    memory_path: Path = Path("data/memory")
    memory_model: str = ""
    max_memory_context_chars: int = Field(default=12000, gt=0)
    log_level: str = "INFO"
    max_context_messages: int = Field(default=20, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
