"""Вики — заметки агента в Markdown: статьи и ссылки, которые пользователь попросил разобрать.

Отдельная папка и отдельный индекс: вики не смешивается с памятью о пользователе,
и, в отличие от памяти, доступна саб-агентам.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from local_agent.tools.docs import MarkdownIndex

PAGE_PATH = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*\.md$")
MAX_PAGE_CHARS = 60_000


@dataclass(frozen=True)
class WikiPage:
    path: str
    title: str
    updated_at: datetime
    size: int


class WikiService:
    def __init__(self, root: Path, index_path: Path) -> None:
        self.root = root
        self._index = MarkdownIndex(root, index_path)

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def pages(self) -> list[WikiPage]:
        self.initialize()
        result = []
        for path in sorted(self.root.rglob("*.md")):
            relative = path.relative_to(self.root).as_posix()
            if not PAGE_PATH.fullmatch(relative):
                continue
            stat = path.stat()
            result.append(WikiPage(relative, _title(path), datetime.fromtimestamp(stat.st_mtime, UTC), stat.st_size))
        return sorted(result, key=lambda page: page.updated_at, reverse=True)

    def read(self, path: str) -> str:
        return self._path(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> str:
        target = self._path(path, must_exist=False)
        if not content.strip():
            raise ValueError("Пустая страница")
        if len(content) > MAX_PAGE_CHARS:
            raise ValueError(f"Страница длиннее {MAX_PAGE_CHARS} символов: разбейте её на несколько")
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content.strip() + "\n", encoding="utf-8", newline="\n")
        return f"{'Обновлена' if existed else 'Создана'} страница вики {path}"

    def delete(self, path: str) -> None:
        self._path(path).unlink()

    def search(self, query: str, limit: int = 5) -> list[tuple[str, str, int, str]]:
        self.initialize()
        return self._index.search(query, limit=limit)

    def _path(self, path: str, *, must_exist: bool = True) -> Path:
        if not isinstance(path, str) or not PAGE_PATH.fullmatch(path):
            raise ValueError("Путь страницы — латиница в нижнем регистре, цифры, - и _, с .md на конце, например ai/rag.md")
        target = self.root / path
        if must_exist and not target.is_file():
            raise FileNotFoundError(f"Страницы {path} нет")
        return target


def _title(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if line.startswith("# "):
                    return line[2:].strip()
    except (OSError, UnicodeError):
        pass
    return path.stem
