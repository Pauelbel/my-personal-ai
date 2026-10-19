"""Делегирование через API: отказ, восстановление выбранного исполнителя и обязательное ревью."""

import asyncio
import json

from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of
from tests.test_graph_flow import project_session, settings

from agents.files import save_files
from agents.revisions import agent_revision
from api.app import create_app
from core.delegation import MAX_ATTEMPTS, MAX_REPAIR_ROUNDS
from core.tool import ToolResult
from llm.base import LLMProviderError
from llm.models import ChatResult, ToolCall

SCRIPT_STEPS = "## Сделать результат\nскрипт: work.py\n"
MODEL_STEPS = "## Сделать результат\nмодель: Напиши итог задачи\nсохранить: result.md\n"
FAILURE = "Скрипт завершился с кодом 1.\nTraceback:\nModuleNotFoundError: No module named 'broken_dependency'"


class FlowProvider(ScriptedProvider):
    """Сценарий умеет сбой провайдера, динамический вызов с revision и несколько вызовов за ответ."""

    async def chat(self, model, messages, tools=None, response_format=None, options=None):
        if self.steps and callable(self.steps[0]):
            self.steps[0] = self.steps[0]()
        if self.steps and isinstance(self.steps[0], Exception | ChatResult):
            step = self.steps.pop(0)
            self.requests.append({"model": model, "messages": list(messages), "tools": tools})
            if isinstance(step, Exception):
                raise step
            return step
        return await super().chat(model, messages, tools, response_format, options)


class GatedFlowProvider(FlowProvider):
    """После карточки останавливает модельный шаг исполнителя до shutdown или Stop."""

    def __init__(self, *steps):
        super().__init__(*steps)
        self.block = False
        self.entered = asyncio.Event()

    async def chat(self, *args, **kwargs):
        if self.block:
            self.entered.set()
            await asyncio.Event().wait()
        return await super().chat(*args, **kwargs)


def resume_in_background(client, session_id, provider):
    async def resume():
        hub, runtime = client.app.state.turn_hub, client.app.state.agent_runtime
        turn = hub.start(session_id, runtime.resume(session_id, {"action": "approve"}))
        await asyncio.wait_for(provider.entered.wait(), timeout=2)
        return turn

    provider.block = True
    return client.portal.call(resume)


def install_designer(client, steps=SCRIPT_STEPS, *, agent_id="designer"):
    capabilities = client.app.state.capabilities
    files = {
        "AGENT.md": "---\nname: Специалист по требованиям\ndescription: Выполняет порученную задачу\ntools: read_file\n---\nРаботай по своим шагам.\n",
        "steps.md": steps,
    }
    if "скрипт:" in steps:
        files["scripts/work.py"] = "import broken_dependency\n"
    agent = save_files(capabilities.paths.agents, agent_id, files, create=capabilities.agents.get(agent_id) is None)
    capabilities.agents.replace(agent)
    return agent


def start(client, workspace, content="Сделай задачу выбранным агентом"):
    session = project_session(client, workspace)
    url = f"/api/sessions/{session['id']}"
    events = events_of(client.post(url + "/turns/stream", json={"content": content}))
    assert events[-1]["type"] == "interrupt"
    assert events[-1]["pending"]["call"]["name"] == "delegate_agent"
    return session, url, events


def approve(client, url):
    return events_of(client.post(url + "/resume", json={"action": "approve"}))


def scripts_fail(monkeypatch):
    called = []

    async def execute(script, arguments, workspace, *args, **kwargs):
        called.append((script, arguments, workspace))
        return ToolResult(FAILURE, is_error=True)

    monkeypatch.setattr("steps.engine.run_script", execute)
    return called


def result_messages(events):
    return [event["message"] for event in events if event["type"] == "tool_result"]


def run_files(client):
    store = client.app.state.delegations
    return [store.get(path.stem) for path in store.root.glob("*.json")]


def test_failed_script_is_diagnosed_and_main_cannot_write_or_present_its_own_result(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    called = scripts_fail(monkeypatch)
    provider = FlowProvider(
        call("delegate_agent", agent="designer"),
        call("write_file", path="fallback.md", content="Кейсы написал основной агент"),
        "Кейсы готовы: самостоятельно написал результат вместо специалиста",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client)
        session, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "done"
        final = events[-1]["message"]["content"]
        assert "не выполнено" in final and "designer" in final and "ModuleNotFoundError" in final
        assert "самостоятельно написал" not in final
        assert not (workspace / "fallback.md").exists() and len(called) == 1
        results = result_messages(events)
        assert [item["tool_name"] for item in results] == ["delegate_agent", "read_capability", "write_file"]
        assert results[-1]["is_error"] and "закреплено за designer" in results[-1]["content"]
        assert set(provider.tool_names(1)) == {"read_capability", "delegate_agent", "propose_agent_patch", "stop_delegation"}
        [run] = run_files(client)
        assert run["agent"] == "designer" and run["status"] == "blocked" and run["attempts"] == 1
        history = client.get(url + "/messages").json()
        assert not any("самостоятельно написал" in item["content"] for item in history if item["role"] == "assistant")
        # Следующая реплика восстанавливает прежнее поручение, а не освобождает основного агента от него.
        provider.steps = ["Задача будто бы выполнена"]
        later = events_of(client.post(url + "/turns/stream", json={"content": "Продолжай"}))
        assert "не выполнено" in later[-1]["message"]["content"]
        assert client.app.state.delegations.active(session["id"])["id"] == run["id"]


def test_transient_model_failure_retries_same_run_without_second_approval(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = FlowProvider(
        call("delegate_agent", agent="designer"), LLMProviderError("Connection refused"),
        "Результат выбранного специалиста", "Готово: специалист закончил задачу",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client, MODEL_STEPS)
        _, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "done" and not any(item["type"] == "interrupt" for item in events)
        [run] = run_files(client)
        assert run["agent"] == "designer" and run["status"] == "done" and run["attempts"] == 2
        assert [entry["status"] for entry in run["history"]] == ["failed", "done"]
        results = [item for item in result_messages(events) if item["tool_name"] == "delegate_agent"]
        assert len(results) == 2
        assert {item["meta"]["delegation"]["id"] for item in results} == {run["id"]}
        assert (workspace / "result.md").read_text(encoding="utf-8") == "Результат выбранного специалиста\n"


def test_patch_requires_new_run_approval_and_resumes_same_agent_after_restart(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    called = []

    async def execute(script, arguments, folder, *args, **kwargs):
        called.append(script)
        if len(called) == 1:
            return ToolResult(FAILURE, is_error=True)
        (folder / "result.md").write_text("Результат выбранного агента", encoding="utf-8")
        return ToolResult("Результат: result.md")

    monkeypatch.setattr("steps.engine.run_script", execute)
    configured = settings(tmp_path)
    provider = FlowProvider()
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        agent = install_designer(client)
        provider.steps = [call("delegate_agent", agent=agent.id), call(
            "propose_agent_patch", agent=agent.id, expected_revision=agent_revision(agent),
            reason="Исправлен сломанный импорт скрипта",
            files={"scripts/work.py": "print('исправленный агент')\n"},
        ), "Готово: задачу завершил выбранный агент"]
        session, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["kind"] == "agent_patch"
        run = client.app.state.delegations.active(session["id"])
        original_revision = run["revision"]
        events = approve(client, url)
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["kind"] == "steps"
        assert events[-1]["pending"]["run"]["id"] == "designer" and len(called) == 1
        retry_arguments = json.loads(events[-1]["pending"]["call"]["arguments"])
        assert retry_arguments["run_id"] == run["id"] and retry_arguments["agent"] == "designer"
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        assert client.get(url).json()["pending"]["kind"] == "steps"
        events = approve(client, url)
        assert events[-1]["type"] == "done" and len(called) == 2
        [finished] = run_files(client)
        assert finished["id"] == run["id"] and finished["agent"] == "designer"
        assert finished["status"] == "done" and finished["attempts"] == 2
        assert finished["history"][0]["revision"] == original_revision
        assert finished["history"][1]["revision"] != original_revision
        assert all(path.parent.parent.name == "designer" for path in called)
        assert (workspace / "result.md").read_text(encoding="utf-8") == "Результат выбранного агента"


def test_rejected_patch_stops_recovery_without_retry_or_source_changes(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    called = scripts_fail(monkeypatch)
    provider = FlowProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        agent = install_designer(client)
        original_revision = agent_revision(agent)
        provider.steps = [call("delegate_agent", agent=agent.id), call(
            "propose_agent_patch", agent=agent.id, expected_revision=original_revision,
            reason="Исправление импорта", files={"scripts/work.py": "print('исправлено')\n"},
        )]
        session, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["pending"]["kind"] == "agent_patch"
        events = events_of(client.post(url + "/resume", json={"action": "reject"}))
        assert events[-1]["type"] == "done" and "не разрешил" in events[-1]["message"]["content"]
        assert len(called) == 1 and agent_revision(agent) == original_revision
        run = client.app.state.delegations.active(session["id"])
        assert run["status"] == "blocked" and run["attempts"] == 1


def test_recovery_has_a_persistent_round_limit(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    called = scripts_fail(monkeypatch)
    provider = FlowProvider(call("delegate_agent", agent="designer"), *[
        call("read_capability", kind="agent", name="designer") for _ in range(MAX_REPAIR_ROUNDS + 3)
    ])
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client)
        _, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "done" and "Лимит" in events[-1]["message"]["content"]
        [run] = run_files(client)
        assert run["status"] == "blocked" and run["repair_rounds"] == MAX_REPAIR_ROUNDS
        assert len(provider.requests) == MAX_REPAIR_ROUNDS + 1 and len(called) == 1


def test_recovery_cannot_switch_to_another_agent(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    called = scripts_fail(monkeypatch)
    provider = FlowProvider(call("delegate_agent", agent="designer"), call("delegate_agent", agent="browser-qa"), "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client)
        _, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "done" and not any(item["type"] == "interrupt" for item in events)
        results = [item for item in result_messages(events) if item["tool_name"] == "delegate_agent"]
        assert len(results) == 2 and results[1]["is_error"] and "закреплено за designer" in results[1]["content"]
        [run] = run_files(client)
        assert run["agent"] == "designer" and run["attempts"] == 1 and len(called) == 1


def test_a_write_in_the_same_batch_after_failed_delegate_is_blocked(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    scripts_fail(monkeypatch)
    provider = FlowProvider(ChatResult(content=None, tool_calls=(
        ToolCall("delegate", "delegate_agent", json.dumps({"agent": "designer"})),
        ToolCall("write", "write_file", json.dumps({"path": "fallback.md", "content": "Подмена исполнителя"})),
    )), "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client)
        _, url, events = start(client, workspace)
        all_events = list(events)
        for _ in range(3):
            events = approve(client, url)
            all_events.extend(events)
            if events[-1]["type"] != "interrupt":
                break
        assert events[-1]["type"] == "done" and not (workspace / "fallback.md").exists()
        [write] = [item for item in result_messages(all_events) if item["tool_name"] == "write_file"]
        assert write["is_error"] and "закреплено за designer" in write["content"]


def test_transient_failures_stop_at_attempt_limit_without_spurious_approvals(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = FlowProvider(call("delegate_agent", agent="designer"), *[
        LLMProviderError("HTTP 503: temporary unavailable") for _ in range(MAX_ATTEMPTS + 2)
    ])
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client, MODEL_STEPS)
        _, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "done" and not any(item["type"] == "interrupt" for item in events)
        [run] = run_files(client)
        assert run["status"] == "blocked" and run["attempts"] == MAX_ATTEMPTS
        assert len(run["history"]) == MAX_ATTEMPTS and len(provider.requests) == MAX_ATTEMPTS + 1


def test_test_cases_completion_requires_test_review_before_the_final_answer(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "docs").mkdir()
    (workspace / "docs/spec.md").write_text("Требования для тестирования", encoding="utf-8")

    async def generate(script, arguments, folder, *args, **kwargs):
        target = folder / "testgen/test_cases/docs/spec.md"
        target.parent.mkdir(parents=True)
        target.write_text("| ID | Проверка | Вопросы |\n| --- | --- | --- |\n| TC-1 | Вход | |\n", encoding="utf-8")
        return ToolResult("Готово: testgen/test_cases/docs/spec.md")

    monkeypatch.setattr("steps.engine.run_script", generate)
    provider = FlowProvider(
        call("delegate_agent", agent="test-cases", inputs={"источник": "docs/spec.md"}),
        "Ревью выполнено агентом test-review", "Кейсы и ревью готовы",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client, "## Проверить кейсы\nмодель: Проверь таблицу тест-кейсов\nсохранить: review.md\n", agent_id="test-review")
        _, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["call"]["name"] == "delegate_agent"
        assert events[-1]["pending"]["run"]["id"] == "test-review"
        [parent] = run_files(client)
        assert parent["agent"] == "test-cases" and parent["status"] == "review_pending"
        assert not (workspace / "review.md").exists()
        events = approve(client, url)
        assert events[-1]["type"] == "done"
        runs = run_files(client)
        assert len(runs) == 2 and all(run["status"] == "done" for run in runs)
        [review] = [run for run in runs if run["agent"] == "test-review"]
        assert review["parent"] == parent["id"]
        assert (workspace / "review.md").read_text(encoding="utf-8") == "Ревью выполнено агентом test-review\n"


def test_reused_provider_call_id_in_a_new_task_does_not_replay_the_previous_task(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    repeated = ChatResult(content=None, tool_calls=(ToolCall("provider-id", "delegate_agent", '{"agent":"designer"}'),))
    provider = FlowProvider(
        repeated, "Результат первой задачи", "Первая задача готова",
        repeated, "Результат второй задачи", "Вторая задача готова",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client, MODEL_STEPS)
        _, url, _ = start(client, workspace)
        assert approve(client, url)[-1]["type"] == "done"
        second = events_of(client.post(url + "/turns/stream", json={"content": "Выполни новую задачу тем же специалистом"}))
        assert second[-1]["type"] == "interrupt"
        assert approve(client, url)[-1]["type"] == "done"
        runs = run_files(client)
        assert len(runs) == 2 and len({run["id"] for run in runs}) == 2
        assert len({run["task_id"] for run in runs}) == 2 and all(run["attempts"] == 1 for run in runs)
        assert (workspace / "result.md").read_text(encoding="utf-8") == "Результат второй задачи\n"


def test_rejecting_mandatory_review_cancels_parent_without_restarting_review_on_next_turn(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()

    async def generate(script, arguments, folder, *args, **kwargs):
        target = folder / "testgen/test_cases/spec.md"
        target.parent.mkdir(parents=True)
        target.write_text("| ID | Проверка |\n| --- | --- |\n| TC-1 | Вход |\n", encoding="utf-8")
        return ToolResult("Кейсы: testgen/test_cases/spec.md")

    monkeypatch.setattr("steps.engine.run_script", generate)
    provider = FlowProvider(call("delegate_agent", agent="test-cases", inputs={"источник": "spec.md"}))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["pending"]["run"]["id"] == "test-review"
        events = events_of(client.post(url + "/resume", json={"action": "reject"}))
        assert events[-1]["type"] == "done" and "не выполнена" in events[-1]["message"]["content"]
        assert client.app.state.delegations.active(session["id"]) is None
        [run] = run_files(client)
        assert run["status"] == "stopped" and run["agent"] == "test-cases"
        provider.steps = ["Можно обсудить следующий вопрос"]
        events = events_of(client.post(url + "/turns/stream", json={"content": "Следующий вопрос"}))
        assert events[-1]["type"] == "done" and len(run_files(client)) == 1


def test_canceling_failed_reviewer_also_cancels_its_parent_task(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()

    async def execute(script, arguments, folder, *args, **kwargs):
        if script.parent.parent.name == "test-review":
            return ToolResult(FAILURE, is_error=True)
        target = folder / "testgen/test_cases/spec.md"
        target.parent.mkdir(parents=True)
        target.write_text("| ID | Проверка |\n| --- | --- |\n| TC-1 | Вход |\n", encoding="utf-8")
        return ToolResult("Кейсы: testgen/test_cases/spec.md")

    monkeypatch.setattr("steps.engine.run_script", execute)
    provider = FlowProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client, agent_id="test-review")
        provider.steps = [call("delegate_agent", agent="test-cases", inputs={"источник": "spec.md"})]
        session, url, _ = start(client, workspace, "Сделай кейсы; если ревью упадёт, отмени поручение")
        events = approve(client, url)
        assert events[-1]["pending"]["run"]["id"] == "test-review"
        provider.steps = [lambda: call(
            "stop_delegation", run_id=client.app.state.delegations.active(session["id"])["id"],
            reason="Пользователь просил отменить поручение при сбое ревью",
        )]
        events = approve(client, url)
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["call"]["name"] == "stop_delegation"
        events = approve(client, url)
        assert events[-1]["type"] == "done" and "отменено" in events[-1]["message"]["content"]
        runs = run_files(client)
        assert len(runs) == 2 and all(run["status"] == "stopped" for run in runs)
        assert client.app.state.delegations.active(session["id"]) is None


def test_rejected_initial_delegate_does_not_allow_a_later_write_from_the_same_batch(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = FlowProvider(ChatResult(content=None, tool_calls=(
        ToolCall("delegate", "delegate_agent", '{"agent":"designer"}'),
        ToolCall("write", "write_file", json.dumps({"path": "fallback.md", "content": "Сделал основной агент"})),
    )))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client, MODEL_STEPS)
        _, url, _ = start(client, workspace)
        events = events_of(client.post(url + "/resume", json={"action": "reject"}))
        if events[-1]["type"] == "interrupt":
            events = approve(client, url)
        assert events[-1]["type"] == "done"
        assert not (workspace / "fallback.md").exists() and not run_files(client)
        [write] = [item for item in result_messages(events) if item["tool_name"] == "write_file"]
        assert write["is_error"]


def test_graceful_restart_preserves_running_owner_and_blocks_main_fallback(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    configured = settings(tmp_path)
    provider = GatedFlowProvider(call("delegate_agent", agent="designer"))
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        install_designer(client, MODEL_STEPS)
        session, url, _ = start(client, workspace)
        resume_in_background(client, session["id"], provider)
        store = client.app.state.delegations
        run = store.active(session["id"])
        assert run["status"] == "running" and run["attempts"] == 1
    assert store.active(session["id"])["id"] == run["id"]
    assert store.get(run["id"])["status"] == "running"
    provider = FlowProvider(call("write_file", path="fallback.md", content="Сам сделал результат"), "Готово")
    with TestClient(create_app(configured, llm_provider=provider)) as client:
        events = events_of(client.post(url + "/turns/stream", json={"content": "Продолжай после перезапуска"}))
        assert events[-1]["type"] == "done" and "не выполнено" in events[-1]["message"]["content"]
        assert not (workspace / "fallback.md").exists() and not (workspace / "result.md").exists()
        active = client.app.state.delegations.active(session["id"])
        assert active["id"] == run["id"] and active["agent"] == "designer"
        assert active["status"] == "blocked" and active["attempts"] == 1
        [write] = [item for item in result_messages(events) if item["tool_name"] == "write_file"]
        assert write["is_error"]


def test_explicit_stop_releases_owner_of_an_inflight_model_step(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = GatedFlowProvider(call("delegate_agent", agent="designer"))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client, MODEL_STEPS)
        session, url, _ = start(client, workspace)
        turn = resume_in_background(client, session["id"], provider)
        run = client.app.state.delegations.active(session["id"])
        assert client.post(url + "/turn/stop").status_code == 204

        async def settled():
            await asyncio.wait_for(turn.task, timeout=2)

        client.portal.call(settled)
        assert client.app.state.delegations.active(session["id"]) is None
        assert client.app.state.delegations.get(run["id"])["status"] == "stopped"
        assert not (workspace / "result.md").exists()


def test_exhausted_recovery_still_allows_explicit_cancellation_on_next_turn(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    called = scripts_fail(monkeypatch)
    provider = FlowProvider(call("delegate_agent", agent="designer"), *[
        call("read_capability", kind="agent", name="designer") for _ in range(MAX_REPAIR_ROUNDS)
    ])
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client)
        session, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "done" and "Лимит" in events[-1]["message"]["content"]
        run = client.app.state.delegations.active(session["id"])
        assert run["repair_rounds"] == MAX_REPAIR_ROUNDS and run["attempts"] == 1
        provider.steps = [call("stop_delegation", run_id=run["id"], reason="Пользователь отменил поручение")]
        events = events_of(client.post(url + "/turns/stream", json={"content": "Отмени это поручение"}))
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["call"]["name"] == "stop_delegation"
        assert provider.tool_names(len(provider.requests) - 1) == ["stop_delegation"]
        events = approve(client, url)
        assert events[-1]["type"] == "done" and "отменено" in events[-1]["message"]["content"]
        assert client.app.state.delegations.active(session["id"]) is None
        finished = client.app.state.delegations.get(run["id"])
        assert finished["status"] == "stopped" and finished["attempts"] == 1
        assert finished["repair_rounds"] == MAX_REPAIR_ROUNDS and len(called) == 1


def test_missing_mandatory_reviewer_finishes_incomplete_without_a_loop_or_approval(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()

    async def generate(script, arguments, folder, *args, **kwargs):
        target = folder / "testgen/test_cases/spec.md"
        target.parent.mkdir(parents=True)
        target.write_text("| ID | Проверка |\n| --- | --- |\n| TC-1 | Вход |\n", encoding="utf-8")
        return ToolResult("Кейсы: testgen/test_cases/spec.md")

    monkeypatch.setattr("steps.engine.run_script", generate)
    provider = FlowProvider(call("delegate_agent", agent="test-cases", inputs={"источник": "spec.md"}))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        client.app.state.capabilities.agents.remove("test-review")
        session, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "done" and "не выполнено" in events[-1]["message"]["content"]
        assert not any(event["type"] == "interrupt" for event in events)
        assert len(provider.requests) == 1 and (workspace / "testgen/test_cases/spec.md").is_file()
        [run] = run_files(client)
        assert run["agent"] == "test-cases" and run["status"] == "blocked"
        assert run["attempts"] == 1 and run["reviewed"] == 0
        assert client.app.state.delegations.active(session["id"])["id"] == run["id"]


def test_patch_on_last_repair_round_still_requires_approval_and_retries_same_run(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    called = []

    async def execute(script, arguments, folder, *args, **kwargs):
        called.append(script)
        if len(called) == 1:
            return ToolResult(FAILURE, is_error=True)
        (folder / "result.md").write_text("Проверенный результат исправленного агента", encoding="utf-8")
        return ToolResult("Результат: result.md")

    monkeypatch.setattr("steps.engine.run_script", execute)
    provider = FlowProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        agent = install_designer(client)
        provider.steps = [call("delegate_agent", agent=agent.id), *[
            call("read_capability", kind="agent", name=agent.id) for _ in range(MAX_REPAIR_ROUNDS - 1)
        ], call("propose_agent_patch", agent=agent.id, expected_revision=agent_revision(agent),
                reason="На последнем диагностическом запросе исправлен сломанный импорт",
                files={"scripts/work.py": "print('исправлено')\n"}), "Задачу завершил выбранный агент"]
        session, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["kind"] == "agent_patch"
        run = client.app.state.delegations.active(session["id"])
        assert run["repair_rounds"] == MAX_REPAIR_ROUNDS and run["attempts"] == 1 and len(called) == 1
        events = approve(client, url)
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["kind"] == "steps"
        arguments = json.loads(events[-1]["pending"]["call"]["arguments"])
        assert arguments["run_id"] == run["id"] and arguments["agent"] == "designer" and len(called) == 1
        events = approve(client, url)
        assert events[-1]["type"] == "done" and len(called) == 2
        [finished] = run_files(client)
        assert finished["id"] == run["id"] and finished["agent"] == "designer" and finished["status"] == "done"
        assert finished["attempts"] == 2 and finished["repair_rounds"] == MAX_REPAIR_ROUNDS
        assert [entry["status"] for entry in finished["history"]] == ["failed", "done"]
        assert (workspace / "result.md").read_text(encoding="utf-8") == "Проверенный результат исправленного агента"


def test_recovery_of_mandatory_reviewer_cannot_replace_the_parent_case_file(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "unrelated.md").write_text("Другой документ", encoding="utf-8")
    reviewer_calls = []

    async def execute(script, arguments, folder, *args, **kwargs):
        if script.parent.parent.name == "test-review":
            reviewer_calls.append(arguments)
            return ToolResult(FAILURE, is_error=True) if len(reviewer_calls) == 1 else ToolResult("Проверка завершена")
        target = folder / "testgen/test_cases/spec.md"
        target.parent.mkdir(parents=True)
        target.write_text("| ID | Проверка |\n| --- | --- |\n| TC-1 | Вход |\n", encoding="utf-8")
        return ToolResult("Кейсы: testgen/test_cases/spec.md")

    monkeypatch.setattr("steps.engine.run_script", execute)
    provider = FlowProvider(call("delegate_agent", agent="test-cases", inputs={"источник": "spec.md"}))
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        install_designer(client, "## Нужно от вас\n- тест-кейсы — файл кейсов\n- требования — исходные требования\n"
                         "## Проверить кейсы\nскрипт: work.py\nпараметры: {\"path\": \"{тест-кейсы}\"}\n",
                         agent_id="test-review")
        session, url, _ = start(client, workspace)
        events = approve(client, url)
        assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["run"]["id"] == "test-review"
        provider.steps = [lambda: call(
            "delegate_agent", agent="test-review", run_id=client.app.state.delegations.active(session["id"])["id"],
            inputs={"тест-кейсы": "unrelated.md"},
        ), "Ревью будто бы выполнено"]
        events = approve(client, url)
        assert events[-1]["type"] == "done" and not any(event["type"] == "interrupt" for event in events)
        parent, child = sorted(run_files(client), key=lambda run: bool(run.get("parent")))
        assert parent["agent"] == "test-cases" and parent["status"] == "review_pending" and parent["reviewed"] == 0
        assert child["parent"] == parent["id"] and child["agent"] == "test-review" and child["status"] == "blocked"
        assert child["attempts"] == 1 and child["inputs"]["тест-кейсы"] == "testgen/test_cases/spec.md"
        assert len(reviewer_calls) == 1
        retries = [item for item in result_messages(events) if item["tool_name"] == "delegate_agent"]
        assert len(retries) == 2 and retries[-1]["is_error"]
