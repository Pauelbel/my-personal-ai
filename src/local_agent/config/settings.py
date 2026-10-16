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
    # Пусто — рядом с остальными данными, в папке над sessions_path (обычно data/).
    tools_path: Path | None = None
    wiki_path: Path | None = None
    attachments_path: Path | None = None
    index_path: Path | None = None
    checkpoints_path: Path | None = None
    mcp_config_path: Path | None = None
    # Общее Python-окружение песочницы: пакеты, которые пользователь разрешил поставить скриптам агента.
    sandbox_env_path: Path | None = None
    # Секреты инструментов (токены, адреса): задаются формой коннектора («настроить»), модель их не видит.
    secrets_path: Path | None = None
    logs_path: Path | None = None
    # Модель для саб-агентов, чтобы они не делили одну модель с основным: провайдер из LLM_PROVIDERS и ID модели.
    # Пусто — модель сессии, которая поручила задачу.
    subagent_provider: str = ""
    subagent_model: str = ""
    # Сколько задач саб-агентов выполняется одновременно; остальные ждут очереди.
    max_parallel_tasks: int = Field(default=2, gt=0)
    # Запускать MCP-серверы (браузер, веб-поиск) при старте.
    mcp_enabled: bool = True
    memory_model: str = ""
    # Сколько символов памяти (профиль и найденные записи) попадает в промпт.
    max_memory_context_chars: int = Field(default=800, gt=0)
    memory_auto_update_messages: int = Field(default=10, ge=0)
    # Чат вики помнит столько последних реплик пользователя вместе с ответами; всё раньше забывается.
    wiki_context_messages: int = Field(default=3, ge=1)
    # Сколько раундов вызова инструментов модель может сделать за один ход (у агента можно задать своё в файле).
    max_tool_rounds: int = Field(default=40, ge=1, le=200)
    log_level: str = "INFO"
    max_context_messages: int = Field(default=20, gt=0)
    default_context_tokens: int = Field(default=8192, gt=0)
    response_reserve_tokens: int = Field(default=1024, ge=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_verify_ssl: bool = True
    allowed_hosts: str = "127.0.0.1,localhost"

    @property
    def data_dir(self) -> Path:
        return self.sessions_path.parent

    def data_path(self, name: str, default: str) -> Path:
        value = getattr(self, name)
        return value if value is not None else self.data_dir / default

    @property
    def allowed_host_set(self) -> set[str]:
        return {host.strip().lower() for host in self.allowed_hosts.split(",") if host.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
