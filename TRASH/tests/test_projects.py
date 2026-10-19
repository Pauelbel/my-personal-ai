"""Проекты: папка и инструменты общие для сессий проекта, доступ к файлам не утекает за его пределы."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from tests.fakes import post_session

from api.app import create_app
from config.settings import Settings


def settings(tmp_path: Path) -> Settings:
    return Settings(
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory",
        projects_path=tmp_path / "projects", _env_file=None,
    )


def folder(tmp_path: Path, name: str = "code") -> Path:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


def test_sessions_share_project_folder_and_last_model(tmp_path):
    workspace = folder(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace), "model": "m1"}).json()
        first = post_session(client, {"project_id": project["id"]}).json()
        client.put(f"/api/sessions/{first['id']}/config", json={"provider": "lm_studio", "model": "m2"})
        second = post_session(client, {"project_id": project["id"]}).json()

    assert first["project_id"] == second["project_id"] == project["id"]
    assert second["model"] == "m2"


def test_project_folder_must_exist_be_absolute_and_unique(tmp_path):
    workspace = folder(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.post("/api/projects", json={"name": "A", "workspace": str(workspace)}).status_code == 201
        duplicate = client.post("/api/projects", json={"name": "B", "workspace": str(workspace)})
        missing = client.post("/api/projects", json={"name": "C", "workspace": str(tmp_path / "nope")})
        relative = client.post("/api/projects", json={"name": "D", "workspace": "code"})

    assert duplicate.status_code == 400 and "уже есть проект" in duplicate.json()["detail"]
    assert missing.status_code == 400
    assert relative.status_code == 400


def test_deleting_project_deletes_its_sessions_but_not_the_folder(tmp_path):
    workspace = folder(tmp_path)
    other = folder(tmp_path, "other")
    with TestClient(create_app(settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace)}).json()
        kept = client.post("/api/projects", json={"workspace": str(other)}).json()
        session = post_session(client, {"project_id": project["id"]}).json()
        survivor = post_session(client, {"project_id": kept["id"]}).json()
        deleted = client.delete(f"/api/projects/{project['id']}")
        gone = client.get(f"/api/sessions/{session['id']}")
        sessions = client.get("/api/sessions").json()
        projects = client.get("/api/projects").json()

    assert deleted.status_code == 204 and gone.status_code == 404
    assert [item["id"] for item in sessions] == [survivor["id"]]
    assert [item["id"] for item in projects] == [kept["id"]]
    assert workspace.exists()


def test_no_system_projects(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get("/api/projects").json() == []


def test_session_folder_is_not_changed_from_session_config(tmp_path):
    workspace = folder(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        session = post_session(client, {}).json()
        configured = client.put(
            f"/api/sessions/{session['id']}/config",
            json={"provider": "lm_studio", "model": "m", "workspace": str(workspace)},
        ).json()
        projects = client.get("/api/projects").json()

    # Папку меняют только в настройках проекта: сессия остаётся в своём проекте, нового проекта нет.
    assert configured["project_id"] == session["project_id"] and configured["model"] == "m"
    assert [item["id"] for item in projects] == [session["project_id"]]


def test_folder_browser_lists_only_folders(tmp_path):
    root = folder(tmp_path, "root")
    (root / "src").mkdir()
    (root / ".git").mkdir()
    (root / "secret.txt").write_text("пароль", encoding="utf-8")
    with TestClient(create_app(settings(tmp_path))) as client:
        listing = client.get("/api/folders", params={"path": str(root)}).json()
        roots = client.get("/api/folders").json()
        relative = client.get("/api/folders", params={"path": "root"})
        missing = client.get("/api/folders", params={"path": str(root / "nope")})
        file = client.get("/api/folders", params={"path": str(root / "secret.txt")})

    assert [item["name"] for item in listing["folders"]] == ["src"]
    assert "secret" not in json.dumps(listing)
    assert listing["parent"] == str(root.resolve().parent)
    assert roots["path"] is None and any(item["path"] == str(Path.home()) for item in roots["folders"])
    assert relative.status_code == 400
    assert missing.status_code == 404
    assert file.status_code == 400


def test_project_is_named_after_its_folder_by_default(tmp_path):
    workspace = folder(tmp_path, "job_wifi")
    other = folder(tmp_path, "my-personal-ai")
    with TestClient(create_app(settings(tmp_path))) as client:
        created = client.post("/api/projects", json={"workspace": str(workspace)}).json()
        renamed = client.put(f"/api/projects/{created['id']}", json={"name": "  ", "workspace": str(other)}).json()

    assert created["name"] == "job_wifi" and renamed["name"] == "my-personal-ai"
