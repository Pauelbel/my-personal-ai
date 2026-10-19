"""Память о пользователе: `data/memory/*.md`, одна строка `- …` — один факт, разделы `## …`.

- profile.md — кто пользователь, preferences.md — привычки и предпочтения, projects.md — проекты,
  solutions.md — успешные решения коротко («задача → что сработало»); остальные файлы — по необходимости.
- В промпт: profile.md и preferences.md целиком (в пределах лимита символов) и до 10 строк из остальных файлов,
  найденных по словам из сообщения пользователя — простым перебором строк, без индекса.
- `.state.json` — до какого сообщения каждой сессии память уже обновлена (для автообновления).
"""

import json
import os
import re
import threading
from pathlib import Path
from uuid import uuid4

DEFAULT_DOCUMENTS = {
    "profile.md": "Профиль",
    "preferences.md": "Предпочтения",
    "projects.md": "Проекты",
    "solutions.md": "Решения задач",
}
ALWAYS = ("profile.md", "preferences.md")
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*\.md$")
WORD = re.compile(r"[a-zа-яё0-9]{4,}", re.I)
MAX_FOUND = 10
# Слова сравниваются по началу: «тест-кейсы» и «тест-кейсов», «Playwright» и «playwright».
STEM = 5


def entries(text: str) -> list[tuple[str, str]]:
    """(раздел, запись) для каждой строки «- …» файла."""
    section, found = "", []
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith("- ") and line[2:].strip():
            found.append((section, line[2:].strip()))
    return found


def clean(text: str) -> str:
    """Запись одной строкой без маркера списка: модель часто присылает «- факт», а строка и так начинается с «- »."""
    return re.sub(r"^(?:[-*•]\s+)+", "", " ".join(text.split()))


def _stems(text: str) -> set[str]:
    return {word.lower()[:STEM] for word in WORD.findall(text)}


class MemoryStore:
    def __init__(self, root: Path, max_context_chars: int = 1500) -> None:
        self.root = root
        self.max_context_chars = max_context_chars
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for name, title in DEFAULT_DOCUMENTS.items():
            path = self.root / name
            if not path.exists():
                _atomic_write(path, f"# {title}\n")

    def names(self) -> list[str]:
        self.initialize()
        return sorted(path.name for path in self.root.glob("*.md"))

    def read(self, name: str) -> str:
        return self._path(name).read_text(encoding="utf-8")

    def documents(self) -> dict[str, str]:
        return {name: self.read(name) for name in self.names()}

    def write(self, name: str, content: str) -> None:
        """Ручная правка из раздела «Память»; новый файл тоже можно создать."""
        with self._lock:
            path = self._path(name, must_exist=False)
            _atomic_write(path, content.replace("\r\n", "\n").rstrip() + "\n")

    # ---------- точечные операции ----------

    def add(self, name: str, section: str, content: str) -> None:
        content, section = clean(content), clean(section).lstrip("#").strip()
        if not content:
            raise ValueError("Пустая запись")
        with self._lock:
            text = self.read(name)
            line = f"- {content}"
            if _normalize(content) in self.existing():
                raise ValueError(f"Такая запись уже есть в памяти: «{content}»")
            lines = text.rstrip().splitlines()
            heading = f"## {section}"
            if heading not in lines:
                lines += ["", heading, "", line]
            else:
                start = lines.index(heading)
                end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
                while end > start + 1 and not lines[end - 1].strip():
                    end -= 1
                lines.insert(end, line)
            _atomic_write(self._path(name), "\n".join(lines).rstrip() + "\n")

    def replace(self, name: str, old: str, new: str | None) -> None:
        """Заменить (new) или удалить (new=None) единственную строку «- old»."""
        old, new = clean(old), clean(new) if new is not None else None
        with self._lock:
            lines = self.read(name).splitlines()
            matches = [index for index, line in enumerate(lines) if line == f"- {old}"]
            if len(matches) != 1:
                raise ValueError(f"Строка «{old}» должна встречаться в {name} ровно один раз")
            if new is None:
                del lines[matches[0]]
            else:
                lines[matches[0]] = f"- {new}"
            _atomic_write(self._path(name), "\n".join(lines).rstrip() + "\n")

    def existing(self) -> set[str]:
        return {_normalize(entry) for text in self.documents().values() for _, entry in entries(text)}

    # ---------- промпт ----------

    def prompt_context(self, query: str) -> str:
        """Профиль и предпочтения целиком, затем до MAX_FOUND строк других файлов, найденных по словам запроса."""
        documents = self.documents()
        lines: list[str] = []
        used = 0

        def take(line: str) -> bool:
            nonlocal used
            if used + len(line) > self.max_context_chars:
                return False
            lines.append(line)
            used += len(line) + 1
            return True

        for name in ALWAYS:
            for section, entry in entries(documents.get(name, "")):
                take(f"- {entry}" + (f" ({section})" if section else ""))
        words = _stems(query)
        scored = []
        for name, text in documents.items():
            if name in ALWAYS:
                continue
            for section, entry in entries(text):
                score = len(words & _stems(f"{entry} {section}"))
                if score:
                    scored.append((score, f"- {entry} ({name.removesuffix('.md')}: {section})" if section else f"- {entry}"))
        scored.sort(key=lambda item: -item[0])
        for _, line in scored[:MAX_FOUND]:
            take(line)
        return "\n".join(lines)

    # ---------- автообновление ----------

    def checkpoint(self, session_id: str) -> str | None:
        return self._state().get(session_id)

    def set_checkpoint(self, session_id: str, message_id: str) -> None:
        with self._lock:
            state = self._state()
            state[session_id] = message_id
            _atomic_write(self.root / ".state.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    def _state(self) -> dict:
        try:
            state = json.loads((self.root / ".state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        # Старый формат: {"version": 1, "sessions": {id: {"last_processed_message_id": …}}}.
        if isinstance(state.get("sessions"), dict):
            return {key: value.get("last_processed_message_id") for key, value in state["sessions"].items()}
        return state if isinstance(state, dict) else {}

    def _path(self, name: str, must_exist: bool = True) -> Path:
        if not SAFE_NAME.fullmatch(name or ""):
            raise ValueError("Имя файла памяти — латиница, цифры, - и _, с .md на конце")
        self.initialize()
        path = self.root / name
        if must_exist and not path.is_file():
            raise ValueError(f"Файла памяти {name} нет. Есть: {', '.join(self.names())}")
        return path


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
