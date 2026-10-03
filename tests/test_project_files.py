"""Просмотр файлов проекта: дерево и содержимое отдаются только изнутри рабочей папки."""

from pathlib import Path

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from tests.test_projects import folder, settings


def test_project_files_are_listed_and_read_inside_workspace(tmp_path: Path):
    workspace = folder(tmp_path)
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("print(1)", encoding="utf-8")
    (workspace / "README.md").write_text("# Привет", encoding="utf-8")
    (workspace / "blob.bin").write_bytes(b"\x00\xff\x00")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    with TestClient(create_app(settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace)}).json()
        base = f"/api/projects/{project['id']}/files"
        root = client.get(base).json()
        nested = client.get(base, params={"path": "src"}).json()
        text = client.get(f"{base}/content", params={"path": "src/a.py"}).json()
        binary = client.get(f"{base}/content", params={"path": "blob.bin"})
        outside = client.get(f"{base}/content", params={"path": "../secret.txt"})
        absolute = client.get(f"{base}/content", params={"path": str(tmp_path / "secret.txt")})
        missing = client.get("/api/projects/nope/files")

    assert [(item["name"], item["is_dir"]) for item in root] == [
        ("src", True), ("blob.bin", False), ("README.md", False)
    ]
    assert nested == [{"name": "a.py", "path": "src/a.py", "is_dir": False}]
    assert text["content"] == "print(1)"
    assert binary.status_code == 415
    assert outside.status_code == 400
    assert absolute.status_code == 400
    assert missing.status_code == 404


def test_project_file_can_be_edited_only_inside_workspace(tmp_path: Path):
    workspace = folder(tmp_path)
    (workspace / "note.md").write_text("old", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    with TestClient(create_app(settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace)}).json()
        url = f"/api/projects/{project['id']}/files/content"
        saved = client.put(url, params={"path": "note.md"}, json={"content": "new\nтекст"})
        escaped = client.put(url, params={"path": "../secret.txt"}, json={"content": "x"})
        created = client.put(url, params={"path": "fresh.md"}, json={"content": "x"})

    assert saved.status_code == 200
    assert (workspace / "note.md").read_text(encoding="utf-8") == "new\nтекст"
    assert escaped.status_code == 400 and outside.read_text(encoding="utf-8") == "secret"
    assert created.status_code == 404 and not (workspace / "fresh.md").exists()


def test_project_entries_are_moved_and_deleted_only_inside_workspace(tmp_path: Path):
    workspace = folder(tmp_path)
    (workspace / "docs").mkdir()
    (workspace / "docs" / "a.md").write_text("a", encoding="utf-8")
    (workspace / "b.md").write_text("b", encoding="utf-8")
    (workspace / "old").mkdir()
    (workspace / "old" / "x.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    with TestClient(create_app(settings(tmp_path))) as client:
        project = client.post("/api/projects", json={"name": "Код", "workspace": str(workspace)}).json()
        base = f"/api/projects/{project['id']}/files"
        renamed = client.post(f"{base}/move", params={"path": "b.md"}, json={"new_path": "c.md"})
        moved = client.post(f"{base}/move", params={"path": "c.md"}, json={"new_path": "docs/c.md"})
        taken = client.post(f"{base}/move", params={"path": "docs/c.md"}, json={"new_path": "docs/a.md"})
        into_self = client.post(f"{base}/move", params={"path": "docs"}, json={"new_path": "docs/inner"})
        out_of_root = client.post(f"{base}/move", params={"path": "docs/a.md"}, json={"new_path": "../a.md"})
        steal = client.post(f"{base}/move", params={"path": "../secret.txt"}, json={"new_path": "s.txt"})
        root_delete = client.delete(base, params={"path": "."})
        outside_delete = client.delete(base, params={"path": "../secret.txt"})
        deleted_file = client.delete(base, params={"path": "docs/c.md"})
        deleted_folder = client.delete(base, params={"path": "old"})
        missing = client.delete(base, params={"path": "nope"})

    assert renamed.status_code == 200 and moved.json() == {"name": "c.md", "path": "docs/c.md", "is_dir": False}
    assert taken.status_code == 409
    assert into_self.status_code == 400
    assert out_of_root.status_code == 400 and not (tmp_path / "a.md").exists()
    assert steal.status_code == 400 and outside.exists()
    assert root_delete.status_code == 400 and outside_delete.status_code == 400
    assert deleted_file.status_code == 204 and not (workspace / "docs" / "c.md").exists()
    assert deleted_folder.status_code == 204 and not (workspace / "old").exists()
    assert missing.status_code == 404
    assert (workspace / "docs" / "a.md").exists() and outside.exists()
