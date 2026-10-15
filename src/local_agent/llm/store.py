"""Провайдеры моделей, добавленные в интерфейсе: Ollama, LM Studio или OpenAI-совместимый API с ключом.

Хранятся в data/providers.json рядом с остальными данными; файл доступен только владельцу — в нём ключи API.
Провайдеры из .env (LLM_BASE_URL, LLM_PROVIDERS) остаются как были и в интерфейсе только показываются.
"""

import json
import os
import re
import threading
from pathlib import Path

from pydantic import BaseModel, Field

# Тип сервера → подпись и адрес по умолчанию: адрес подставляется в форму, его можно поменять.
KINDS = {
    "ollama": ("Ollama", "http://127.0.0.1:11434/v1"),
    "lmstudio": ("LM Studio", "http://127.0.0.1:1234/v1"),
    "openai": ("OpenAI-совместимый API", "https://api.openai.com/v1"),
}


class ProviderEntry(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    name: str
    kind: str = "openai"
    base_url: str
    api_key: str = ""


class ProviderFile(BaseModel):
    providers: list[ProviderEntry] = []
    # Модель для новых проектов и личных чатов — выбор в настройках вместо DEFAULT_MODEL из .env.
    default_provider: str = ""
    default_model: str = ""


class ProviderStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> ProviderFile:
        try:
            return ProviderFile.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ProviderFile()

    def save(self, data: ProviderFile) -> ProviderFile:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            # Ключи API: файл сразу создаётся с правами только для владельца.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data.model_dump(), handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        return data


def provider_id(name: str, taken: set[str]) -> str:
    """id из названия: латиница, цифры и дефис; название без латиницы — «provider», «provider-2» и т. д."""
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "provider"
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def masked(key: str) -> str:
    """Ключ в интерфейсе — только хвост: по нему видно, какой ключ сохранён, но целиком его не достать."""
    return f"…{key[-4:]}" if len(key) > 8 else ("задан" if key else "")
