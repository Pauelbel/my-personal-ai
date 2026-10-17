"""Ход на графе: компактный реестр, activate, interrupt вместо подтверждения, resume после перезапуска."""

from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session

from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.llm.models import ReasoningDelta


def settings(tmp_path) -> Settings:
    return Settings(
        agents_path=tmp_path / "agents", skills_path=tmp_path / "skills", sessions_path=tmp_path / "sessions",
        projects_path=tmp_path / "projects", conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory", memory_auto_update_messages=0,
        mcp_enabled=False, _env_file=None,
    )


def project_session(client, workspace) -> dict:
    project = client.post("/api/projects", json={"name": "Проект", "workspace": str(workspace)}).json()
    return post_session(client, {"model": "test", "project_id": project["id"]}).json()


def workspace_with_note(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "note.txt").write_text("Встреча в пятницу", encoding="utf-8")
    return workspace


def test_registry_is_compact_and_activate_binds_schemas(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("activate", names=["git"]), "Готово", "Ещё раз")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Что в git?"}))
        project = client.get("/api/projects").json()
        other = post_session(client, {"model": "test", "project_id": session["project_id"]}).json()
        client.post(f"/api/sessions/{other['id']}/turns/stream", json={"content": "Снова"})

    assert events[-1]["type"] == "done"
    # Сначала схемы базовых инструментов и чтения файлов, остальные — строкой реестра.
    first = provider.tool_names(0)
    assert {"activate", "remember", "propose", "list_files", "read_file"} <= set(first) and "git" not in first
    assert "- git — " in provider.system_prompt(0) and "Скиллов пока нет" in provider.system_prompt(0)
    assert "git" in provider.tool_names(1)
    # Подключённое запоминается на проект: новая сессия начинает с него.
    assert next(item for item in project if item["id"] == session["project_id"])["active_tools"] == ["list_files", "read_file", "git"]
    assert "git" in provider.tool_names(-1)


def test_answer_keeps_turn_summary(tmp_path):
    provider = ScriptedProvider(call("activate", names=["git"]), "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Подключи git"}))
        history = client.get(url + "/messages").json()

    stats = events[-1]["message"]["stats"]
    # Два запроса к модели: вызов инструмента и ответ; токены считает только тот, о котором сервер их сообщил.
    assert stats["rounds"] == 2 and stats["input_tokens"] == 100 and stats["output_tokens"] == 5
    assert stats["seconds"] >= 0
    assert history[-1]["stats"] == stats
    assert all(message["stats"] is None for message in history[:-1])


def test_read_only_project_hides_tools_that_change_files(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("write_file", path="out.txt", content="x"), "Не могу писать")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        client.put(f"/api/projects/{session['project_id']}", json={"name": "Проект", "workspace": str(workspace), "mode": "read"})
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Запиши"}))

    assert events[-1]["type"] == "done" and not (workspace / "out.txt").exists()
    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert result["is_error"] and "Инструмента «write_file» нет" in result["content"]
    assert "только для чтения" in provider.system_prompt(0)
    assert "- write_file" not in provider.system_prompt(0)


def test_retry_does_not_duplicate_the_unanswered_message(tmp_path):
    provider = ScriptedProvider("", "", "", "Ответ")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        failed = events_of(client.post(url + "/turns/stream", json={"content": "Привет"}))
        retried = events_of(client.post(url + "/turns/stream", json={"content": "Привет", "retry": True}))
        history = client.get(url + "/messages").json()

    assert failed[-1]["type"] == "error" and retried[-1]["type"] == "done"
    assert [message["role"] for message in history] == ["user", "assistant"]


def test_write_waits_for_decision_and_survives_restart(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("write_file", path="out.txt", content="итог"), "Записал")
    configured = settings(tmp_path)
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Запиши итог"}))
        assert events[-1]["type"] == "interrupt"
        assert events[-1]["pending"]["call"]["name"] == "write_file"
        assert not (workspace / "out.txt").exists()

    # Сервер перезапустили: карточка на месте, решение продолжает тот же ход.
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        assert client.get(url).json()["pending"]["kind"] == "tool_call"
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))
        assert events[-1]["type"] == "done"
        assert client.get(url).json()["pending"] is None
        assert client.post(url + "/resume", json={"action": "approve"}).status_code == 409
        history = client.get(url + "/messages").json()

    assert (workspace / "out.txt").read_text(encoding="utf-8") == "итог"
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "assistant"]


def test_message_while_waiting_is_a_reply_to_the_card(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("write_file", path="out.txt", content="итог"), "Хорошо, сначала план")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Запиши итог"})
        events = events_of(client.post(url + "/turns/stream", json={"content": "Нет, сначала покажи план"}))
        history = client.get(url + "/messages").json()

    assert [event["type"] for event in events if event["type"] in {"tool_result", "user_message", "done"}] == [
        "tool_result", "user_message", "done",
    ]
    assert not (workspace / "out.txt").exists()
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "user", "assistant"]
    assert "ответил" in history[2]["content"] and history[3]["content"] == "Нет, сначала покажи план"
    # Модель видит и отказ, и реплику пользователя после него.
    assert [message.role for message in provider.requests[-1]["messages"][-2:]] == ["tool", "user"]


def test_missing_capability_is_reported_not_invented(tmp_path):
    provider = ScriptedProvider(call("search_jira", query="баги"), "Инструмента для Jira нет, могу предложить создать")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Найди баги"}))

    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert result["is_error"] and "propose" in result["content"]


def test_incomplete_call_is_returned_to_model_before_the_card(tmp_path):
    edit = {"kind": "fix", "notes": ["заметка.md"], "problem": "Опечатка", "change": "Исправить"}
    provider = ScriptedProvider(call("wiki_propose_edits"), call("wiki_propose_edits", edits=[edit]))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        client.app.state.wiki_service.write("заметка.md", "# Заметка\n\nТекст")
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "/lint"}))

    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert "непустой список правок" in result["content"] and "edits (array, обязательный)" in result["content"]
    # Карточку пользователь видит только для исправленного вызова.
    assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["title"].endswith("правок — 1")


def test_empty_model_answer_is_retried(tmp_path):
    provider = ScriptedProvider("", "", "Ответ со второй попытки")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Привет"}))

    assert events[-1]["type"] == "done" and events[-1]["message"]["content"] == "Ответ со второй попытки"
    # Последняя попытка — без инструментов: так модель точно ответит текстом.
    assert provider.requests[-1]["tools"] is None


def test_looping_model_is_cut_and_retried(tmp_path):
    from local_agent.llm.models import ChatResult, ReasoningDelta

    class Looping(ScriptedProvider):
        async def chat_stream(self, model, messages, tools=None, options=None):
            self.requests.append({"model": model, "messages": list(messages), "tools": tools})
            if len(self.requests) == 1:
                while True:
                    yield ReasoningDelta("I'll click Checkout.\nI'll type Ivan.\nI'll click Finish.\nI'll report the result.\n")
            yield "Ответ со второй попытки"
            yield ChatResult(content="Ответ со второй попытки", input_tokens=100, output_tokens=5)

    provider = Looping()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Оформи заказ"}))

    assert {"type": "retry"} in events
    assert events[-1]["type"] == "done" and events[-1]["message"]["content"] == "Ответ со второй попытки"
    assert len(provider.requests) == 2


def test_attention_lists_pending_cards(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(call("write_file", path="out.txt", content="итог"))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Запиши"})
        attention = client.get("/api/attention", params={"session_id": session["id"]}).json()

    [card] = attention["cards"]
    assert card["session_id"] == session["id"] and card["pending"]["call"]["name"] == "write_file"


def run_to_limit(tmp_path, *steps):
    """Лимит в 2 шага: модель зовёт инструменты дважды, и ход встаёт на карточке «продолжить или остановить»."""
    provider = ScriptedProvider(call("list_files", path="."), call("list_files", path="."), *steps)
    configured = settings(tmp_path).model_copy(update={"max_tool_rounds": 2})
    client = TestClient(create_app(configured, llm_provider=provider))
    client.__enter__()
    session = post_session(client, {"model": "test"}).json()
    events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Обойди всё"}))
    return client, provider, f"/api/sessions/{session['id']}", events


def test_step_limit_asks_to_continue_and_continue_extends_the_turn(tmp_path):
    client, provider, url, events = run_to_limit(tmp_path, call("list_files", path="."), "Готово: обошёл всё")
    try:
        card = events[-1]
        assert card["type"] == "interrupt" and card["pending"]["kind"] == "limit"
        assert card["pending"]["title"] == "Сделано 2 шага — продолжить работу?"
        # Карточка встала до запроса к модели: модель на лимите ещё не звали.
        assert len(provider.requests) == 2
        resumed = events_of(client.post(url + "/resume", json={"action": "approve"}))
    finally:
        client.__exit__(None, None, None)

    assert resumed[-1]["type"] == "done" and resumed[-1]["message"]["content"] == "Готово: обошёл всё"
    # «Продолжить» вернуло инструменты: третий шаг сделан вызовом, четвёртый запрос — итог.
    assert len(provider.requests) == 4 and provider.requests[2]["tools"]


def test_step_limit_stop_asks_for_summary_without_tools(tmp_path):
    client, provider, url, events = run_to_limit(tmp_path, "Сделано: два обхода. Осталось: остальное")
    try:
        assert events[-1]["pending"]["kind"] == "limit"
        resumed = events_of(client.post(url + "/resume", json={"action": "reject"}))
    finally:
        client.__exit__(None, None, None)

    assert resumed[-1]["type"] == "done" and resumed[-1]["message"]["content"].startswith("Сделано: два обхода")
    last = provider.requests[-1]
    assert not last["tools"] and last["messages"][-1].role == "user" and "остановил работу" in last["messages"][-1].content


def test_promised_step_without_tool_call_is_continued_in_the_same_turn(tmp_path):
    workspace = workspace_with_note(tmp_path)
    provider = ScriptedProvider(
        call("list_files", path="."), "Шаг 2.\n\nПерехожу к чтению заметки.", call("read_file", path="note.txt"),
        "Заметка: встреча в пятницу.",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Что в заметке?"}))
        history = client.get(url + "/messages").json()

    # Обещание осталось в чате, а модель в том же ходе получила просьбу сделать шаг и сделала его.
    assert events[-1]["type"] == "done" and events[-1]["message"]["content"] == "Заметка: встреча в пятницу."
    assert provider.requests[2]["messages"][-1].role == "user" and "не вызвал инструмент" in provider.requests[2]["messages"][-1].content
    assert [message["content"] for message in history if message["role"] == "assistant" and not message.get("tool_calls")] == [
        "Шаг 2.\n\nПерехожу к чтению заметки.", "Заметка: встреча в пятницу.",
    ]
    # Подсказка модели — не реплика пользователя: в историю она не попадает.
    assert [message["content"] for message in history if message["role"] == "user"] == ["Что в заметке?"]


def test_promises_action_tells_a_promise_from_a_summary_or_question():
    from local_agent.core.graph import promises_action

    for text in (
        "Заполняю данные.", "Проверяю сортировку Name (A to Z).", "Теперь открою корзину", "**Шаг 5**\n\nДалее:",
        "Для начала я перейду на сайт и выполню основные проверки.", "Итак, проверю корзину.",
    ):
        assert promises_action(text), text
    for text in (
        "Сценарии пройдены успешно.", "Жду ваших указаний.", "Могу продолжить с оплаты.", "Перейти к оплате?",
        "Отчёт сохранён в test_results.md.", "", "Я сообщу, когда будет готово.",
    ):
        assert not promises_action(text), text


def test_answer_in_english_is_rewritten_in_russian_before_it_reaches_the_chat(tmp_path):
    english = "I have finished checking the cart page. All products are displayed correctly and the totals match."
    provider = ScriptedProvider(english, "Проверил корзину: все товары на месте, суммы сходятся.")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Проверь корзину"}))
        history = client.get(url + "/messages").json()

    assert events[-1]["type"] == "done" and events[-1]["message"]["content"].startswith("Проверил корзину")
    assert any(event["type"] == "retry" for event in events)
    assert "по-русски" in provider.requests[1]["messages"][-1].content
    # Английский вариант в историю не попал.
    assert [message["content"] for message in history if message["role"] == "assistant"] == [
        "Проверил корзину: все товары на месте, суммы сходятся.",
    ]


def test_foreign_answer_is_kept_when_the_user_asked_for_it():
    from local_agent.core.graph import foreign_allowed, is_foreign

    assert not is_foreign("Готово: тест-кейсы в `test_cases.md`, сценарии Login, Cart и Checkout на https://www.saucedemo.com/.")
    assert foreign_allowed([{"role": "user", "content": "Напиши README на английском"}])
    assert foreign_allowed([{"role": "user", "content": "Please summarize the article about cart checkout flows for me."}])
    assert not foreign_allowed([{"role": "user", "content": "Проверь корзину"}])


class ThinkingProvider(ScriptedProvider):
    """Стримит рассуждения перед каждым шагом сценария."""

    async def chat_stream(self, model, messages, tools=None):
        result = await self.chat(model, messages, tools)
        yield ReasoningDelta(f"думаю над шагом {len(self.requests)}")
        if result.content:
            yield result.content
        yield result


def test_reasoning_is_kept_for_display_but_not_sent_back_to_model(tmp_path):
    provider = ThinkingProvider(call("activate", names=["git"]), "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Подключи git"}))
        history = client.get(url + "/messages").json()
        client.post(url + "/turns/stream", json={"content": "Ещё"})

    assert [event["text"] for event in events if event["type"] == "reasoning"] == ["думаю над шагом 1", "думаю над шагом 2"]
    calls, answer = [message for message in history if message["role"] == "assistant"]
    assert calls["reasoning"] == "думаю над шагом 1" and answer["reasoning"] == "думаю над шагом 2"
    sent = " ".join(message.content or "" for message in provider.requests[-1]["messages"])
    assert "думаю над шагом" not in sent


def test_fit_turn_shortens_oldest_tool_results_first():
    from local_agent.core.context import fit_turn
    from local_agent.llm.models import ChatMessage

    messages = [ChatMessage("system", "s"), ChatMessage("user", "u")]
    messages += [ChatMessage("tool", str(index) * 4000, tool_call_id=str(index)) for index in range(4)]
    same, compacted = fit_turn(messages, budget=100_000)
    assert same is messages and compacted == 0
    fitted, compacted = fit_turn(messages, budget=3600)
    # Хватило сократить самый давний; два последних результата не трогаются никогда.
    assert compacted == 1 and "сокращено до 2000 из 4000" in fitted[2].content
    assert [message.content for message in fitted[3:]] == [message.content for message in messages[3:]]
    tight, compacted = fit_turn(messages, budget=10)
    assert compacted == 2 and tight[4].content == messages[4].content and len(tight[2].content) < 400


def test_long_turn_is_compacted_instead_of_overflowing(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    for name in "abcd":
        (workspace / f"{name}.txt").write_text(name * 6000, encoding="utf-8")
    provider = ScriptedProvider(*(call("read_file", path=f"{name}.txt") for name in "abcd"), "Готово")
    small = settings(tmp_path).model_copy(update={"default_context_tokens": 6000})
    with TestClient(create_app(small, llm_provider=provider)) as client:
        session = project_session(client, workspace)
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Прочитай всё"}))

    assert events[-1]["type"] == "done"
    assert any("сокращены давние результаты" in event.get("text", "") for event in events if event["type"] == "log")
    results = [message.content for message in provider.requests[-1]["messages"] if message.role == "tool"]
    assert "сокращено" in results[0] and "d" * 6000 in results[-1] and "c" * 6000 in results[-2]


def test_turn_does_not_end_while_plan_has_open_steps(tmp_path):
    provider = ScriptedProvider(
        call("plan", action="set", steps=["Тест-кейсы", "Автотесты"]),
        "Тест-кейсы готовы: …",
        call("plan", action="done", step=1, note="в ответе"),
        call("plan", action="done", step=2, note="tests/test_login.py"),
        "Итог: всё сделано",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Сделай кейсы и автотесты"}))
        # Новый ход с вопросом не по плану: старый, даже незаконченный, план ход не держит.
        client.app.state.session_service.set_plan(session["id"], [{"text": "Старое", "done": False}], "old")
        later = events_of(client.post(url + "/turns/stream", json={"content": "Который час?"}))

    assert events[-1]["type"] == "done" and events[-1]["message"]["content"] == "Итог: всё сделано"
    assert any("В плане не сделано пунктов: 2" in event.get("text", "") for event in events if event["type"] == "log")
    nudge = provider.requests[2]["messages"][-1]
    assert nudge.role == "user" and "2. Автотесты" in nudge.content
    # План — в системном промпте каждого запроса, пока в нём есть несделанное.
    assert "1. [ ] Тест-кейсы" in provider.system_prompt(1) and "1. [x] Тест-кейсы — в ответе" in provider.system_prompt(3)
    assert later[-1]["type"] == "done" and "Старое" in provider.system_prompt(-1)


def test_question_to_user_ends_turn_even_with_open_plan(tmp_path):
    provider = ScriptedProvider(call("plan", action="set", steps=["Кейсы", "Автотесты"]), "Какой фреймворк использовать?")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Сделай"}))
        stored = client.app.state.session_service.get(session["id"])

    assert events[-1]["message"]["content"] == "Какой фреймворк использовать?" and len(provider.requests) == 2
    assert [step["text"] for step in stored.plan] == ["Кейсы", "Автотесты"]
