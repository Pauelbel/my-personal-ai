"""Перенос данных старого Мипо: `python -m meepo.migrate [--old data-old] [--data data]`.

Запускается вручную один раз; повторный запуск ничего не дублирует, старые данные только читаются.
- Вики: заметки, картинки и история из `<old>/wiki` (или WIKI_PATH из старого .env) → `data/wiki`, без index.sqlite.
  Файл, который уже есть в новой вики, не перезаписывается.
- Память: записи `<old>/memory/*.md` → `data/memory` по разделам; запись, которая уже есть, не добавляется.
  Подтверждённый QA-опыт (`qa-experience.sqlite`, status = confirmed) → `solutions.md` строкой
  «задача → действия → результат». SQLite открывается только на чтение; после переноса он не нужен.
"""

import argparse
import shutil
import sqlite3
from pathlib import Path

from meepo.memory.store import MemoryStore, entries
from meepo.settings import ROOT, Env, read_env_file

SKIPPED = {"index.sqlite", "index.sqlite-wal", "index.sqlite-shm"}


def old_wiki(old: Path) -> Path:
    """Вики старого Мипо: WIKI_PATH из старого .env, если он указывал на отдельную папку, иначе <old>/wiki."""
    configured = read_env_file(ROOT / ".env").get("WIKI_PATH", "")
    if configured and Path(configured).is_absolute() and Path(configured).is_dir():
        return Path(configured)
    return old / "wiki"


def migrate_wiki(source: Path, target: Path) -> int:
    copied = 0
    if not source.is_dir():
        return 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name in SKIPPED or path.name.endswith(".tmp"):
            continue
        destination = target / path.relative_to(source)
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied += 1
    return copied


def migrate_memory(source: Path, store: MemoryStore) -> int:
    store.initialize()
    added = 0
    for path in sorted(source.glob("*.md")) if source.is_dir() else []:
        text = path.read_text(encoding="utf-8")
        if path.name not in store.names():
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
            store.write(path.name, f"# {title}\n")
        for section, entry in entries(text):
            added += _add(store, path.name, section or "Разное", entry)
    return added


def migrate_experience(database: Path, store: MemoryStore) -> int:
    if not database.is_file():
        return 0
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT project, task, actions, result FROM experience WHERE status = 'confirmed' ORDER BY date").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()
    added = 0
    for project, task, actions, result in rows:
        line = " → ".join(" ".join(str(part).split()) for part in (task, actions, result) if str(part).strip())
        added += _add(store, "solutions.md", project or "QA-опыт", line)
    return added


def _add(store: MemoryStore, name: str, section: str, entry: str) -> int:
    try:
        store.add(name, section, entry)
        return 1
    except ValueError:
        # Такая запись уже есть: повторный перенос ничего не дублирует.
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Перенос вики и памяти старого Мипо")
    parser.add_argument("--old", type=Path, default=ROOT / "data-old", help="папка данных старого Мипо")
    parser.add_argument("--data", type=Path, default=None, help="папка данных нового Мипо (по умолчанию — из .env)")
    args = parser.parse_args()
    data = args.data or Env.load().data_path
    store = MemoryStore(data / "memory")
    wiki = migrate_wiki(old_wiki(args.old), data / "wiki")
    memory = migrate_memory(args.old / "memory", store)
    experience = migrate_experience(args.old / "memory" / "qa-experience.sqlite", store)
    print(f"Вики: скопировано файлов — {wiki}.\nПамять: добавлено записей — {memory}.\n"
          f"QA-опыт: перенесено в solutions.md — {experience}.")


if __name__ == "__main__":
    main()
