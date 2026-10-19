"""Скиллы: создание с проверками, запуск (stdin/stdout/stderr, таймаут), история и откат, секреты."""

import asyncio
import sys
import time

from fastapi.testclient import TestClient

from meepo.app import create_app
from meepo.llm.fake import FakeLLM
from meepo.settings import Env
from meepo.skills import runner
from meepo.skills.env import check_requirements
from meepo.skills.registry import Skill

YES = {"action": "approve"}

SKILL_MD = """---
name: echo
description: Повторяет аргументы. Применять для проверки.
entry: scripts/main.py
requires: []
secrets: []
timeout: 30
---
# Что делает
Печатает аргументы.
"""

ECHO = """import json, os, sys
args = json.load(sys.stdin)
print("шаг 1 из 2", file=sys.stderr, flush=True)
print("шаг 2 из 2", file=sys.stderr, flush=True)
print("аргументы:", json.dumps(args, ensure_ascii=False), "токен:", os.environ.get("ECHO_TOKEN", "-"),
      "состояние:", os.path.isdir(os.environ["MEEPO_STATE_DIR"]))
"""


class FakeEnv:
    """Окружение скиллов без создания venv: интерпретатор приложения, пакеты «установлены»."""

    python = sys.executable

    def missing(self, requirements):
        return []

    def packages(self):
        return {}

    async def ensure(self):
        pass

    async def install(self, requirements):
        return ""


def create_call(name="echo", skill_md=SKILL_MD, code=ECHO, **extra):
    return ("create_skill", {"name": name, "reason": "нужно для проверки", "skill_md": skill_md,
                             "files": {"scripts/main.py": code}, **extra})


def with_env(h):
    h.services.agent.services["env"] = FakeEnv()
    h.services.skills_env = FakeEnv()
    return h


def test_create_and_run_skill(harness):
    h = with_env(harness([
        {"calls": [create_call()]},
        {"calls": [("run_skill", {"name": "echo", "args": {"path": "docs"}})]},
        "Готово.",
    ]))
    h.turn("сделай скилл и запусти", [YES, YES])
    create_card, run_card = h.cards()
    assert create_card["skill"]["files"]["scripts/main.py"] == ECHO
    assert create_card["group"] == "skill_edit" and run_card["group"] == "skill_run"
    assert (h.services.skills.root / "echo" / "SKILL.md").is_file()
    result = [m for m in h.messages() if m["role"] == "tool"][-1]
    assert not result.get("is_error"), result["content"]
    assert 'аргументы: {"path": "docs"}' in result["content"] and "состояние: True" in result["content"]
    progress = [event["text"] for event in h.events if event["type"] == "progress"]
    assert progress == ["шаг 1 из 2", "шаг 2 из 2"]
    # Новый скилл виден в системном промпте следующего запроса.
    assert "- echo — Повторяет аргументы" in h.llm.requests[-1]["messages"][0]["content"]


def test_bad_name_and_package_rejected_before_card(harness):
    h = with_env(harness([
        {"calls": [create_call(name="Плохое имя"), create_call(requires=["https://evil.example/pkg.whl"])]},
        "Понял.",
    ]))
    h.turn("сделай скилл")
    assert h.cards() == []
    first, second = [m["content"] for m in h.messages() if m["role"] == "tool"]
    assert "name — латиница" in first
    assert "не имена пакетов PyPI" in second
    assert check_requirements(["--index-url=x"]) and check_requirements(["-e ."]) and not check_requirements(["pypdf==5.1"])


def test_syntax_error_and_missing_entry(harness):
    h = with_env(harness([
        {"calls": [create_call(code="def broken(:\n"), create_call(skill_md=SKILL_MD.replace("scripts/main.py", "scripts/x.py"))]},
        "Понял.",
    ]))
    h.turn("сделай скилл")
    first, second = [m["content"] for m in h.messages() if m["role"] == "tool"]
    assert "Синтаксическая ошибка в scripts/main.py" in first
    assert "Нет файла entry scripts/x.py" in second


def test_run_requires_read_first(harness):
    h = with_env(harness([{"calls": [("run_skill", {"name": "echo"})]}, "Ок."]))
    h.services.skills.write("echo", {"SKILL.md": SKILL_MD, "scripts/main.py": ECHO}, replace=False)
    h.turn("запусти echo")
    assert "Сначала прочитай скилл" in [m for m in h.messages() if m["role"] == "tool"][0]["content"]


def test_timeout_kills_process_tree(tmp_path):
    folder = tmp_path / "slow"
    (folder / "scripts").mkdir(parents=True)
    marker = tmp_path / "child-alive"
    # Скрипт запускает дочерний процесс, который через 3 с создаст файл: убитое дерево его не создаст.
    (folder / "scripts" / "child.py").write_text(
        f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    (folder / "scripts" / "main.py").write_text(
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, str(pathlib.Path(__file__).with_name('child.py'))])\n"
        "time.sleep(60)\n", encoding="utf-8")
    skill = Skill("slow", folder, timeout=1)
    project = tmp_path / "project"
    project.mkdir()
    started = time.monotonic()
    text, failed = asyncio.run(runner.run(skill, {}, project=project, python=sys.executable,
                                          env=runner.environment(project, skill, {}, {}), on_progress=lambda line: None))
    assert failed and "не уложился в 1 с" in text
    assert time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists()


def test_long_output_goes_to_file(tmp_path):
    folder = tmp_path / "big"
    (folder / "scripts").mkdir(parents=True)
    (folder / "scripts" / "main.py").write_text("print('начало' + 'x' * 20000 + 'конец')\n", encoding="utf-8")
    skill = Skill("big", folder)
    project = tmp_path / "project"
    project.mkdir()
    text, failed = asyncio.run(runner.run(skill, {}, project=project, python=sys.executable,
                                          env=runner.environment(project, skill, {}, {}), on_progress=lambda line: None))
    assert not failed and text.startswith("начало") and text.endswith("конец")
    assert ".meepo/skills/big/last-output.md" in text and len(text) < 8000
    assert (project / ".meepo/skills/big/last-output.md").read_text(encoding="utf-8").startswith("начало")


def test_update_goes_to_history_and_rollback(harness):
    new_code = ECHO.replace("аргументы:", "новые аргументы:")
    h = with_env(harness([
        {"calls": [("read_skill", {"name": "echo"})]},
        {"calls": [("update_skill", {"name": "echo", "reason": "переименовать вывод",
                                     "files": {"scripts/main.py": new_code}})]},
        "Готово.",
    ]))
    h.services.skills.write("echo", {"SKILL.md": SKILL_MD, "scripts/main.py": ECHO}, replace=False)
    h.turn("поправь echo", [YES])
    assert '+print("новые аргументы:"' in h.cards()[0]["diff"]
    folder = h.services.skills.root / "echo"
    assert "новые аргументы" in (folder / "scripts/main.py").read_text(encoding="utf-8")
    (version,) = h.services.history.versions("echo")
    with TestClient(create_app(h.services.env, FakeLLM([])), base_url="http://127.0.0.1") as client:
        detail = client.post("/api/skills/echo/rollback", json={"version": version}).json()
        assert "новые аргументы" not in detail["files"]["scripts/main.py"]
        assert len(detail["history"]) == 2


def test_three_failed_fixes_stop(harness):
    calls = [{"calls": [("update_skill", {"name": "echo", "reason": "r", "files": {"scripts/main.py": ECHO + f"\n# {n}\n"}})]}
             for n in range(4)]
    h = with_env(harness([{"calls": [("read_skill", {"name": "echo"})]}, *calls, "Три правки не помогли — как быть?"]))
    h.services.skills.write("echo", {"SKILL.md": SKILL_MD, "scripts/main.py": ECHO}, replace=False)
    h.turn("чини", [{"action": "approve", "trust": True}])
    results = [m["content"] for m in h.messages() if m["role"] == "tool"]
    assert "3 правки скилла echo подряд не помогли" in results[-1]


def test_secrets_never_leave_server(harness):
    secret_md = SKILL_MD.replace("secrets: []", "secrets: [ECHO_TOKEN]")
    h = with_env(harness([
        {"calls": [("read_skill", {"name": "echo"})]},
        {"calls": [("run_skill", {"name": "echo"})]},
        "Готово.",
    ]))
    h.services.skills.write("echo", {"SKILL.md": secret_md, "scripts/main.py": ECHO}, replace=False)
    app = create_app(h.services.env, FakeLLM([]))
    app.state.services = h.services
    with TestClient(app, base_url="http://127.0.0.1") as client:
        h.turn("запусти", [{"action": "approve", "trust": True}])
        not_set = [m for m in h.messages() if m["role"] == "tool"][1]["content"]
        assert "не заданы ECHO_TOKEN" in not_set
        assert client.put("/api/skills/echo/secrets/ECHO_TOKEN", json={"value": "очень-секретный-токен"}).status_code == 204
        detail = client.get("/api/skills/echo")
        assert detail.json()["secrets"] == {"ECHO_TOKEN": True}
        assert "очень-секретный-токен" not in detail.text
        assert "очень-секретный-токен" not in client.get("/api/skills").text
    h.llm.script = [{"calls": [("run_skill", {"name": "echo"})]}, "Готово."]
    h.turn("ещё раз", [YES])
    result = [m for m in h.messages() if m["role"] == "tool"][-1]["content"]
    assert "токен: ***" in result
    assert "очень-секретный-токен" not in (h.services.env.data_path / "conversations").joinpath(f"{h.sid}.jsonl").read_text(encoding="utf-8")
