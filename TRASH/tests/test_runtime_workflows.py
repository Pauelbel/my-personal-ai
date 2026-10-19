"""Сквозные проверки записи инструкций и восстановления хода с управляемым провайдером."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from tests.fakes import post_session

from api.app import create_app
from config.settings import Settings
from llm.base import LLMProviderUnavailable
from llm.models import ChatResult, ToolCall


def settings(tmp_path):
    return Settings(
        sessions_path=tmp_path / "sessions", projects_path=tmp_path / "projects",
        conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory",
        memory_auto_update_messages=0, _env_file=None,
    )


def project_session(client, workspace):
    project = client.post("/api/projects", json={"name": "Проект", "workspace": str(workspace)}).json()
    return post_session(client, {"model": "test", "project_id": project["id"]}).json()


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
        session = post_session(client, {"model": "test"}).json()
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
