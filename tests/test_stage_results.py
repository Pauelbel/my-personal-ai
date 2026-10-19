"""Итог этапа плана выбирает сервер, когда модель отмечает этап сделанным; чат показывает именно его.

Сценарии — из настоящих диалогов, где чек-лист пропадал с экрана: текст этапа в одном ответе с отметкой,
короткая отметка после длинного результата, комментарии к шагам между ними.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session, say
from tests.test_graph_flow import settings

from api.app import create_app
from core.plan import stage_messages, stage_result
from memory.models import Message, StoredToolCall

CHECKLIST = "### Чек-лист\n" + "\n".join(f"- [P1] Проверка {index}: вход и каталог" for index in range(12))
BUGS = "### Найденные дефекты\n" + "\n".join(f"{index}. У problem_user не та картинка товара {index}" for index in range(1, 6))


def message(index, role, content="", calls=()):
    return Message(
        id=f"m{index}", session_id="s", role=role, content=content, created_at=datetime.now(UTC),
        tool_calls=[StoredToolCall(id=f"c{index}-{n}", name=name, arguments="{}") for n, name in enumerate(calls)],
    )


def test_result_is_the_text_written_with_the_stage_mark():
    history = [
        message(1, "user", "Тестовое задание"), message(2, "assistant", "", ["plan"]), message(3, "tool", "план"),
        message(4, "assistant", "Приступаю к кейсам.", ["delegate_agent"]), message(5, "tool", "результат агента"),
        message(6, "assistant", CHECKLIST, ["plan"]),
    ]
    assert stage_result(stage_messages(history)).id == "m6"


def test_short_mark_after_long_result_picks_the_result_not_the_narration():
    history = [
        message(1, "assistant", "", ["plan"]), message(2, "tool", "план"),
        message(3, "assistant", "Приступаю к тестированию под problem_user.", ["browser_click"]), message(4, "tool", "ok"),
        message(5, "assistant", BUGS, ["write_file"]), message(6, "tool", "записан"),
        message(7, "assistant", "Готово.", ["plan"]),
    ]
    assert stage_result(stage_messages(history)).id == "m5"


def test_stage_without_written_result_has_none_and_boundaries_hold():
    history = [
        message(1, "assistant", CHECKLIST, ["plan"]), message(2, "tool", "план"),
        message(3, "user", "продолжай"),
        message(4, "assistant", "Изучил сайт.", ["plan"]),
    ]
    # Чек-лист — итог прошлого этапа (до прошлой отметки и до реплики пользователя): этому этапу он не достаётся.
    assert [item.id for item in stage_messages(history)] == ["m4"]
    assert stage_result(stage_messages(history)) is None


def test_plan_done_records_the_stage_result_for_the_chat(tmp_path):
    provider = ScriptedProvider(
        call("plan", action="set", steps=["Тест-кейсы", "Баг-репорты"]),
        say(CHECKLIST, ("plan", {"action": "done", "step": 1})),
        say("Приступаю к поиску багов.", ("list_files", {"path": "."})),
        BUGS,
        call("plan", action="done", step=2),
        "Итог: два этапа готовы.",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events_of(client.post(url + "/turns/stream", json={"content": "Кейсы и баги"}))
        history = client.get(url + "/messages").json()

    by_content = {item["content"]: item["id"] for item in history if item["role"] == "assistant"}
    marks = [item["meta"] for item in history if item["role"] == "tool" and item.get("meta")]
    assert [(mark["stage"], mark["title"]) for mark in marks] == [(1, "Тест-кейсы"), (2, "Баг-репорты")]
    assert marks[0]["result"] == by_content[CHECKLIST]
    # Баги написаны отдельным ответом без вызова (модель подтолкнули продолжить), отметка — следующим: итог — баги.
    assert marks[1]["result"] == by_content[BUGS]
