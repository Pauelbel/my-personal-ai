"""Настройки: сохранение и проверка значений."""

from fastapi.testclient import TestClient

from meepo.app import create_app
from meepo.llm.fake import FakeLLM
from meepo.settings import Env


def test_settings_roundtrip_and_validation(tmp_path):
    app = create_app(Env(data_path=tmp_path, default_model="env-model"), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/settings").json()["active_model"] == "env-model"
        saved = client.put("/api/settings", json={"model": "m", "guards": {"plan": False}, "max_tool_rounds": 10}).json()
        assert saved["active_model"] == "m" and saved["guards"]["plan"] is False and saved["guards"]["language"] is True
        assert client.put("/api/settings", json={"max_tool_rounds": 0}).status_code == 400
        assert client.put("/api/settings", json={"guards": {"magic": True}}).status_code == 400
        assert client.put("/api/settings", json={"llm_base_url": "ftp://x"}).status_code == 400
        assert client.put("/api/settings", json={"unknown": 1}).status_code == 400
        assert client.get("/api/models").json() == ["fake"]


def test_models_probe_other_server(tmp_path):
    app = create_app(Env(data_path=tmp_path, default_model="m"), FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/models", params={"base_url": "ftp://x"}).status_code == 400
        # Порт 9 закрыт: сервер модели недоступен — понятная ошибка, а не падение.
        response = client.get("/api/models", params={"base_url": "http://127.0.0.1:9/v1"})
        assert response.status_code == 503 and "Не удалось подключиться" in response.json()["detail"]
