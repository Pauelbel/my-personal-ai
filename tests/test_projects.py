"""Проекты: папка и инструменты общие для сессий проекта, доступ к файлам не утекает за его пределы."""

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def settings(tmp_path: Path) -> Settings:
    return Settings(
        agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory",
        projects_path=tmp_path / "projects", skills_path=tmp_path / "skills", _env_file=None,
    )


def folder(tmp_path: Path, name: str = "code") -> Path:
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    return path


def test_sessions_share_project_folder_tools_and_last_model(tmp_path):
    workspace = folder(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace), "model": "m1"}).json()
        first = client.post("/api/sessions", json={"project_id": project["id"]}).json()
        client.put(f"/api/sessions/{first['id']}/tools/write_file", json={"enabled": True})
        client.put(f"/api/sessions/{first['id']}/tools/git", json={"enabled": False})
        client.put(f"/api/sessions/{first['id']}/config", json={"provider": "lm_studio", "model": "m2"})
        second = client.post("/api/sessions", json={"project_id": project["id"]}).json()
        second_tools = client.get(f"/api/sessions/{second['id']}/tools").json()

    assert first["workspace"] == str(workspace.resolve())
    assert second["workspace"] == str(workspace.resolve())
    assert second["model"] == "m2"
    # Чтение включено у нового проекта сразу; переключатели в одной сессии видны во всех.
    assert [tool["id"] for tool in second_tools if tool["enabled"]] == [
        "list_files", "read_file", "search_files", "write_file", "search_docs"
    ]


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


def test_legacy_git_permissions_become_one_toggle(tmp_path):
    workspace = folder(tmp_path)
    configured = settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace)}).json()
        session = client.post("/api/sessions", json={"project_id": project["id"]}).json()
    path = tmp_path / "projects" / f"{project['id']}.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["enabled_tools"] = ["git_log", "git_show", "read_file"]
    path.write_text(json.dumps(saved), encoding="utf-8")
    with TestClient(create_app(configured)) as client:
        tools = client.get(f"/api/sessions/{session['id']}/tools").json()
        assert [tool["id"] for tool in tools if tool["enabled"]] == ["read_file", "git"]
        result = client.put(f"/api/sessions/{session['id']}/tools/git", json={"enabled": False})
        assert result.status_code == 200 and result.json()["enabled"] is False
    with TestClient(create_app(configured)) as client:
        tools = client.get(f"/api/sessions/{session['id']}/tools").json()
        assert [tool["id"] for tool in tools if tool["enabled"]] == ["read_file"]


def test_deleting_project_keeps_sessions_without_file_access(tmp_path):
    workspace = folder(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace)}).json()
        session = client.post("/api/sessions", json={"project_id": project["id"]}).json()
        client.put(f"/api/sessions/{session['id']}/tools/read_file", json={"enabled": True})
        deleted = client.delete(f"/api/projects/{project['id']}")
        after = client.get(f"/api/sessions/{session['id']}").json()
        projects = client.get("/api/projects").json()

    assert deleted.status_code == 204
    assert projects == []
    assert after["project_id"] is None
    assert after["workspace"] is None
    assert after["enabled_tools"] == []
    assert workspace.exists()


def test_folder_without_project_moves_session_into_project(tmp_path):
    workspace = folder(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        created = client.post("/api/sessions", json={"workspace": str(workspace)}).json()
        plain = client.post("/api/sessions", json={}).json()
        configured = client.put(
            f"/api/sessions/{plain['id']}/config",
            json={"provider": "lm_studio", "model": "m", "workspace": str(workspace)},
        ).json()
        projects = client.get("/api/projects").json()

    assert len(projects) == 1 and projects[0]["name"] == "code"
    assert created["project_id"] == configured["project_id"] == projects[0]["id"]
    assert plain["project_id"] is None and plain["workspace"] is None


def test_legacy_session_with_folder_is_adopted_on_start(tmp_path):
    workspace = folder(tmp_path)
    (tmp_path / "sessions").mkdir()
    now = datetime.now(UTC).isoformat()
    (tmp_path / "sessions" / "old.json").write_text(json.dumps({
        "id": "old", "title": "Старая", "created_at": now, "updated_at": now, "agent_id": "default",
        "model": "m", "provider": "lm_studio", "workspace": str(workspace), "enabled_tools": ["read_file"],
    }), encoding="utf-8")

    with TestClient(create_app(settings(tmp_path))) as client:
        session = client.get("/api/sessions/old").json()
        projects = client.get("/api/projects").json()
    with TestClient(create_app(settings(tmp_path))) as client:
        again = client.get("/api/projects").json()

    assert len(projects) == 1 and projects[0]["enabled_tools"] == ["read_file"]
    assert session["project_id"] == projects[0]["id"]
    assert session["workspace"] == str(workspace.resolve())
    assert again == projects


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
