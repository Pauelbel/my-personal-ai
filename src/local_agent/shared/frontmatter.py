"""Markdown-файл с блоком параметров: «---», строки «ключ: значение», «---», затем текст.

Так записаны агенты (data/agents) и скиллы (data/skills); архив — подпапка .deleted с файлами <id>.<uuid>.md.
"""

import re
from pathlib import Path
from uuid import uuid4

ITEM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ARCHIVED_ID = re.compile(r"(?P<id>[a-z0-9][a-z0-9_-]*)\.[0-9a-f]{32}\.md")


def split(path: Path) -> tuple[str, str]:
    """(блок параметров, текст)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[3:]:
        raise ValueError(f"{path}: файл должен начинаться с блока --- параметры ---")
    header, body = text[4:].split("\n---\n", 1)
    return header, body


def fields(path: Path, header: str, known: set[str]) -> dict[str, str]:
    """Параметры блока; незнакомый ключ — ошибка: опечатка не должна молча менять поведение."""
    values: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or key not in known:
            raise ValueError(f"{path}: неизвестный параметр «{line.strip()}»")
        values[key] = value.strip()
    return values


def items(value: str) -> list[str]:
    """Список через запятую: «read_file, git» → ["read_file", "git"]."""
    return [item.strip() for item in value.split(",") if item.strip()]


def archive(root: Path, item_id: str) -> None:
    """Убирает <id>.md в .deleted под уникальным именем: вернуть можно, переложив файл обратно."""
    path = root / f"{item_id}.md"
    folder = root / ".deleted"
    folder.mkdir(exist_ok=True)
    path.rename(folder / f"{item_id}.{uuid4().hex}.md")
