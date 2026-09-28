"""Опциональная проверка полного хода агента с запущенным локальным LM Studio."""

import os

import pytest
from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


@pytest.mark.skipif(
    not os.getenv("LM_STUDIO_TEST_MODEL"),
    reason="Set LM_STUDIO_TEST_MODEL to run against a local LM Studio server",
)
def test_live_lm_studio_turn(tmp_path) -> None:
    model = os.environ["LM_STUDIO_TEST_MODEL"]
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        default_model=model,
        _env_file=None,
    )

    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={}).json()
        response = client.post(
            f"/api/sessions/{session['id']}/turns",
            json={"content": "Say only OK."},
        )
        assert response.status_code == 200, response.text
        assert response.json()["role"] == "assistant"
        assert response.json()["content"].strip()
        history = client.get(f"/api/sessions/{session['id']}/messages").json()
        assert [message["role"] for message in history] == ["user", "assistant"]


@pytest.mark.skipif(
    not os.getenv("LM_STUDIO_TEST_MODEL"),
    reason="Set LM_STUDIO_TEST_MODEL to run against a local LM Studio server",
)
def test_live_memory_update(tmp_path) -> None:
    model = os.environ["LM_STUDIO_TEST_MODEL"]
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        default_model=model,
        _env_file=None,
    )

    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={}).json()
        client.post(
            f"/api/sessions/{session['id']}/messages",
            json={"content": "Я предпочитаю, чтобы изменения кода были небольшими и проверяемыми."},
        )
        updated = client.post(f"/api/sessions/{session['id']}/memory/update")
        assert updated.status_code == 200, updated.text
        assert updated.json()["processed_messages"] == 1
        assert updated.json()["applied_operations"] >= 1
        documents = client.get("/api/memory").json()
        contents = [
            client.get(f"/api/memory/{document['name']}").json()["content"]
            for document in documents
        ]
        assert any("\n- " in content for content in contents)
        assert all("<!-- memory:id=" not in content for content in contents)

        repeated = client.post(f"/api/sessions/{session['id']}/memory/update")
        assert repeated.status_code == 200
        assert repeated.json()["processed_messages"] == 0
