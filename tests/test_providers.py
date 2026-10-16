"""Провайдеры моделей из интерфейса: проверка подключения, сохранение ключа без выдачи наружу, модель по умолчанию."""

import json
import stat

import httpx
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider
from tests.test_graph_flow import settings

from local_agent.api.app import create_app


def server(seen):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.headers.get("authorization") != "Bearer sk-work-1234567890":
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        return httpx.Response(200, json={"data": [{"id": "gpt-4.1-mini"}, {"id": "Qwen3-32B"}]})

    return httpx.MockTransport(handle)


def test_provider_with_key_is_probed_saved_and_listed_without_the_key(tmp_path):
    seen = []
    with TestClient(create_app(settings(tmp_path), llm_provider=ScriptedProvider())) as client:
        client.app.state.provider_transport = server(seen)
        fields = {"name": "Работа", "kind": "openai", "base_url": "https://llm.example.com/v1"}
        wrong = client.post("/api/providers/probe", json={**fields, "api_key": "sk-wrong"})
        probe = client.post("/api/providers/probe", json={**fields, "api_key": "sk-work-1234567890"}).json()
        created = client.post("/api/providers", json={**fields, "api_key": "sk-work-1234567890"}).json()
        listed = {item["id"]: item for item in client.get("/api/providers").json()}
        models = client.get("/api/models", params={"provider": created["id"]}).json()
        # Правка без ключа оставляет сохранённый; проверка сохранённого берёт его ключ.
        client.put(f"/api/providers/{created['id']}", json={**fields, "name": "Работа (офис)"})
        again = client.post("/api/providers/probe", json={**fields, "id": created["id"]}).json()
        default = client.put("/api/providers/default", json={"provider": created["id"], "model": "Qwen3-32B"})
        config = client.get("/api/providers/config").json()
        project = client.post("/api/projects", json={"workspace": str(tmp_path)}).json()
        stored = tmp_path / "providers.json"
        saved = json.loads(stored.read_text(encoding="utf-8"))
        env_edit = client.put("/api/providers/lm_studio", json=fields)
        removed = client.delete(f"/api/providers/{created['id']}")
        after = [item["id"] for item in client.get("/api/providers").json()]

    assert wrong.status_code == 502
    assert probe["models"] == ["gpt-4.1-mini", "Qwen3-32B"] and again["models"] == probe["models"]
    item = listed[created["id"]]
    assert item["editable"] and item["key"] == "…7890" and "sk-work" not in json.dumps(listed)
    assert listed["lm_studio"]["editable"] is False
    assert models["models"] == ["gpt-4.1-mini", "Qwen3-32B"]
    assert all(header == "Bearer sk-work-1234567890" for url, header in seen[1:])
    # Ключ лежит в файле, доступном только владельцу; имя поменялось, ключ остался.
    assert saved["providers"][0]["api_key"] == "sk-work-1234567890" and saved["providers"][0]["name"] == "Работа (офис)"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    # Модель по умолчанию — для новых проектов.
    assert default.status_code == 200 and config["default_provider"] == created["id"] and config["default_model"] == "Qwen3-32B"
    assert project["provider"] == created["id"] and project["model"] == "Qwen3-32B"
    assert env_edit.status_code == 404 and removed.status_code == 204 and created["id"] not in after


def test_ollama_without_key_sends_no_authorization(tmp_path):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": [{"id": "gemma4:12b"}]})

    with TestClient(create_app(settings(tmp_path), llm_provider=ScriptedProvider())) as client:
        client.app.state.provider_transport = httpx.MockTransport(handle)
        probe = client.post("/api/providers/probe", json={"name": "Дом", "kind": "ollama", "base_url": "http://127.0.0.1:11434/v1"})

    assert probe.json()["models"] == ["gemma4:12b"] and seen == [None]


def test_probe_uses_ssl_verification_setting(tmp_path, monkeypatch):
    import local_agent.llm.openai_compatible as adapter

    real_client = adapter.httpx.AsyncClient
    verification_options = []

    def capture_client(*args, **kwargs):
        verification_options.append(kwargs.get("verify"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(adapter.httpx, "AsyncClient", capture_client)
    configured = settings(tmp_path)
    configured.llm_verify_ssl = False

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "corporate-model"}]})

    with TestClient(create_app(configured, llm_provider=ScriptedProvider())) as client:
        client.app.state.provider_transport = httpx.MockTransport(respond)
        response = client.post(
            "/api/providers/probe",
            json={"name": "Работа", "kind": "openai", "base_url": "https://llm.example.com/v1"},
        )

    assert response.json() == {"models": ["corporate-model"]}
    assert verification_options[-1] is False
