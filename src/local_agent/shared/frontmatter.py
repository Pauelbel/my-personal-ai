"""Markdown-файл с блоком параметров: «---», строки «ключ: значение», «---», затем текст. Так записан SKILL.md."""

import re
from pathlib import Path

ITEM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


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
