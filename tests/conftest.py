"""TestClient ходит с Host: testserver, поэтому разрешаем его только в тестах."""

import os
import tempfile

import pytest

os.environ.setdefault("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
# Тесты не передают skills_path явно: без этого стартовые навыки копировались бы в data/skills проекта.
# Папка существует и пуста, поэтому навыков нет и use_skill не предлагается; навыки проверяются отдельно.
os.environ.setdefault("SKILLS_PATH", tempfile.mkdtemp(prefix="skills-"))
# То же для проектов и папки «Черновиков»: тесты, которым важен список проектов, передают projects_path явно.
os.environ.setdefault("PROJECTS_PATH", tempfile.mkdtemp(prefix="projects-"))
os.environ.setdefault("DRAFTS_PATH", tempfile.mkdtemp(prefix="drafts-"))


@pytest.fixture(autouse=True)
def separate_projects(monkeypatch, tmp_path_factory):
    """Сессии без проекта попадают в общие «Черновики», а их инструменты общие для всех их сессий:
    у каждого теста свои проекты, иначе включённый в одном тесте инструмент достался бы другому."""
    monkeypatch.setenv("PROJECTS_PATH", str(tmp_path_factory.mktemp("projects")))
    monkeypatch.setenv("DRAFTS_PATH", str(tmp_path_factory.mktemp("drafts")))
