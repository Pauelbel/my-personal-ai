"""Проверка сессий подтверждает API и сохранение данных между запусками."""

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def test_session_persists_after_app_restart(tmp_path) -> None:
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", projects_path=tmp_path / "projects", _env_file=None)
    (tmp_path / "code").mkdir()

    with TestClient(create_app(settings)) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(tmp_path / "code")}).json()
        created = client.post(
            "/api/sessions",
            json={"title": "Первый проект", "project_id": project["id"]},
        )
        assert created.status_code == 201
        session = created.json()
        assert session["title"] == "Первый проект"
        assert session["agent_id"] == "default"
        assert session["project_id"] == project["id"]
        # Папка у сессии одна — папка проекта; своей папки сессия в API не отдаёт.
        assert "workspace" not in session

    with TestClient(create_app(settings)) as client:
        fetched = client.get(f"/api/sessions/{session['id']}")
        listed = client.get("/api/sessions")
        missing = client.get("/api/sessions/does-not-exist")

    assert fetched.status_code == 200
    assert fetched.json() == session
    assert listed.status_code == 200
    assert listed.json() == [session]
    assert missing.status_code == 404


def test_session_without_project_goes_to_drafts(tmp_path) -> None:
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", projects_path=tmp_path / "projects", drafts_path=tmp_path / "drafts", _env_file=None)

    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={}).json()
        # Старое поле папки в запросе больше ничего не значит: сессия всё равно попадает в «Черновики».
        with_folder = client.post("/api/sessions", json={"workspace": str(tmp_path)}).json()
        unknown = client.post("/api/sessions", json={"project_id": "nope"})
        projects = client.get("/api/projects").json()
        tools = client.get(f"/api/sessions/{session['id']}/tools").json()

    assert session["project_id"] == with_folder["project_id"] == "drafts"
    assert unknown.status_code == 400
    [drafts] = projects
    assert drafts["id"] == "drafts" and drafts["name"] == "Черновики"
    assert drafts["workspace"] == str((tmp_path / "drafts").resolve())
    assert (tmp_path / "drafts").is_dir()
    # В черновиках агент не читает файлы, пока пользователь сам не включит инструменты.
    assert drafts["enabled_tools"] == []
    assert not any(tool["enabled"] for tool in tools)

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/projects").json() == projects


def test_session_can_be_renamed(tmp_path) -> None:
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)

    with TestClient(create_app(settings)) as client:
        session = client.post("/api/sessions", json={}).json()
        renamed = client.put(
            f"/api/sessions/{session['id']}/title", json={"title": "  Мой проект  "}
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Мой проект"
        assert client.put(
            f"/api/sessions/{session['id']}/title", json={"title": "   "}
        ).status_code == 422
        assert client.post(
            f"/api/sessions/{session['id']}/messages",
            json={"content": "Первое сообщение"},
        ).status_code == 201
        assert client.get(f"/api/sessions/{session['id']}").json()["title"] == "Мой проект"

    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/sessions/{session['id']}").json()["title"] == "Мой проект"


def test_deleting_session_removes_only_its_messages(tmp_path) -> None:
    settings = Settings(agents_path=tmp_path / "agents", sessions_path=tmp_path / "sessions", conversations_path=tmp_path / "conversations", memory_path=tmp_path / "memory", _env_file=None)

    with TestClient(create_app(settings)) as client:
        first = client.post("/api/sessions", json={}).json()
        second = client.post("/api/sessions", json={}).json()
        for session in (first, second):
            assert client.post(
                f"/api/sessions/{session['id']}/messages",
                json={"content": "Тестовое сообщение"},
            ).status_code == 201

        deleted = client.delete(f"/api/sessions/{first['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/sessions/{first['id']}").status_code == 404
        assert client.get(f"/api/sessions/{first['id']}/messages").status_code == 404
        assert client.delete(f"/api/sessions/{first['id']}").status_code == 404
        assert client.get(f"/api/sessions/{second['id']}/messages").status_code == 200

    session_files = sorted(settings.sessions_path.glob("*.json"))
    assert [path.stem for path in session_files] == [second["id"]]
    assert not (settings.conversations_path / f"{first['id']}.jsonl").exists()
    assert (settings.conversations_path / f"{second['id']}.jsonl").exists()
