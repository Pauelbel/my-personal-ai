"""Стартовые скиллы и помощник meepo_skill: на фейковом OpenAI-совместимом сервере, без настоящей модели."""

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from meepo.app import BUILTIN_SKILLS
from meepo.skills import runner
from meepo.skills.registry import SkillRegistry, check_files

DOCS = "# Вход\n\n## Авторизация\n\nПользователь вводит логин и пароль и нажимает «Войти». " + "Пустые поля подсвечиваются. " * 30

CASE = {"title": "Авторизация пользователя", "priority": "High", "preconditions": ["Сервис запущен."],
        "steps": [{"action": "Откройте страницу входа.", "test_data": "—", "expected": "Открывается форма входа."}],
        "questions": "Какой лимит попыток?"}


class FakeModel:
    """OpenAI-совместимый сервер: на схему test_cases — кейсы, на схему ревью — замечания, иначе — текст."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.fail_after: int | None = None
        model = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.requests.append(body)
                if model.fail_after is not None and len(model.requests) > model.fail_after:
                    self.send_response(500)
                    self.end_headers()
                    return
                schema = (body.get("response_format") or {}).get("json_schema", {}).get("schema", {})
                if "test_cases" in schema.get("properties", {}):
                    content = json.dumps({"test_cases": [CASE]}, ensure_ascii=False)
                elif "remarks" in schema.get("properties", {}):
                    content = json.dumps({"remarks": [{"case": "TC-001", "issue": "Нет негативной проверки."}],
                                          "questions": [{"case": "TC-001", "question": "Сколько попыток входа?"}],
                                          "verdict": "Можно отдавать после правки TC-001."}, ensure_ascii=False)
                else:
                    content = "Дублей нет"
                data = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"


@pytest.fixture
def model():
    fake = FakeModel()
    yield fake
    fake.server.shutdown()


def run_skill(tmp_path: Path, name: str, args: dict, model: FakeModel) -> tuple[str, bool]:
    registry = SkillRegistry(tmp_path / "skills", BUILTIN_SKILLS)
    registry.install_builtin()
    skill = registry.get(name)
    project = tmp_path / "project"
    env = runner.environment(project, skill, {"url": model.url, "model": "fake"}, {})
    return asyncio.run(runner.run(skill, args, project=project, python=Path(sys.executable), env=env,
                                  on_progress=lambda line: None))


def test_builtin_skills_are_valid(tmp_path):
    registry = SkillRegistry(tmp_path / "skills", BUILTIN_SKILLS)
    registry.install_builtin()
    skills = {skill.name: skill for skill in registry.list()}
    assert set(skills) == {"testcase-generate", "testcase-review", "to-markdown"}
    for skill in skills.values():
        assert not skill.error, skill.error
        assert check_files(skill.name, registry.read_files(skill.name)) is None
    assert skills["to-markdown"].requires == ["markitdown[pdf,docx,xlsx,pptx]"]
    # Удалённый стартовый скилл при следующем старте не возвращается.
    import shutil
    shutil.rmtree(tmp_path / "skills" / "to-markdown")
    registry.install_builtin()
    assert registry.get("to-markdown") is None


def test_generate_then_review(tmp_path, model):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "auth.md").write_text(DOCS, encoding="utf-8")
    text, failed = run_skill(tmp_path, "testcase-generate", {"path": "docs/auth.md"}, model)
    assert not failed, text
    cases_file = project / "testgen/test_cases/docs/auth.md"
    table = cases_file.read_text(encoding="utf-8")
    assert "| TC-001 | Авторизация пользователя | High |" in table and "Какой лимит попыток?" in table
    assert "testcase-review" in text
    # Повторный запуск ничего не генерирует заново.
    before = len(model.requests)
    text, failed = run_skill(tmp_path, "testcase-generate", {"path": "docs"}, model)
    assert not failed and len(model.requests) == before and "Новых страниц для генерации нет" in text

    text, failed = run_skill(tmp_path, "testcase-review", {"cases": "testgen/test_cases/docs/auth.md", "docs": "docs/auth.md"}, model)
    assert not failed, text
    review = (project / "testgen/test_cases/docs/auth.review.md").read_text(encoding="utf-8")
    assert "**TC-001**: Нет негативной проверки." in review and "Вывод: Можно отдавать" in review
    assert "<документация>" in model.requests[-1]["messages"][1]["content"]
    # Вопрос ревьюера дописан в колонку «Вопросы», прежний вопрос остался.
    updated = cases_file.read_text(encoding="utf-8")
    assert "Какой лимит попыток?<br>Сколько попыток входа?" in updated
    # Повторное ревью не дублирует вопрос и не зовёт модель: файл с вопросами — те же кейсы.
    before = len(model.requests)
    run_skill(tmp_path, "testcase-review", {"cases": "testgen/test_cases/docs/auth.md", "docs": "docs/auth.md"}, model)
    assert len(model.requests) == before
    assert cases_file.read_text(encoding="utf-8").count("Сколько попыток входа?") == 1


def test_review_any_markdown_without_docs_resumes(tmp_path, model):
    project = tmp_path / "project"
    (project / "cases").mkdir(parents=True)
    rows = "\n".join(f"| TC-{n:03d} | Кейс {n} | Откройте страницу {n}. | Страница открыта. | |" for n in range(1, 26))
    (project / "cases/login.md").write_text(
        f"# Мои кейсы\n\n| ID | Название | Шаги | Ожидаемый результат | Вопросы |\n|---|---|---|---|---|\n{rows}\n", encoding="utf-8")
    model.fail_after = 1
    text, failed = run_skill(tmp_path, "testcase-review", {"cases": "cases/login.md"}, model)
    assert failed and "Повторите запуск" in text
    model.fail_after = None
    done_before = len(model.requests)
    text, failed = run_skill(tmp_path, "testcase-review", {"cases": "cases"}, model)
    assert not failed, text
    # 25 кейсов — 3 пачки по 10; первая уже была готова, поэтому запросов: 2 пачки + поиск дублей.
    assert len(model.requests) - done_before == 3
    first = model.requests[0]["messages"]
    assert "Документации нет" in first[0]["content"] and "<документация>" not in first[1]["content"]
    assert "Вопросы" not in first[1]["content"]
    assert "Дубли по всему файлу" in (project / "cases/login.review.md").read_text(encoding="utf-8")
