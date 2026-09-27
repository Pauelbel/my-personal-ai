"""Проверка хода агента подтверждает ограниченный контекст и сохранение ответа."""

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings
from local_agent.llm.base import LLMProviderUnavailable
from local_agent.llm.models import ChatMessage, ChatResult


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def chat(self, model: str, messages: list[ChatMessage], tools=None) -> ChatResult:
        self.calls.append((model, messages))
        return ChatResult(content="Ответ модели", input_tokens=12, output_tokens=3)

    async def list_models(self) -> list[str]:
        return ["test-model"]

    async def close(self) -> None:
        pass


class FailingProvider(FakeProvider):
    async def chat(self, model: str, messages: list[ChatMessage]) -> ChatResult:
        raise LLMProviderUnavailable("Не удалось подключиться к LM Studio")


def test_turn_uses_recent_messages_and_persists_reply(tmp_path) -> None:
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        max_context_messages=2,
        _env_file=None,
    )
    provider = FakeProvider()

    with TestClient(create_app(settings, llm_provider=provider)) as client:
        assert client.get("/api/models").json()["models"] == ["test-model"]
        session = client.post("/api/sessions", json={"model": "test-model"}).json()
        for content in ("Старое", "Предыдущее"):
            assert client.post(
                f"/api/sessions/{session['id']}/messages", json={"content": content}
            ).status_code == 201

        response = client.post(
            f"/api/sessions/{session['id']}/turns", json={"content": "Новое"}
        )
        assert response.status_code == 200
        assert response.json()["content"] == "Ответ модели"
        assert client.get(f"/api/sessions/{session['id']}").json()["title"] == "Старое"
        assert client.get(f"/api/sessions/{session['id']}").json()["context_tokens"] == 12

    model, context = provider.calls[0]
    assert model == "test-model"
    assert [message.role for message in context] == ["system", "user"]
    assert context[1].content == "Предыдущее\n\nНовое"

    with TestClient(create_app(settings, llm_provider=FakeProvider())) as client:
        history = client.get(f"/api/sessions/{session['id']}/messages").json()
        assert client.get(f"/api/sessions/{session['id']}").json()["context_tokens"] == 12

    assert [message["content"] for message in history] == [
        "Старое", "Предыдущее", "Новое", "Ответ модели"
    ]
    assert [message["role"] for message in history] == [
        "user", "user", "user", "assistant"
    ]


def test_session_model_can_be_selected_after_creation(tmp_path) -> None:
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)
    provider = FakeProvider()

    with TestClient(create_app(settings, llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": ""}).json()
        configured = client.put(
            f"/api/sessions/{session['id']}/config",
            json={
                "provider": "lm_studio",
                "model": "test-model",
                "workspace": str(tmp_path),
            },
        )
        assert configured.status_code == 200
        assert configured.json()["model"] == "test-model"
        assert configured.json()["workspace"] == str(tmp_path.resolve())
        response = client.post(
            f"/api/sessions/{session['id']}/turns", json={"content": "Привет"}
        )
        assert response.status_code == 200
        changed = client.put(
            f"/api/sessions/{session['id']}/config",
            json={"provider": "lm_studio", "model": "another-model", "workspace": None},
        )
        assert changed.json()["context_tokens"] is None


def test_failed_model_call_keeps_user_message(tmp_path) -> None:
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)

    with TestClient(create_app(settings, llm_provider=FailingProvider())) as client:
        session = client.post("/api/sessions", json={"model": "test-model"}).json()
        response = client.post(
            f"/api/sessions/{session['id']}/turns", json={"content": "Привет"}
        )
        history = client.get(f"/api/sessions/{session['id']}/messages").json()

    assert response.status_code == 503
    assert response.json()["detail"]["user_message_saved"] is True
    assert [message["content"] for message in history] == ["Привет"]
