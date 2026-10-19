"""Секреты скиллов: токены и адреса, которые модель никогда не видит.

Имена объявлены в SKILL.md (`secrets: [JIRA_URL, JIRA_TOKEN]`), значения — `data/secrets/<скилл>.json`,
вне папки скилла: копии в истории и сама папка секретов не содержат. Значения вводятся в карточке скилла
в UI; API отдаёт только «задан / не задан». Скрипт получает значения переменными окружения; если значение
всё же попало в его вывод, оно заменяется на *** до того, как вывод увидит модель.
"""

import json
import os
import re
import threading
from pathlib import Path

SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MAX_VALUE_CHARS = 8000
# Короткие значения (да/нет, номер порта) не маскируем: замена «1» на *** испортила бы весь вывод.
MIN_MASKED_CHARS = 6
MASK = "***"


def check_secret_names(names: object) -> str | None:
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return "secrets — список имён"
    bad = [name for name in names if not SECRET_NAME.fullmatch(name)]
    return f"Недопустимые имена секретов: {', '.join(bad)} (нужно, например, JIRA_TOKEN)" if bad else None


class SecretStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()

    def values(self, skill: str) -> dict[str, str]:
        try:
            data = json.loads(self._path(skill).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return {key: value for key, value in data.items() if isinstance(value, str)} if isinstance(data, dict) else {}

    def status(self, skill: str, names: list[str]) -> dict[str, bool]:
        """Какие из объявленных секретов заданы — без значений."""
        values = self.values(skill)
        return {name: name in values for name in names}

    def set(self, skill: str, name: str, value: str) -> None:
        if not SECRET_NAME.fullmatch(name):
            raise ValueError("Имя секрета — латинские заглавные буквы, цифры и _, например JIRA_TOKEN")
        value = value.strip()
        if not value or len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"Значение секрета — непустая строка до {MAX_VALUE_CHARS} символов")
        with self._lock:
            values = self.values(skill)
            values[name] = value
            self._write(skill, values)

    def delete(self, skill: str, name: str) -> None:
        with self._lock:
            values = self.values(skill)
            values.pop(name, None)
            self._write(skill, values)

    @staticmethod
    def mask(text: str, values: dict[str, str]) -> str:
        for value in sorted(values.values(), key=len, reverse=True):
            if len(value) >= MIN_MASKED_CHARS:
                text = text.replace(value, MASK)
        return text

    def _path(self, skill: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", skill):
            raise ValueError("Недопустимое имя скилла")
        return self.root / f"{skill}.json"

    def _write(self, skill: str, values: dict[str, str]) -> None:
        path = self._path(skill)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        # Файл сразу создаётся с правами только владельца: секрет не бывает читаемым для других ни на миг.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(sorted(values.items())), stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
