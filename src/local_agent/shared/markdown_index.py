"""Полнотекстовый индекс Markdown-файлов папки (SQLite FTS5): документация проекта, вики, долговременная память.

Общий механизм поиска: им пользуются инструмент search_docs, вики и память.
"""

import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from local_agent.shared.folders import MAX_SEARCH_FILE_BYTES, MAX_SEARCH_FILES, SKIPPED_DIRS

DOC_SUFFIXES = {".md", ".markdown"}
CHUNK_CHARS = 1500
DEFAULT_LIMIT = 6
# Меняется вместе со схемой или правилами нарезки: старый индекс тогда строится заново.
SCHEMA_VERSION = 1
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


class MarkdownIndex:
    """Полнотекстовый индекс Markdown-файлов папки: документация проекта, вики, долговременная память.

    Индекс лежит вне индексируемой папки и обновляется при каждом поиске по изменённым файлам.
    chunker режет текст файла на (заголовок, строка, текст): по умолчанию — разделы Markdown.
    """

    def __init__(self, root: Path, db_path: Path, chunker=None) -> None:
        self.root = root
        self.db_path = db_path
        self._chunker = chunker or chunks

    def search(self, query: str, limit: int = DEFAULT_LIMIT, prefix: str = "") -> list[tuple[str, str, int, str]]:
        """Разделы по ключевым словам; пустой список, если в запросе нет слов для поиска."""
        try:
            stems, short = terms(query)
        except ValueError:
            return []
        return self.query(stems, short, prefix, limit)[0]

    def query(self, stems: list[str], short: list[str], prefix: str, limit: int) -> tuple[list, bool]:
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as db:
            skipped = sync(db, self.root.resolve(), self._chunker)
            return search_index(db, stems, short, prefix, limit), skipped

    def _open(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=30)
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            with db:
                db.execute("DROP TABLE IF EXISTS files")
                db.execute("DROP TABLE IF EXISTS chunks")
                db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER)")
                # trigram ищет подстроки: русский текст находится по основе слова без стеммера.
                db.execute(
                    "CREATE VIRTUAL TABLE chunks USING fts5(path, title, text, line UNINDEXED, tokenize='trigram')"
                )
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return db


def sync(db: sqlite3.Connection, root: Path, chunker=None) -> bool:
    """Переиндексирует изменённые файлы и забывает удалённые. True — файлов больше лимита, часть пропущена."""
    found: dict[str, tuple[int, int]] = {}
    skipped = False
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS)
        for file_name in sorted(files):
            path = Path(directory) / file_name
            if path.suffix.lower() not in DOC_SUFFIXES:
                continue
            if len(found) >= MAX_SEARCH_FILES:
                skipped = True
                break
            try:
                if not path.resolve(strict=True).is_relative_to(root):
                    continue
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size <= MAX_SEARCH_FILE_BYTES:
                found[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    known = {path: (mtime, size) for path, mtime, size in db.execute("SELECT path, mtime_ns, size FROM files")}
    with db:
        for path in known.keys() - found.keys():
            db.execute("DELETE FROM chunks WHERE path = ?", (path,))
            db.execute("DELETE FROM files WHERE path = ?", (path,))
        for path, state in found.items():
            if known.get(path) == state:
                continue
            db.execute("DELETE FROM chunks WHERE path = ?", (path,))
            try:
                text = (root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                text = ""
            db.executemany(
                "INSERT INTO chunks (path, title, text, line) VALUES (?, ?, ?, ?)",
                [(path, title, body, line) for title, line, body in (chunker or chunks)(text, Path(path).stem)],
            )
            db.execute("INSERT OR REPLACE INTO files (path, mtime_ns, size) VALUES (?, ?, ?)", (path, *state))
    return skipped


def chunks(text: str, fallback_title: str) -> list[tuple[str, int, str]]:
    """Режет Markdown по заголовкам 1–3 уровня, а длинные разделы — по границам абзацев."""
    chunks: list[tuple[str, int, str]] = []
    headings: list[str] = []
    lines: list[str] = []
    size = 0
    start = 1
    in_code = False

    def flush() -> None:
        nonlocal lines, size
        body = "\n".join(lines).strip()
        if body:
            chunks.append((" › ".join(headings) or fallback_title, start, body))
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
            # Номер строки указывает на первый текст раздела, а не на пустую строку после заголовка.
            if not line.strip():
                continue
            start = number
        lines.append(line)
        size += len(line) + 1
    flush()
    return chunks


def terms(query: str) -> tuple[list[str], list[str]]:
    """Основы слов от трёх букв для индекса и двухбуквенные слова (uv, CI, QA): trigram их не видит."""
    words = re.findall(r"\w+", query.casefold())
    # Обрезанная основа находит другие формы слова: «авторизация» → «авториза» найдёт «авторизации».
    stems = list(dict.fromkeys(word[:-2] if len(word) > 6 else word for word in words if len(word) >= 3))
    short = list(dict.fromkeys(word for word in words if len(word) == 2))
    if not stems and not short:
        raise ValueError("Нужно хотя бы одно слово от двух букв")
    return stems, short


def search_index(
    db: sqlite3.Connection, stems: list[str], short: list[str], prefix: str, limit: int,
) -> list[tuple[str, str, int, str]]:
    in_folder, folder_params = "", []
    if prefix:
        in_folder, folder_params = " AND path LIKE ? ESCAPE '\\'", [f"{like_escape(prefix)}/%"]
    found: dict[tuple[str, int], tuple[str, str, int, str]] = {}
    if stems:
        # В кавычках слово — строка поиска, а не синтаксис FTS5; \w кавычек не содержит.
        # Совпадение в заголовке раздела весит впятеро больше, чем в тексте.
        rows = db.execute(
            "SELECT path, title, line, text FROM chunks WHERE chunks MATCH ?" + in_folder
            + " ORDER BY bm25(chunks, 1.0, 5.0, 1.0, 0.0) LIMIT ?",
            [" OR ".join(f'"{stem}"' for stem in stems), *folder_params, limit * 5],
        )
        found.update(((row[0], row[2]), row) for row in rows)
    if short:
        # MATCH нельзя объединить с LIKE через OR, поэтому короткие слова — отдельным запросом.
        like = "LIKE ? ESCAPE '\\'"
        conditions = " OR ".join(f"path {like} OR title {like} OR text {like}" for _ in short)
        params = [f"%{like_escape(word)}%" for word in short for _ in range(3)]
        rows = db.execute(
            f"SELECT path, title, line, text FROM chunks WHERE ({conditions})" + in_folder + " LIMIT ?",
            [*params, *folder_params, limit * 5],
        )
        for row in rows:
            found.setdefault((row[0], row[2]), row)
    terms = [*stems, *short]

    def score(row: tuple[str, str, int, str]) -> int:
        # Больше найденных слов запроса — выше; слово в имени файла или заголовке весит вдвое.
        head, body = f"{row[0]} {row[1]}".casefold(), row[3].casefold()
        return sum(2 * (term in head) + (term in body) for term in terms)

    # sorted устойчива: при равном счёте сохраняется порядок bm25.
    return sorted(found.values(), key=score, reverse=True)[:limit]


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
