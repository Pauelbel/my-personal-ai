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
        database_path=tmp_path / "agent.sqlite3",
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
