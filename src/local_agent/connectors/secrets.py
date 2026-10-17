"""Секреты для инструментов: токены и адреса, которые модель никогда не видит.

Значения хранятся в data/secrets.json (права только у владельца) и задаются формой «настроить» у коннектора
в «Возможности → Инструменты» — токены и пароли API обратно не отдаёт (адрес и логин — не тайна, их видно). Скрипт инструмента получает переменными окружения лишь те секреты, что перечислены
в его tool.md (имя с ? на конце — необязательный секрет). Если значение секрета всё же попало в вывод,
оно заменяется на *** до того, как вывод увидит модель (кроме адресов и логинов: *_URL, *_EMAIL, *_USER, *_LOGIN).
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
# Адреса и логины не маскируем: без них не построить ссылку на задачу, а тайны в них нет.
PUBLIC_SUFFIXES = ("_URL", "_EMAIL", "_USER", "_LOGIN")


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        return sorted(self._read())

    def get(self, name: str) -> str | None:
        return self._read().get(name)

    def pick(self, names: list[str] | tuple[str, ...]) -> dict[str, str]:
        values = self._read()
        return {name: values[name] for name in names if name in values}

    def set(self, name: str, value: str) -> None:
        if not SECRET_NAME.fullmatch(name):
            raise ValueError("Имя секрета — латинские заглавные буквы, цифры и _, например JIRA_TOKEN")
        value = value.strip()
        if not value or len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"Значение секрета — непустая строка до {MAX_VALUE_CHARS} символов")
        with self._lock:
            values = self._read()
            values[name] = value
            self._write(values)

    def delete(self, name: str) -> None:
        with self._lock:
            values = self._read()
            if values.pop(name, None) is None:
                raise FileNotFoundError(f"Секрета {name} нет")
            self._write(values)

    def mask(self, text: str) -> str:
        hidden = [value for name, value in self._read().items() if not name.endswith(PUBLIC_SUFFIXES)]
        for value in sorted(hidden, key=len, reverse=True):
            if len(value) >= MIN_MASKED_CHARS:
                text = text.replace(value, MASK)
        return text

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return {key: value for key, value in data.items() if isinstance(value, str)} if isinstance(data, dict) else {}

    def _write(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        # Файл сразу создаётся с правами только владельца: секрет не бывает читаемым для других ни на миг.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(sorted(values.items())), stream, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path)


def check_secret_names(names: object) -> str | None:
    """Имя секрета; ? на конце — необязательный: без него инструмент всё равно запускается."""
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return "secrets — список имён"
    bad = [name for name in names if not SECRET_NAME.fullmatch(name.removesuffix("?"))]
    return f"Недопустимые имена секретов: {', '.join(bad)} (нужно, например, JIRA_TOKEN)" if bad else None
