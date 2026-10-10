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

    assert first["project_id"] == second["project_id"] == project["id"]
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


def test_deleting_project_moves_sessions_to_drafts(tmp_path):
    workspace = folder(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace)}).json()
        session = client.post("/api/sessions", json={"project_id": project["id"]}).json()
        client.put(f"/api/sessions/{session['id']}/tools/read_file", json={"enabled": True})
        deleted = client.delete(f"/api/projects/{project['id']}")
        after = client.get(f"/api/sessions/{session['id']}").json()
        projects = client.get("/api/projects").json()

    assert deleted.status_code == 204
    assert [item["id"] for item in projects] == ["drafts"]
    # В «Черновиках» действуют их папка и инструменты, а доступа к папке удалённого проекта больше нет.
    assert after["project_id"] == "drafts"
    assert after["enabled_tools"] == []
    assert workspace.exists()


def test_drafts_can_be_renamed_but_not_deleted(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        drafts = client.get("/api/projects").json()[0]
        deleted = client.delete("/api/projects/drafts")
        renamed = client.put("/api/projects/drafts", json={"name": "Наброски", "workspace": drafts["workspace"]})
    with TestClient(create_app(settings(tmp_path))) as client:
        projects = client.get("/api/projects").json()

    assert deleted.status_code == 400 and "нельзя удалить" in deleted.json()["detail"]
    assert renamed.status_code == 200
    # Переименованные «Черновики» при перезапуске не создаются заново.
    assert [(item["id"], item["name"]) for item in projects] == [("drafts", "Наброски")]


def test_session_folder_is_not_changed_from_session_config(tmp_path):
    workspace = folder(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        session = client.post("/api/sessions", json={}).json()
        configured = client.put(
            f"/api/sessions/{session['id']}/config",
            json={"provider": "lm_studio", "model": "m", "workspace": str(workspace)},
        ).json()
        projects = client.get("/api/projects").json()

    # Папку меняют только в настройках проекта: сессия остаётся в «Черновиках», нового проекта нет.
    assert configured["project_id"] == "drafts" and configured["model"] == "m"
    assert [item["id"] for item in projects] == ["drafts"]


def write_session(tmp_path: Path, session_id: str, **fields) -> None:
    now = datetime.now(UTC).isoformat()
    (tmp_path / "sessions").mkdir(exist_ok=True)
    (tmp_path / "sessions" / f"{session_id}.json").write_text(json.dumps({
        "id": session_id, "title": session_id, "created_at": now, "updated_at": now, "agent_id": "default",
        "model": "m", "provider": "lm_studio", "enabled_tools": [], **fields,
    }), encoding="utf-8")


def test_legacy_sessions_get_a_project_on_start(tmp_path):
    workspace = folder(tmp_path)
    # 1. Своя папка → проект этой папки, с инструментами сессии.
    write_session(tmp_path, "with-folder", workspace=str(workspace), enabled_tools=["read_file"])
    # 2. Ни проекта, ни папки → «Черновики».
    write_session(tmp_path, "plain", workspace=None)
    # 3. Проекта, на который ссылается сессия, больше нет → «Черновики».
    write_session(tmp_path, "orphan", workspace=None, project_id="gone")
    # Своя папка, которой уже нет на диске, тоже не теряет сессию.
    write_session(tmp_path, "lost-folder", workspace=str(tmp_path / "removed"))
    # Скрытая сессия агента с холста идёт за своей сессией-хозяйкой.
    write_session(tmp_path, "node", workspace=None, parent_id="with-folder", node_id="qa", hidden=True)

    with TestClient(create_app(settings(tmp_path))) as client:
        sessions = {item["id"]: item for item in client.get("/api/sessions", params={"include_hidden": True}).json()}
        projects = client.get("/api/projects").json()
    with TestClient(create_app(settings(tmp_path))) as client:
        again = client.get("/api/projects").json()

    [project] = [item for item in projects if item["id"] != "drafts"]
    assert project["workspace"] == str(workspace.resolve()) and project["enabled_tools"] == ["read_file"]
    assert {key: item["project_id"] for key, item in sessions.items()} == {
        "with-folder": project["id"], "plain": "drafts", "orphan": "drafts", "lost-folder": "drafts",
        "node": project["id"],
    }
    assert again == projects
    # Устаревшая папка сессии не отдаётся в API и стирается из файла при миграции.
    assert all("workspace" not in item for item in sessions.values())
    stored = json.loads((tmp_path / "sessions" / "with-folder.json").read_text(encoding="utf-8"))
    assert "workspace" not in stored


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
