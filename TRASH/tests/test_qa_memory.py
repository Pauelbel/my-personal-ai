"""QA-опыт отделён по проектам и безопасно попадает в контекст."""

import asyncio

from memory.markdown import MarkdownMemoryStore
from memory.qa_experience import QAExperienceStore
from memory.service import MemoryService
from sessions.models import Session
from tools.memory import QAMemorySearchTool


def add(store: QAExperienceStore, project: str, status: str, *, evidence: str = ""):
    return store.save(
        project_id=project, project=project, task="Проверка POST /orders", source="Jira SHOP-412",
        status=status, applies_to="API заказов", actions="Использовали fixture verified_user",
        result="POST /orders вернул 201", evidence=evidence,
    )


def test_confirmed_requires_evidence_and_search_is_project_scoped(tmp_path):
    store = QAExperienceStore(tmp_path / "experience.sqlite")
    try:
        add(store, "shop", "confirmed")
    except ValueError as exc:
        assert "доказательство" in str(exc)
    else:
        raise AssertionError("Неподтверждённое правило сохранено как confirmed")

    confirmed = add(store, "shop", "confirmed", evidence="pytest: 8 passed")
    add(store, "other", "confirmed", evidence="pytest: passed")
    found = store.search("shop", "orders fixture", confirmed_only=True)
    assert [item.id for item in found] == [confirmed.id]


def test_observation_is_not_in_automatic_context_but_explicit_search_marks_it(tmp_path):
    memory_store = MarkdownMemoryStore(tmp_path / "memory")
    memory_store.initialize()
    memory = MemoryService(memory_store, None, None, memory_model="", max_context_chars=4000)
    memory.qa.save(
        project_id="shop", project="shop", task="Нестабильный локатор кнопки", source="browser run",
        status="observation", applies_to="checkout UI", actions="Повторили клик",
        result="Локатор иногда не найден",
    )
    add(memory.qa, "shop", "confirmed", evidence="tests/api/test_orders.py: 8 passed")

    context = memory.prompt_context("orders fixture", "shop")
    assert "tests/api/test_orders.py" in context
    assert "НЕ ПОДТВЕРЖДЕНО" not in context

    session = Session(
        id="s", title="x", created_at="2026-10-18T00:00:00Z", updated_at="2026-10-18T00:00:00Z",
        model="test", provider="test", project_id="shop", workspace=str(tmp_path / "shop"),
    )
    result = asyncio.run(QAMemorySearchTool(memory, session).execute({"query": "orders fixture локатор checkout"}, None))
    assert "НЕ ПОДТВЕРЖДЕНО" in result.content and result.content.count("- [") == 2


def test_search_returns_at_most_five_records(tmp_path):
    store = QAExperienceStore(tmp_path / "experience.sqlite")
    for number in range(8):
        store.save(
            project_id="shop", project="shop", task=f"Проверка orders {number}", source=f"test_{number}.py",
            status="confirmed", applies_to="API orders", actions="Запустили тест", result="passed",
            evidence=f"pytest test_{number}.py: passed",
        )
    assert len(store.search("shop", "orders", limit=100, confirmed_only=True)) == 5


def test_repeated_experience_updates_instead_of_accumulating(tmp_path):
    store = QAExperienceStore(tmp_path / "experience.sqlite")
    first = add(store, "shop", "observation")
    confirmed = add(store, "shop", "confirmed", evidence="pytest: passed")
    assert confirmed.id == first.id
    assert len(store.search("shop", "orders")) == 1
