"""Перенос данных: вики без index.sqlite, память по разделам, подтверждённый QA-опыт; повторный запуск ничего не дублирует."""

import sqlite3

from meepo.memory.store import MemoryStore
from meepo.migrate import migrate_experience, migrate_memory, migrate_wiki


def old_data(tmp_path):
    old = tmp_path / "old"
    (old / "wiki" / "images").mkdir(parents=True)
    (old / "wiki" / "rag.md").write_text("# RAG\n", encoding="utf-8")
    (old / "wiki" / "images" / "a.png").write_bytes(b"png")
    (old / "wiki" / "index.sqlite").write_bytes(b"sqlite")
    (old / "memory").mkdir()
    (old / "memory" / "profile.md").write_text("# Профиль\n\n## Обращение\n\n- Зовут Павел\n", encoding="utf-8")
    (old / "memory" / "decisions.md").write_text("# Решения\n\n## Стек\n\n- Автотесты на Playwright\n", encoding="utf-8")
    db = sqlite3.connect(old / "memory" / "qa-experience.sqlite")
    db.execute("CREATE TABLE experience (id TEXT, project TEXT, task TEXT, actions TEXT, result TEXT, status TEXT, date TEXT)")
    db.execute("INSERT INTO experience VALUES ('1', 'Портал', 'Флаки-тест входа', 'ждать ответа /login', 'стабилен', 'confirmed', '2026-10-01')")
    db.execute("INSERT INTO experience VALUES ('2', 'Портал', 'Догадка', 'что-то', '?', 'observation', '2026-10-02')")
    db.commit()
    db.close()
    return old


def test_migrate_is_idempotent(tmp_path):
    old = old_data(tmp_path)
    data = tmp_path / "data"
    store = MemoryStore(data / "memory")
    for _ in range(2):
        migrate_wiki(old / "wiki", data / "wiki")
        migrate_memory(old / "memory", store)
        migrate_experience(old / "memory" / "qa-experience.sqlite", store)
    assert (data / "wiki" / "rag.md").is_file() and (data / "wiki" / "images" / "a.png").is_file()
    assert not (data / "wiki" / "index.sqlite").exists()
    assert store.read("profile.md").count("- Зовут Павел") == 1
    assert "## Стек\n\n- Автотесты на Playwright" in store.read("decisions.md")
    solutions = store.read("solutions.md")
    assert solutions.count("- Флаки-тест входа → ждать ответа /login → стабилен") == 1 and "Догадка" not in solutions
    # Старые данные не тронуты.
    assert (old / "wiki" / "index.sqlite").read_bytes() == b"sqlite"
