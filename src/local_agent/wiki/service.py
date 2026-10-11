"""Вики — заметки агента в Markdown по паттерну LLM Wiki: ingest (добавить), index (index.md), lint (проверить).

Отдельная папка и отдельный индекс: вики не смешивается с памятью о пользователе,
и, в отличие от памяти, доступна саб-агентам (только чтение).

Добавление заметки: ключевые слова нового текста сравниваются со строками index.md. Совпали
два слова и больше — текст дополняет найденную заметку, и она пересобирается целиком; иначе
создаётся новая. Запись заметки и её строки в index.md — одна операция. Прежняя версия
перезаписанной или удалённой заметки остаётся в папке истории рядом с вики.
"""

import posixpath
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from local_agent.tools.docs import MarkdownIndex
from local_agent.wiki import notes
from local_agent.wiki.notes import INDEX_FILE, MATCH_THRESHOLD, IndexEntry, Note

PAGE_PATH = re.compile(r"^[^\W_][\w-]*(/[^\W_][\w-]*)*\.md$")
MAX_PAGE_CHARS = 60_000
MIN_KEYWORDS = 2
# Описание попадает в строку index.md: оно должно быть короче абзаца.
MAX_DESCRIPTION_CHARS = 160


@dataclass(frozen=True)
class WikiPage:
    path: str
    title: str
    updated_at: datetime
    size: int


@dataclass(frozen=True)
class Match:
    entry: IndexEntry
    shared: tuple[str, ...]


@dataclass(frozen=True)
class NoteFields:
    """Заметка целиком, как её собрал агент: из этого рендерится файл по шаблону."""

    title: str
    description: str
    ideas: tuple[str, ...]
    keywords: tuple[str, ...]
    type: str = "note"
    tags: tuple[str, ...] = ()
    source: str = ""
    details: str = ""
    related: tuple[str, ...] = ()


class WikiService:
    def __init__(self, root: Path, index_path: Path, history_path: Path | None = None) -> None:
        self.root = root
        self.history = history_path or root.parent / f"{root.name}-history"
        self._index = MarkdownIndex(root, index_path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- чтение ----------

    def pages(self) -> list[WikiPage]:
        self.initialize()
        result = []
        for relative, path in self._files():
            stat = path.stat()
            result.append(WikiPage(relative, _title(path), datetime.fromtimestamp(stat.st_mtime, UTC), stat.st_size))
        return sorted(result, key=lambda page: page.updated_at, reverse=True)

    def read(self, path: str) -> str:
        if path == INDEX_FILE:
            with self._lock:
                self._entries()
            return (self.root / INDEX_FILE).read_text(encoding="utf-8")
        return self._path(path).read_text(encoding="utf-8")

    def note(self, path: str) -> Note:
        return notes.parse(path, self.read(path))

    def notes(self) -> list[Note]:
        result = []
        for relative, path in self._files():
            try:
                result.append(notes.parse(relative, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError):
                continue
        return result

    def index(self) -> list[IndexEntry]:
        with self._lock:
            return self._entries()

    def search(self, query: str, limit: int = 5) -> list[tuple[str, str, int, str]]:
        self.initialize()
        rows = self._index.search(query, limit=limit + 3)
        return [row for row in rows if row[0] != INDEX_FILE][:limit]

    def match(
        self, keywords: list[str] | tuple[str, ...], exclude: tuple[str, ...] = (), at_least: int = MATCH_THRESHOLD,
    ) -> list[Match]:
        """Заметки из index.md, с которыми совпало не меньше at_least ключевых слов, — лучшие первыми."""
        mine = notes.clean_keywords(keywords)
        with self._lock:
            entries = self._entries()
        found = [
            Match(entry, tuple(notes.shared_keywords(mine, entry.keywords)))
            for entry in entries if entry.path not in exclude
        ]
        found = [item for item in found if len(item.shared) >= at_least]
        return sorted(found, key=lambda item: -len(item.shared))

    def similar_pairs(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """Пары заметок с общими ключевыми словами — кандидаты в дубли и противоречия для lint."""
        entries = self.index()
        pairs = []
        for index, left in enumerate(entries):
            for right in entries[index + 1:]:
                shared = tuple(notes.shared_keywords(left.keywords, right.keywords))
                if len(shared) >= MATCH_THRESHOLD:
                    pairs.append((left.path, right.path, shared))
        return sorted(pairs, key=lambda pair: -len(pair[2]))

    def graph(self) -> dict:
        """Точки — заметки, линии — ссылки между ними; размер точки — число связей, цвет — кластер."""
        items = self.notes()
        known = {note.path for note in items}
        edges: dict[tuple[str, str], None] = {}
        for note in items:
            for target in note.links:
                if target in known and target != note.path:
                    edges[tuple(sorted((note.path, target)))] = None
        degree = {note.path: 0 for note in items}
        for left, right in edges:
            degree[left] += 1
            degree[right] += 1
        cluster = notes.clusters([note.path for note in items], list(edges))
        return {
            "nodes": [
                {"path": note.path, "title": note.title, "type": note.type, "tags": list(note.tags),
                 "degree": degree[note.path], "cluster": cluster[note.path]}
                for note in items
            ],
            "edges": [{"from": left, "to": right} for left, right in edges],
        }

    # ---------- запись ----------

    def write(self, path: str, content: str) -> str:
        """Ручная правка из интерфейса: файл как есть, строка индекса — по его фронтматтеру и ключевым словам."""
        target = self._path(path, must_exist=False)
        _check_size(content)
        with self._lock:
            existed = target.exists()
            self._backup(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content.strip() + "\n", encoding="utf-8", newline="\n")
            self._upsert(notes.parse(path, content))
        return f"{'Обновлена' if existed else 'Создана'} страница вики {path}"

    def delete(self, path: str) -> None:
        target = self._path(path)
        with self._lock:
            self._backup(path)
            target.unlink()
            self._save_index([entry for entry in self._entries() if entry.path != path])

    def save_note(self, fields: NoteFields, path: str | None = None, replaces: tuple[str, ...] = ()) -> str:
        """Создаёт заметку или пересобирает существующую (path) целиком; replaces — слитые в неё заметки.

        Имя файла — из заголовка. Если заголовок изменился, файл переименовывается, а ссылки на него
        и на слитые заметки в остальной вики переписываются.
        """
        title = " ".join(fields.title.split())
        keywords = notes.clean_keywords(fields.keywords)
        ideas = _ideas(fields.ideas)
        if not title or not fields.description.strip():
            raise ValueError("Нужны заголовок и описание в одну строку")
        # Модель иногда кладёт в описание весь конспект: в описании остаётся одна фраза,
        # а сам текст уходит в «Подробности» — ничего из присланного не теряется.
        description, overflow = _short(fields.description)
        details = "\n\n".join(part for part in (overflow, fields.details.strip()) if part)
        if not ideas:
            raise ValueError("Нужна хотя бы одна ключевая мысль")
        if len(keywords) < MIN_KEYWORDS:
            raise ValueError(f"Нужно не меньше {MIN_KEYWORDS} ключевых слов: по ним заметку находят в index.md")
        with self._lock:
            entries = self._entries()
            existing = {entry.path for entry in entries}
            if path is not None:
                self._path(path)
            replaces = tuple(dict.fromkeys(item for item in replaces if item != path))
            for item in replaces:
                self._path(item)
            folder = posixpath.dirname(path) if path else ""
            target = posixpath.join(folder, notes.slug(title)) if folder else notes.slug(title)
            if target == INDEX_FILE:
                raise ValueError("index.md — служебное имя: выберите другой заголовок")
            if target != path and target not in replaces and (self.root / target).exists():
                raise ValueError(
                    f"Заметка {target} уже есть: дополни её — прочитай через wiki_read и сохрани с path={target}"
                )
            if path is None and (found := self.match(keywords, exclude=replaces)):
                best = found[0]
                raise ValueError(
                    f"С заметкой {best.entry.path} совпали ключевые слова: {', '.join(best.shared)}. "
                    f"Новый текст должен дополнить её: прочитай её через wiki_read, собери заметку заново "
                    f"целиком (старое + новое) и сохрани с path={best.entry.path}"
                )
            moves = {old: target for old in (path, *replaces) if old and old != target}
            related, unknown = [], []
            for item in dict.fromkeys(fields.related):
                item = moves.get(item, item)
                if item == target:
                    continue
                if item in existing:
                    related.append((self._title_of(item), notes.relative_link(target, item)))
                else:
                    unknown.append(item)
            content = notes.render(
                type=" ".join(fields.type.split()) or "note", title=title,
                description=description, tags=notes.clean_keywords(fields.tags),
                source=" ".join(fields.source.split()) or f"текст из чата, {datetime.now(UTC):%Y-%m-%d}",
                ideas=list(ideas), details=details, related=related, keywords=keywords,
            )
            _check_size(content)
            for old in (path, *replaces):
                if old:
                    self._backup(old)
            if target in replaces:
                self._backup(target)
            (self.root / target).parent.mkdir(parents=True, exist_ok=True)
            (self.root / target).write_text(content, encoding="utf-8", newline="\n")
            for old in moves:
                (self.root / old).unlink(missing_ok=True)
            if moves:
                self._retarget(moves, skip=target)
            # Строка индекса остаётся на месте прежней заметки, новая — в конец.
            entry = notes.entry_of(notes.parse(target, content))
            kept: list[IndexEntry] = []
            for item in entries:
                if item.path in (path, target):
                    if entry not in kept:
                        kept.append(entry)
                elif item.path not in moves:
                    kept.append(item)
            if entry not in kept:
                kept.append(entry)
            self._save_index(kept)
        verb = "Обновлена" if path else "Создана"
        parts = [f"{verb} заметка {target}, строка в index.md обновлена"]
        if path and target != path:
            parts.append(f"файл переименован из {path}")
        if replaces:
            parts.append(f"в неё слиты и удалены: {', '.join(replaces)}")
        if unknown:
            parts.append(f"ссылки на несуществующие заметки пропущены: {', '.join(unknown)}")
        return "; ".join(parts) + "."

    # ---------- внутреннее ----------

    def _files(self) -> list[tuple[str, Path]]:
        result = []
        for path in sorted(self.root.rglob("*.md")):
            relative = path.relative_to(self.root).as_posix()
            if relative != INDEX_FILE and PAGE_PATH.fullmatch(relative):
                result.append((relative, path))
        return result

    def _entries(self) -> list[IndexEntry]:
        """Строки index.md, сверенные с файлами: новые заметки дописываются, строки удалённых — убираются."""
        self.initialize()
        index = self.root / INDEX_FILE
        entries = notes.parse_index(index.read_text(encoding="utf-8")) if index.exists() else []
        files = dict(self._files())
        kept = [entry for entry in dict((entry.path, entry) for entry in entries).values() if entry.path in files]
        listed = {entry.path for entry in kept}
        for relative, path in files.items():
            if relative not in listed:
                try:
                    kept.append(notes.entry_of(notes.parse(relative, path.read_text(encoding="utf-8"))))
                except (OSError, UnicodeError):
                    continue
        if kept != entries or not index.exists():
            self._save_index(kept)
        return kept

    def _upsert(self, note: Note) -> None:
        entry = notes.entry_of(note)
        entries = self._entries()
        replaced = [entry if item.path == note.path else item for item in entries]
        self._save_index(replaced if entry in replaced else [*replaced, entry])

    def _save_index(self, entries: list[IndexEntry]) -> None:
        (self.root / INDEX_FILE).write_text(notes.render_index(entries), encoding="utf-8", newline="\n")

    def _retarget(self, moves: dict[str, str], skip: str) -> None:
        for relative, path in self._files():
            if relative == skip:
                continue
            text = path.read_text(encoding="utf-8")
            updated = notes.retarget_links(relative, text, moves)
            if updated != text:
                path.write_text(updated, encoding="utf-8", newline="\n")

    def _backup(self, path: str) -> None:
        source = self.root / path
        if not source.is_file():
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        target = self.history / f"{path[:-3]}.{stamp}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    def _title_of(self, path: str) -> str:
        return _title(self.root / path)

    def _path(self, path: str, *, must_exist: bool = True) -> Path:
        if path == INDEX_FILE:
            raise ValueError("index.md обновляется автоматически: правьте сами заметки")
        if not isinstance(path, str) or not PAGE_PATH.fullmatch(path):
            raise ValueError("Путь заметки — буквы, цифры, - и _, с .md на конце, например rag_основы.md")
        target = self.root / path
        if must_exist and not target.is_file():
            raise FileNotFoundError(f"Заметки {path} нет")
        return target


# Буквальные «\n» в строке — модель экранировала перевод строки дважды.
LITERAL_NEWLINE = re.compile(r"\\n")
LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _short(text: str) -> tuple[str, str]:
    """Описание для index.md и остаток. Короткое описание в одну строку — как есть, остатка нет.

    Абзац или целый конспект: описание — первая фраза первой строки, которая не заголовок,
    а остаток — весь исходный текст (уйдёт в «Подробности»).
    """
    raw = LITERAL_NEWLINE.sub("\n", text).strip()
    flat = " ".join(raw.split())
    if len(flat) <= MAX_DESCRIPTION_CHARS and "\n" not in raw:
        return flat, ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    plain = [line for line in lines if not line.startswith("#")] or [line.lstrip("#").strip() for line in lines]
    first = re.sub(r"[*_`]", "", LIST_MARKER.sub("", plain[0])) if plain else flat
    sentence = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
    if len(sentence) > MAX_DESCRIPTION_CHARS:
        sentence = sentence[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(",;:—-– ") + "…"
    # Одна длинная строка без структуры — просто сокращённое описание, переносить нечего.
    rest = raw if "\n" in raw or len(flat) > 2 * MAX_DESCRIPTION_CHARS else ""
    return sentence, rest


def _ideas(items: tuple[str, ...]) -> tuple[str, ...]:
    """Мысли по одной на пункт: пункт с переводами строк (настоящими или буквальными «\n») делится на несколько."""
    result = []
    for item in items:
        for line in LITERAL_NEWLINE.sub("\n", str(item)).splitlines():
            idea = " ".join(LIST_MARKER.sub("", line).split())
            if idea:
                result.append(idea)
    return tuple(result)


def _check_size(content: str) -> None:
    if not content.strip():
        raise ValueError("Пустая страница")
    if len(content) > MAX_PAGE_CHARS:
        raise ValueError(f"Страница длиннее {MAX_PAGE_CHARS} символов: разбейте её на несколько")


def _title(path: Path) -> str:
    try:
        return notes.parse(path.name, path.read_text(encoding="utf-8")).title
    except (OSError, UnicodeError):
        return path.stem
