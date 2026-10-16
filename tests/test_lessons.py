"""Уроки: агент по просьбе пользователя дописывает правило в свой промпт — только после подтверждения."""

from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of
from tests.test_graph_flow import settings

from local_agent.api.app import create_app
from local_agent.capabilities.lessons import LESSONS_HEADER, lessons_of, with_lesson

RULE = "Тест-кейсы оформляй таблицей и всегда с предусловиями"


def test_lessons_section_grows_at_the_end_of_prompt():
    prompt = with_lesson("Ты тестировщик.", "Первое")
    assert prompt == f"Ты тестировщик.\n\n{LESSONS_HEADER}\n- Первое"
    prompt = with_lesson(prompt, "Второе")
    assert lessons_of(prompt) == ["Первое", "Второе"] and prompt.startswith("Ты тестировщик.")


def test_agent_learns_rule_after_user_approves(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "qa.md").write_text(
        "---\nname: Тестировщик\ndescription: Пишет тест-кейсы\ntools: read_file\n---\nТы тестировщик.\n", encoding="utf-8",
    )
    provider = ScriptedProvider(call("learn", rule=f"  {RULE} "), "Запомнил", call("learn", rule=RULE), "Уже знаю")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        chat = client.post("/api/agents/qa/chat", json={"provider": "lm_studio", "model": "test"}).json()
        url = f"/api/sessions/{chat['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Запомни: таблицей и с предусловиями"}))[-1]
        # До подтверждения промпт не меняется.
        assert LESSONS_HEADER not in (tmp_path / "agents" / "qa.md").read_text(encoding="utf-8")
        events_of(client.post(url + "/resume", json={"action": "approve"}))
        again = events_of(client.post(url + "/turns/stream", json={"content": "Запомни ещё раз"}))

    assert card["type"] == "interrupt" and card["pending"]["title"] == "Запомнить урок агенту «Тестировщик»"
    assert card["pending"]["preview"] == f"{LESSONS_HEADER}\n- {RULE}"
    text = (tmp_path / "agents" / "qa.md").read_text(encoding="utf-8")
    assert text.rstrip().endswith(f"{LESSONS_HEADER}\n- {RULE}") and "tools: read_file" in text
    # Повтор того же правила карточку не показывает: модель сразу узнаёт, что оно уже есть.
    assert not any(event["type"] == "interrupt" for event in again)
    assert "уже есть" in provider.requests[-1]["messages"][-1].content
