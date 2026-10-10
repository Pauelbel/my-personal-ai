"""Сквозные проверки записи инструкций и восстановления хода с управляемым провайдером."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.llm.base import LLMProviderUnavailable
from local_agent.llm.models import ChatResult, ToolCall

RULE = "Тестовые отчёты этого проекта называются «Проверка Маяк»."
RULE_PATH = ".github/instructions/learnings.instructions.md"


def settings(tmp_path):
    return Settings(
        agents_path=tmp_path / "agents", skills_path=tmp_path / "skills",
        sessions_path=tmp_path / "sessions", projects_path=tmp_path / "projects",
        conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory",
        memory_auto_update_messages=0, _env_file=None,
    )


def project_session(client, workspace):
    project = client.post("/api/projects", json={"name": "Проект", "workspace": str(workspace)}).json()
    return client.post("/api/sessions", json={"model": "test", "project_id": project["id"]}).json()


class WorkflowProvider:
    def __init__(self):
        self.step = 0
        self.messages = []

    async def chat(self, model, messages, tools=None):
        self.messages.append(list(messages))
        steps = (
            ("use_skill", {"name": "update-skills"}),
            ("write_file", {"path": RULE_PATH, "content": RULE}),
            ("read_file", {"path": RULE_PATH}),
        )
        if self.step < len(steps):
            name, arguments = steps[self.step]
            self.step += 1
            return ChatResult(content=None, tool_calls=(ToolCall(
                f"call-{self.step}", name, json.dumps(arguments, ensure_ascii=False),
            ),))
        return ChatResult(content="Готово")

    async def close(self):
        pass


@pytest.mark.parametrize("approved", [True, False])
def test_skill_write_approval_and_new_dialog(tmp_path, approved):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = WorkflowProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        session_id = session["id"]
        assert client.put(f"/api/sessions/{session_id}/tools/write_file", json={
            "enabled": True,
        }).status_code == 200
        runtime = client.app.state.agent_runtime
        events = []

        async def exercise():
            async for event in runtime.stream_turn(session_id, "Запомни для проекта", "test"):
                events.append(event)
                if event["type"] == "approval_required":
                    assert not (workspace / RULE_PATH).exists()
                    assert event["call"]["name"] == "write_file"
                    assert runtime.resolve_approval(session_id, event["call"]["id"], approved)

        client.portal.call(exercise)
        assert sum(event["type"] == "approval_required" for event in events) == 1
        results = [event["message"] for event in events if event["type"] == "tool_result"]
        assert "Обновление инструкций" in results[0].content
        assert (workspace / RULE_PATH).exists() is approved
        if approved:
            assert (workspace / RULE_PATH).read_text(encoding="utf-8") == RULE
            assert results[-1].content == RULE
        else:
            assert "отклонил" in results[1].content
        assert not runtime.is_busy(session_id)
        fresh = client.post("/api/sessions", json={
            "project_id": session["project_id"], "model": "test",
        }).json()
        assert client.post(f"/api/sessions/{fresh['id']}/turns", json={
            "content": "Как называется тестовый отчёт?",
        }).status_code == 200
        assert (RULE in provider.messages[-1][0].content) is approved
        assert len(provider.messages[-1]) == 2


class RecoveryProvider:
    def __init__(self):
        self.mode = "interrupt"
        self.messages = []

    async def chat_stream(self, model, messages, tools=None):
        self.messages.append(list(messages))
        if self.mode == "interrupt":
            yield "Начало ответа"
            await asyncio.Event().wait()
        elif self.mode == "unavailable":
            raise LLMProviderUnavailable("Тестовая недоступность")
        elif self.mode == "tool_error":
            self.mode = "success"
            yield ChatResult(content=None, tool_calls=(ToolCall("missing", "read_file", '{"path":"missing.txt"}'),))
        else:
            yield "Ответ после повтора"
            yield ChatResult(content="Ответ после повтора")

    async def close(self):
        pass


def test_interrupted_answer_is_saved_and_next_turn_is_available(tmp_path):
    provider = RecoveryProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test"}).json()
        session_id = session["id"]
        runtime = client.app.state.agent_runtime

        async def exercise():
            received = asyncio.Event()

            async def consume():
                async for event in runtime.stream_turn(session_id, "Первый запрос", "test"):
                    if event["type"] == "delta":
                        received.set()

            task = asyncio.create_task(consume())
            await asyncio.wait_for(received.wait(), timeout=2)
            assert runtime.is_busy(session_id)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        client.portal.call(exercise)
        assert not runtime.is_busy(session_id)
        history = client.get(f"/api/sessions/{session_id}/messages").json()
        assert [message["role"] for message in history] == ["user", "assistant"]
        assert "Начало ответа" in history[-1]["content"]
        assert "ответ прерван" in history[-1]["content"]
        provider.mode = "success"
        assert client.post(f"/api/sessions/{session_id}/turns", json={
            "content": "Продолжи",
        }).status_code == 200
        assert "Начало ответа" in provider.messages[-1][-2].content


def test_provider_failure_and_tool_error_allow_retry(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    provider = RecoveryProvider()
    provider.mode = "unavailable"
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace)
        url = f"/api/sessions/{session['id']}"
        failed = client.post(url + "/turns", json={"content": "Прочитай файл"})
        assert failed.status_code == 503
        assert failed.json()["detail"]["user_message_saved"]
        assert not client.app.state.agent_runtime.is_busy(session["id"])
        provider.mode = "tool_error"
        assert client.post(url + "/turns", json={"content": "Повтори"}).status_code == 200
        assert provider.messages[-1][-1].role == "tool"
        assert "missing.txt" in provider.messages[-1][-1].content
        history = client.get(url + "/messages").json()
        errors = [message for message in history if message["role"] == "tool"]
        assert len(errors) == 1 and errors[0]["is_error"]
        assert history[-1]["content"] == "Ответ после повтора"
        assert not client.app.state.agent_runtime.is_busy(session["id"])
