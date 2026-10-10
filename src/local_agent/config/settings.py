"""Настройки хранят изменяемые параметры вне исходного кода приложения."""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderConfig(BaseModel):
    """Дополнительный OpenAI-compatible сервер: Ollama, второй LM Studio или облачный API."""

    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    name: str
    base_url: str
    api_key: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # Основной OpenAI-compatible сервер: Ollama, LM Studio, vLLM. LM_STUDIO_BASE_URL — старое имя, читается для совместимости.
    llm_base_url: str = Field(
        default="http://127.0.0.1:1234/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "LM_STUDIO_BASE_URL"),
    )
    llm_name: str = "Локальный сервер"
    llm_providers: list[ProviderConfig] = Field(default_factory=list)
    default_model: str = ""
    agents_path: Path = Path("data/agents")
    skills_path: Path = Path("data/skills")
    sessions_path: Path = Path("data/sessions")
    projects_path: Path = Path("data/projects")
    conversations_path: Path = Path("data/conversations")
    memory_path: Path = Path("data/memory")
    doc_index_path: Path = Path("data/doc_index")
    # Шаг — один ход одного агента холста; на нём ход команды останавливается, даже если агенты не договорились.
    max_run_steps: int = Field(default=20, gt=0)
    memory_model: str = ""
    max_memory_context_chars: int = Field(default=12000, gt=0)
    memory_auto_update_messages: int = Field(default=10, ge=0)
    log_level: str = "INFO"
    max_context_messages: int = Field(default=20, gt=0)
    default_context_tokens: int = Field(default=8192, gt=0)
    response_reserve_tokens: int = Field(default=1024, ge=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    allowed_hosts: str = "127.0.0.1,localhost"

    @property
    def allowed_host_set(self) -> set[str]:
        return {host.strip().lower() for host in self.allowed_hosts.split(",") if host.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
