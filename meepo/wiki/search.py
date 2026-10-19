"""Поиск по заметкам вики перебором Markdown-файлов — без индекса и SQLite.

Файлы режутся на разделы по заголовкам 1–3 уровня (длинные — по абзацам), раздел — это (путь, заголовок,
строка, текст). Слово запроса совпадает, если его основа (без последних букв у длинных слов) есть в тексте:
так «авторизация» находит «авторизации». Ранжирование — по числу совпавших слов; слово в имени файла
или заголовке раздела весит вдвое больше, чем в тексте.
"""

import os
import re
from pathlib import Path

DOC_SUFFIXES = {".md", ".markdown"}
CHUNK_CHARS = 1500
MAX_FILE_BYTES = 1024 * 1024
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def terms(query: str) -> list[str]:
    """Основы слов от двух букв: «авторизация» → «авториза», короткие (uv, CI, QA) — как есть."""
    words = re.findall(r"\w+", query.casefold())
    return list(dict.fromkeys(word[:-2] if len(word) > 6 else word for word in words if len(word) >= 2))


def chunks(text: str, fallback_title: str) -> list[tuple[str, int, str]]:
    """(заголовок раздела, номер первой строки, текст): по заголовкам 1–3 уровня, длинные — по абзацам."""
    found: list[tuple[str, int, str]] = []
    headings: list[str] = []
    lines: list[str] = []
    size, start, in_code = 0, 1, False

    def flush() -> None:
        nonlocal lines, size
        body = "\n".join(lines).strip()
        if body:
            found.append((" › ".join(headings) or fallback_title, start, body))
        lines, size = [], 0

    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            in_code = not in_code
        heading = None if in_code else HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            headings = [*headings[:level - 1], heading.group(2)]
            continue
        # Пустая строка вне кода — граница абзаца; без неё режем только вдвое длинный раздел.
        if size >= CHUNK_CHARS and ((not line.strip() and not in_code) or size >= 2 * CHUNK_CHARS):
            flush()
        if not lines:
            if not line.strip():
                continue
            start = number
        lines.append(line)
        size += len(line) + 1
    flush()
    return found


class MarkdownSearch:
    def __init__(self, root: Path, ignored_dirs: set[str] | None = None) -> None:
        self.root = root
        self._ignored = ignored_dirs or set()

    def search(self, query: str, limit: int = 6) -> list[tuple[str, str, int, str]]:
        """Разделы по ключевым словам, лучшие первыми; пустой список, если в запросе нет слов."""
        words = terms(query)
        if not words or not self.root.is_dir():
            return []
        scored = []
        for path in self._files():
            relative = path.relative_to(self.root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            for title, line, body in chunks(text, path.stem):
                head, lowered = f"{relative} {title}".casefold(), body.casefold()
                score = sum(2 * (word in head) + (word in lowered) for word in words)
                if score:
                    scored.append((score, (relative, title, line, body)))
        # sorted устойчива: при равном счёте — порядок файлов и разделов.
        scored.sort(key=lambda item: -item[0])
        return [row for _, row in scored[:limit]]

    def _files(self) -> list[Path]:
        found = []
        for directory, dirs, files in os.walk(self.root):
            dirs[:] = sorted(name for name in dirs if name not in self._ignored and not name.startswith("."))
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix.lower() in DOC_SUFFIXES and path.stat().st_size <= MAX_FILE_BYTES:
                    found.append(path)
        return found
