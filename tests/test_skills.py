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
                             "files": {"script.py": code}, **extra})


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
    assert create_card["skill"]["files"]["script.py"] == ECHO
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


def test_syntax_error_and_extra_files(harness):
    # Скилл — ровно SKILL.md и один script.py: второй модуль рядом не принимается.
    split = ("create_skill", {"name": "echo", "reason": "r", "skill_md": SKILL_MD,
                              "files": {"script.py": ECHO, "scripts/parse.py": "x = 1\n"}})
    h = with_env(harness([
        {"calls": [create_call(code="def broken(:\n"), split, ("create_skill", {"name": "echo", "reason": "r", "skill_md": SKILL_MD, "files": {}})]},
        "Понял.",
    ]))
    h.turn("сделай скилл")
    first, second, third = [m["content"] for m in h.messages() if m["role"] == "tool"]
    assert "Синтаксическая ошибка в script.py" in first
    assert "Лишние файлы: scripts/parse.py" in second and "весь код — в одном script.py" in second
    assert "Нужен файл script.py" in third


def test_old_layout_reported(tmp_path):
    from meepo.skills.registry import SkillRegistry
    folder = tmp_path / "old"
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text(SKILL_MD.replace("echo", "old"), encoding="utf-8")
    (folder / "scripts" / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert "старый формат" in SkillRegistry(tmp_path).get("old").error


def test_run_without_read_skill(harness):
    # Чтение скилла — совет в промпте, а не условие: верный первый вызов не отклоняется.
    h = with_env(harness([{"calls": [("run_skill", {"name": "echo", "args": {"x": 1}})]}, "Готово."]))
    h.services.skills.write("echo", {"SKILL.md": SKILL_MD, "script.py": ECHO}, replace=False)
    h.turn("запусти echo", [YES])
    assert 'аргументы: {"x": 1}' in [m for m in h.messages() if m["role"] == "tool"][0]["content"]


def test_timeout_kills_process_tree(tmp_path):
    folder = tmp_path / "slow"
    folder.mkdir()
    marker = tmp_path / "child-alive"
    # Скрипт запускает дочерний процесс, который через 3 с создаст файл: убитое дерево его не создаст.
    child = f"import time; time.sleep(3); open({str(marker)!r}, 'w').close()"
    (folder / "script.py").write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
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
    folder.mkdir()
    (folder / "script.py").write_text("print('начало' + 'x' * 20000 + 'конец')\n", encoding="utf-8")
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
                                     "files": {"script.py": new_code}})]},
        "Готово.",
    ]))
    h.services.skills.write("echo", {"SKILL.md": SKILL_MD, "script.py": ECHO}, replace=False)
    h.turn("поправь echo", [YES])
    assert '+print("новые аргументы:"' in h.cards()[0]["diff"]
    folder = h.services.skills.root / "echo"
    assert "новые аргументы" in (folder / "script.py").read_text(encoding="utf-8")
    (version,) = h.services.history.versions("echo")
    with TestClient(create_app(h.services.env, FakeLLM([])), base_url="http://127.0.0.1") as client:
        detail = client.post("/api/skills/echo/rollback", json={"version": version}).json()
        assert "новые аргументы" not in detail["files"]["script.py"]
        assert len(detail["history"]) == 2


def test_three_failed_fixes_stop(harness):
    calls = [{"calls": [("update_skill", {"name": "echo", "reason": "r", "files": {"script.py": ECHO + f"\n# {n}\n"}})]}
             for n in range(4)]
    h = with_env(harness([{"calls": [("read_skill", {"name": "echo"})]}, *calls, "Три правки не помогли — как быть?"]))
    h.services.skills.write("echo", {"SKILL.md": SKILL_MD, "script.py": ECHO}, replace=False)
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
    h.services.skills.write("echo", {"SKILL.md": secret_md, "script.py": ECHO}, replace=False)
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


def test_read_skill_file(harness):
    h = with_env(harness([{"calls": [("read_skill", {"name": "echo", "file": "script.py"}),
                                     ("read_skill", {"name": "echo", "file": "nope.py"})]}, "Прочитал."]))
    h.services.skills.write("echo", {"SKILL.md": SKILL_MD, "script.py": ECHO}, replace=False)
    h.turn("покажи код echo")
    code, missing = [m for m in h.messages() if m["role"] == "tool"]
    assert code["content"].startswith("echo/script.py:\nimport json") and "нет файла nope.py" in missing["content"]


def test_required_args_checked_before_card(harness):
    # Обязательные аргументы — из раздела «# Аргументы» SKILL.md: без них карточки нет, модель видит список.
    skill_md = SKILL_MD + "# Аргументы (JSON)\n- path — файл или папка в проекте (обязательно)\n- limit — сколько (по умолчанию 5)\n"
    h = with_env(harness([{"calls": [("run_skill", {"name": "echo", "args": {"limit": 2}})]},
                          {"calls": [("run_skill", {"name": "echo", "args": {"path": "docs"}})]}, "Готово."]))
    h.services.skills.write("echo", {"SKILL.md": skill_md, "script.py": ECHO}, replace=False)
    h.turn("запусти echo", [YES])
    first, second = [m["content"] for m in h.messages() if m["role"] == "tool"]
    assert "Не хватает обязательных аргументов скилла echo: path" in first and "- limit — сколько" in first
    assert len(h.cards()) == 1 and 'аргументы: {"path": "docs"}' in second
