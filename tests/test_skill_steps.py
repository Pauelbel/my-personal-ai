"""Шаги скилла: steps.md разбирается и проверяется, запуск — карточкой с полями, шаги идут по порядку сами."""

import shutil

import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of
from tests.test_graph_flow import project_session, settings

from local_agent.api.app import create_app
from local_agent.skills.files import LIBRARY, library_skills
from local_agent.steps.files import parse_steps

SKILL = "---\nname: Демо\ndescription: Выписывает правила и проверяет их командой\n---\nПравила по теме пишутся по шагам.\n"
STEPS = """Короткая работа для проверки.

## Нужно от вас
- тема — о чём правила
- заметки — что учесть (необязательно)

## Выписать правила
модель: Выпиши правила про {тема}.
сохранить: out/rules.md

## Проверить
команда: grep -q Правило out/rules.md
при ошибке: Выписать правила, 2
"""


def write_skill(tmp_path, steps=STEPS, name="demo", skill=SKILL, scripts=None):
    folder = tmp_path / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(skill, encoding="utf-8")
    if steps is not None:
        (folder / "steps.md").write_text(steps, encoding="utf-8")
    for file, code in (scripts or {}).items():
        (folder / "scripts").mkdir(exist_ok=True)
        (folder / "scripts" / file).write_text(code, encoding="utf-8")
    return folder


def workspace(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def run_events(events):
    return [event["run"] for event in events if event["type"] == "steps"]


def result_of(events):
    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    return result


def test_steps_file_is_parsed_into_inputs_and_steps():
    plan = parse_steps(STEPS)

    assert plan.about == "Короткая работа для проверки."
    assert [(item.name, item.optional) for item in plan.inputs] == [("тема", False), ("заметки", True)]
    first, second = plan.steps
    assert (first.title, first.kind, first.text, first.save) == ("Выписать правила", "model", "Выпиши правила про {тема}.", "out/rules.md")
    assert (second.kind, second.on_error, second.retry_to, second.attempts) == ("command", "retry", 0, 2)


@pytest.mark.parametrize(("broken", "problem"), [
    (STEPS.replace("модель: Выпиши", "Выпиши"), "не указано, что делает шаг"),
    (STEPS.replace("out/rules.md\n\n## Проверить", "out/{тмея}.md\n\n## Проверить"), "{тмея} — такого нет"),
    (STEPS.replace("Выписать правила, 2", "Нет такого шага, 2"), "шага «Нет такого шага» нет"),
    (STEPS.replace("команда: grep", "агент: grep"), "агенту нужны инструменты"),
    (STEPS.replace("команда: grep -q Правило out/rules.md", "скрипт: ../evil.sh"), "имя файла .py"),
    (STEPS.split("## Выписать")[0], "нет ни одного шага"),
])
def test_broken_steps_are_explained(broken, problem):
    with pytest.raises(ValueError, match=problem):
        parse_steps(broken)


def test_library_skills_are_valid_and_their_scripts_exist():
    skills = {skill.id: skill for skill in library_skills()}

    assert {"test-cases", "autotests", "browser-check", "jira-task-review", "bug-report", "update-skills"} <= set(skills)
    assert {skill_id for skill_id, skill in skills.items() if skill.steps} == {
        "test-cases", "autotests", "browser-check", "jira-task-review",
    }
    for skill in skills.values():
        for step in skill.steps.steps if skill.steps else ():
            if step.kind == "script":
                assert (skill.folder / "scripts" / step.text).is_file()


def test_skill_steps_run_from_card_with_inputs_retries_and_reports(tmp_path):
    write_skill(tmp_path)
    folder = workspace(tmp_path)
    provider = ScriptedProvider(
        call("run_skill", skill="demo", inputs={"тема": "вход", "лишнее": "x"}),
        "Тут нет нужного слова",
        "Правило 1. Пароль скрыт.",
        "Готово: правила в out/rules.md",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, folder)
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Выпиши правила про вход"}))[-1]
        events = events_of(client.post(url + "/resume", json={"action": "approve", "inputs": {"тема": "вход в систему"}}))

    pending = card["pending"]
    assert pending["kind"] == "steps" and pending["run"]["values"] == {"тема": "вход", "заметки": ""}
    assert [step["title"] for step in pending["run"]["steps"]] == ["Выписать правила", "Проверить"]
    assert pending["run"]["effects"][0]["code"] == "grep -q Правило out/rules.md"
    assert pending["run"]["problems"] == [] and pending["title"] == "Запустить по шагам: «Демо»"
    # Поле из карточки важнее того, что подставила модель; вторая попытка шага видит, почему не прошла первая.
    first, second = provider.requests[1]["messages"][1].content, provider.requests[2]["messages"][1].content
    assert first.startswith("Скилл «Демо» — шаг 1 из 2") and "Выпиши правила про вход в систему." in first
    assert "Прошлая попытка" not in first and "Прошлая попытка не прошла" in second and "кодом 1" in second
    assert (folder / "out" / "rules.md").read_text(encoding="utf-8") == "Правило 1. Пароль скрыт.\n"
    progress = run_events(events)
    assert progress and progress[-1]["status"] == "done" and progress[-1]["skill"] == "demo"
    result = result_of(events)
    report = result["meta"]["steps"]
    assert not result["is_error"] and report["status"] == "done"
    assert [step["status"] for step in report["steps"]] == ["done", "done"] and report["steps"][1]["tries"] == 2
    assert report["steps"][0]["files"] == ["out/rules.md"]
    assert result["content"].startswith("Шаги скилла «Демо» выполнены") and "работу шагов сам не повторяй" in result["content"]
    assert events[-1]["type"] == "done" and events[-1]["message"]["content"].startswith("Готово")


def test_steps_stop_when_retries_run_out(tmp_path):
    write_skill(tmp_path)
    provider = ScriptedProvider(call("run_skill", skill="demo", inputs={"тема": "вход"}), "нет", "нет", "нет", "Не вышло")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace(tmp_path))
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Правила"})
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))

    result = result_of(events)
    report = result["meta"]["steps"]
    assert result["is_error"] and report["status"] == "failed" and "Шаг 2 «Проверить» не получился" in report["error"]
    assert report["steps"][1]["tries"] == 3


def test_missing_required_input_stops_before_any_step(tmp_path):
    write_skill(tmp_path)
    provider = ScriptedProvider(call("run_skill", skill="demo"), "Нужна тема")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace(tmp_path))
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Правила"})
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))

    result = result_of(events)
    assert result["is_error"] and result["meta"]["steps"]["error"].startswith("В карточке не заполнены поля: тема")
    assert len(provider.requests) == 2


def test_input_name_translated_by_the_model_lands_in_the_empty_required_field(tmp_path):
    write_skill(tmp_path)
    provider = ScriptedProvider(call("run_skill", skill="demo", inputs={"topic": "вход"}), "Ясно")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace(tmp_path))
        card = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Правила"}))[-1]

    # Gemma пишет «folder» вместо «папка»: одно чужое значение и одно пустое обязательное поле — оно туда и встаёт.
    assert card["pending"]["run"]["values"] == {"тема": "вход", "заметки": ""}


def test_run_button_opens_the_card_without_asking_the_model(tmp_path):
    write_skill(tmp_path)
    write_skill(tmp_path, steps=None, name="plain", skill=SKILL.replace("Демо", "Без шагов"))
    provider = ScriptedProvider("Правило 1", "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace(tmp_path))
        url = f"/api/sessions/{session['id']}"
        plain = client.post(url + "/turns/stream", json={"skill": "plain"})
        events = events_of(client.post(url + "/turns/stream", json={"skill": "demo"}))
        asked_before_card = len(provider.requests)
        title = client.get(url).json()["title"]
        finished = events_of(client.post(url + "/resume", json={"action": "approve", "inputs": {"тема": "вход"}}))

    assert plain.status_code == 400 and "нет шагов" in plain.json()["detail"]
    assert asked_before_card == 0 and title == "Скилл «Демо» по шагам"
    assert events[0]["message"]["content"] == "▶ Запустить по шагам: Демо"
    assert events[-1]["type"] == "interrupt" and events[-1]["pending"]["kind"] == "steps"
    assert finished[-1]["type"] == "done"


def test_agent_step_works_with_its_tools_without_cards(tmp_path):
    write_skill(tmp_path, """## Нужно от вас
- имя — имя файла

## Записать
агент: Запиши файл {имя}.
инструменты: файлы
""", "writer")
    folder = workspace(tmp_path)
    provider = ScriptedProvider(
        call("run_skill", skill="writer", inputs={"имя": "a.txt"}),
        call("write_file", path="a.txt", content="привет"),
        "Записал a.txt",
        "Готово",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, folder)
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Запиши"})
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))

    assert (folder / "a.txt").read_text(encoding="utf-8") == "привет"
    assert not any(event["type"] == "interrupt" for event in events)
    assert result_of(events)["meta"]["steps"]["steps"][0]["files"] == ["a.txt"]
    # У шага-агента — только его инструменты.
    assert set(provider.tool_names(1)) == {"list_files", "read_file", "search_files", "write_file", "edit_file"}


def test_script_step_runs_skill_script_with_session_model(tmp_path):
    # Строку хода работы видно, когда с прошлого обновления прогресса прошла секунда: долгий скрипт пишет их реже.
    script = (
        "import json, os, sys, time\n"
        "args = json.load(sys.stdin)\n"
        "time.sleep(1.1)\n"
        "print('разбираю ' + args['path'], file=sys.stderr, flush=True)\n"
        "open('out.md', 'w', encoding='utf-8').write('готово')\n"
        "print(os.environ['MEEPO_LLM_MODEL'] + ' ' + os.environ.get('MEEPO_LLM_KEY', ''))\n"
    )
    write_skill(tmp_path, "## Нужно от вас\n- папка — что разобрать\n\n## Разобрать\nскрипт: work.py\n"
                "параметры: {\"path\": \"{папка}\"}\n", "scripted", scripts={"work.py": script})
    folder = workspace(tmp_path)
    provider = ScriptedProvider(call("run_skill", skill="scripted", inputs={"папка": "docs"}), "Готово")
    provider.api_key = "sk-тайна"
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, folder)
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Разбери"}))[-1]
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))

    effects = card["pending"]["run"]["effects"]
    assert effects[0]["code"] == "work.py" and "скрипт скилла" in effects[0]["text"]
    report = result_of(events)["meta"]["steps"]
    step = report["steps"][0]
    # Скрипт получил модель сессии, а ключ API в итог не попал; строка stderr была видна как ход работы.
    assert report["status"] == "done" and step["output"] == "test ***"
    assert step["files"] == ["out.md"] and (folder / "out.md").read_text(encoding="utf-8") == "готово"
    assert any(run["steps"][0]["detail"] == "разбираю docs" for run in run_events(events))


def test_read_only_project_shows_why_steps_cannot_run(tmp_path):
    write_skill(tmp_path)
    provider = ScriptedProvider(call("run_skill", skill="demo", inputs={"тема": "x"}), "Ясно")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        folder = workspace(tmp_path)
        session = project_session(client, folder)
        client.put(f"/api/projects/{session['project_id']}", json={"name": "Проект", "workspace": str(folder), "mode": "read"})
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Правила"}))[-1]
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))

    assert any("только для чтения" in problem for problem in card["pending"]["run"]["problems"])
    result = result_of(events)
    assert result["is_error"] and "только для чтения" in result["meta"]["steps"]["error"]


def test_main_agent_sees_skills_with_steps_first_and_proposes_new_ones(tmp_path):
    write_skill(tmp_path)
    write_skill(tmp_path, steps=None, name="notes", skill="---\nname: Заметки\ndescription: Как вести заметки\n---\nКоротко.\n")
    steps = "## Нужно от вас\n- папка — где документы\n\n## Свести\nмодель: Сведи {папка}.\nсохранить: summary.md\n"
    proposal = {"kind": "skill", "name": "summary", "title": "Сводка", "description": "Сводит документы",
                "reason": "Просили", "content": "Сводка документов — по шагам."}
    provider = ScriptedProvider(
        call("propose", **proposal, steps="## Свести\nмодель: Сведи {папк}."),
        call("propose", **proposal, steps=steps),
        "Создал скилл",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace(tmp_path))
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Научись сводить документы"}))[-1]
        client.post(url + "/resume", json={"action": "approve"})
        overview = client.get("/api/capabilities").json()

    prompt = provider.system_prompt(0)
    assert "- demo — Выписывает правила и проверяет их командой (▶ по шагам; нужно: тема, заметки)" in prompt
    assert "- notes — Как вести заметки\n" in prompt and prompt.index("- demo") < prompt.index("- notes")
    assert prompt.index("Скиллы —") < prompt.index("Инструменты (")
    # Первое предложение не разобралось (опечатка в {папк}) и вернулось модели с причиной, без карточки.
    rejected = provider.requests[1]["messages"][-1].content
    assert "шаги не разобрались" in rejected and "{папк}" in rejected and "Нужно от вас" in rejected
    pending = card["pending"]["proposal"]
    assert pending["kind"] == "skill" and [step["title"] for step in pending["plan"]["steps"]] == ["Свести"]
    folder = tmp_path / "skills" / "summary"
    assert (folder / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: Сводка\n")
    assert (folder / "steps.md").read_text(encoding="utf-8") == steps
    assert {skill["id"] for skill in overview["skills"]} == {"demo", "notes", "summary"}


def test_main_agent_reads_skill_itself_before_proposing_an_update(tmp_path):
    write_skill(tmp_path)
    shutil.copytree(LIBRARY / "test-cases", tmp_path / "skills" / "test-cases")
    edited = STEPS.replace("Выпиши правила про {тема}.", "Выпиши правила про {тема} таблицей.")
    provider = ScriptedProvider(
        call("read_capability", kind="skill", name="demo"),
        call("read_capability", kind="skill", name="test-cases"),
        call("propose", kind="skill", name="demo", title="Демо", description="Выписывает правила таблицей",
             reason="Просили таблицу", content="Правила по теме пишутся по шагам, таблицей.", steps=edited),
        "Обновил",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, workspace(tmp_path))
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Правила — таблицей"}))
        events_of(client.post(url + "/resume", json={"action": "approve"}))

    demo, testcases = [event["message"] for event in events if event["type"] == "tool_result"]
    assert not demo["is_error"] and "title: Демо" in demo["content"]
    assert "content:\nПравила по теме пишутся по шагам." in demo["content"] and "steps:\n" + STEPS.strip() in demo["content"]
    # Скрипты скилла агент тоже видит: формат тест-кейсов правится через propose со scripts.
    assert "scripts → testcases.py:" in testcases["content"] and "def generate(" in testcases["content"]
    card = events[-1]["pending"]
    assert card["update"] and card["proposal"]["previous"] == "Правила по теме пишутся по шагам."
    assert card["proposal"]["previous_steps"] == STEPS
    assert (tmp_path / "skills" / "demo" / "steps.md").read_text(encoding="utf-8") == edited.strip() + "\n"


def test_capabilities_api_creates_edits_enables_and_archives_skills(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        overview = client.get("/api/capabilities").json()
        library = {item["id"] for item in overview["library"] if item["kind"] == "skill"}
        enabled = client.post("/api/capabilities/library/skill/browser-check")
        broken = client.post("/api/capabilities/skill", json={"id": "mine", "text": "---\nname: x\n---\nтекст\n"})
        created = client.post("/api/capabilities/skill", json={"id": "mine", "text": SKILL})
        duplicate = client.post("/api/capabilities/skill", json={"id": "mine", "text": SKILL})
        url = "/api/capabilities/skill/mine"
        source = client.get(url).json()
        with_steps = client.put(url, json={"files": {"steps.md": STEPS}})
        steps_view = next(item for item in client.get("/api/capabilities").json()["skills"] if item["id"] == "mine")
        broken_steps = client.put(url, json={"files": {"steps.md": STEPS.replace("модель:", "")}})
        bad_script = client.put(url, json={"files": {"scripts/x.py": "def ("}})
        outside = client.put(url, json={"files": {"../evil.md": "x"}})
        without_steps = client.put(url, json={"files": {"steps.md": None}})
        plain_view = next(item for item in client.get("/api/capabilities").json()["skills"] if item["id"] == "mine")
        client.post(f"{url}/archive")
        archived = client.get("/api/capabilities").json()

    assert {"test-cases", "autotests", "browser-check", "jira-task-review"} <= library
    assert enabled.status_code == 200 and (tmp_path / "skills" / "browser-check" / "steps.md").is_file()
    assert broken.status_code == 400 and "mine/SKILL.md" in broken.json()["detail"] and ".mine." not in broken.json()["detail"]
    assert created.status_code == 201 and duplicate.status_code == 409
    assert source["files"] == {"SKILL.md": SKILL} and source["path"].endswith("skills/mine")
    assert with_steps.status_code == 204 and [step["title"] for step in steps_view["steps"]["steps"]] == ["Выписать правила", "Проверить"]
    assert steps_view["files"] == ["SKILL.md", "steps.md"]
    assert broken_steps.status_code == 400 and "steps.md" in broken_steps.json()["detail"]
    assert bad_script.status_code == 400 and "scripts/x.py" in bad_script.json()["detail"]
    assert outside.status_code == 400
    assert without_steps.status_code == 204 and plain_view["steps"] is None
    assert [item["id"] for item in archived["archive"] if item["kind"] == "skill"] == ["mine"]
    assert "mine" not in {item["id"] for item in archived["skills"]}
    assert "## Нужно от вас" in overview["templates"]["steps"] and "description:" in overview["templates"]["skill"]


def test_tool_step_reports_files_it_wrote(tmp_path):
    write_skill(tmp_path, """## Записать
инструмент: write_file
параметры: {"path": "docs/note.md", "content": "текст"}
""", "note")
    folder = workspace(tmp_path)
    provider = ScriptedProvider(call("run_skill", skill="note"), "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, folder)
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/turns/stream", json={"content": "Запиши"})
        events = events_of(client.post(url + "/resume", json={"action": "approve"}))

    assert result_of(events)["meta"]["steps"]["steps"][0]["files"] == ["docs/note.md"]

