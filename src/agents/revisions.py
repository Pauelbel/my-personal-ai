"""Версия исходных файлов агента: для запуска и проверки утверждаемой правки."""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from agents.files import files_of
from agents.models import Agent


def source_revision(files: Mapping[str, str]) -> str:
    """Имена и содержимое входят в хеш: добавленный или удалённый скрипт меняет версию."""
    source = json.dumps(dict(files), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def agent_revision(agent: Agent | Path) -> str:
    folder = agent.folder if isinstance(agent, Agent) else agent
    if folder is None:
        raise ValueError("У агента нет папки с исходными файлами")
    return source_revision(files_of(folder))
