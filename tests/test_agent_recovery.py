"""Восстановление повторяет изменённый шаг, сохраняя неизменный завершённый префикс."""

import asyncio
from dataclasses import replace

import pytest
from tests.fakes import ScriptedProvider

from agents.models import Agent
from agents.recovery import resumable_snapshot, step_fingerprints
from agents.runtime import TESTCASES_LAUNCHER
from steps.engine import RunContext, StepRunner, Toolkit
from steps.models import Step, Steps


def make_agent(tmp_path, *steps):
    folder = tmp_path / "agent"
    (folder / "scripts").mkdir(parents=True, exist_ok=True)
    return Agent("qa", "QA", "Правила агента", "", "", steps=Steps((), steps), folder=folder, tools=("read_file",))


def completed_snapshot(fingerprints, count=None):
    return {"status": "failed", "step_fingerprints": fingerprints, "steps": [
        {"title": f"Шаг {index}", "kind": "command", "status": "done", "files": [], "output": "Готово"}
        for index in range(len(fingerprints) if count is None else count)
    ]}


def test_fixing_the_second_script_preserves_the_completed_first_side_effect(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    agent = make_agent(tmp_path, Step("Подготовить", "script", "first.py"), Step("Завершить", "script", "second.py"))
    first = agent.folder / "scripts" / "first.py"
    second = agent.folder / "scripts" / "second.py"
    first.write_text("open('effects.log', 'a').write('first\\n')\n", encoding="utf-8")
    second.write_text("raise RuntimeError('broken')\n", encoding="utf-8")
    context = RunContext(ScriptedProvider(), "test", workspace, 8192, Toolkit(lambda name: None, list))
    before = step_fingerprints(agent, {}, workspace)
    snapshot = asyncio.run(StepRunner(agent.as_workflow(), {}, context).run())
    snapshot["step_fingerprints"] = before
    assert snapshot["status"] == "failed"
    second.write_text("open('second.txt', 'w').write('done')\n", encoding="utf-8")
    after = step_fingerprints(agent, {}, workspace)

    resume = resumable_snapshot(snapshot, after, workspace)
    result = asyncio.run(StepRunner(agent.as_workflow(), {}, context, resume=resume).run())

    assert before[0] == after[0] and before[1] != after[1]
    assert len(resume["steps"]) == 1
    assert result["status"] == "done"
    assert (workspace / "effects.log").read_text(encoding="utf-8") == "first\n"
    assert (workspace / "second.txt").read_text(encoding="utf-8") == "done"


@pytest.mark.parametrize("kind", ["model", "agent"])
@pytest.mark.parametrize("field", ["system_prompt", "tools"])
def test_prompt_and_tool_changes_invalidate_model_steps_but_preserve_commands(tmp_path, kind, field):
    agent = make_agent(tmp_path, Step("Шаг 0", "command", "true"), Step("Шаг 1", kind, "Составь итог"))
    before = step_fingerprints(agent, {}, tmp_path)
    changed = replace(agent, **{field: "Новые правила" if field == "system_prompt" else ("read_file", "search_files")})
    after = step_fingerprints(changed, {}, tmp_path)

    resume = resumable_snapshot(completed_snapshot(before), after, tmp_path)

    assert before[0] == after[0]
    assert before[1] != after[1]
    assert len(resume["steps"]) == 1


def test_changing_the_first_step_invalidates_every_later_done_step(tmp_path):
    agent = make_agent(tmp_path, Step("Шаг 0", "command", "true"), Step("Шаг 1", "command", "true"))
    before = step_fingerprints(agent, {}, tmp_path)
    changed = replace(agent, steps=Steps((), (replace(agent.steps.steps[0], text="false"), agent.steps.steps[1])))
    after = step_fingerprints(changed, {}, tmp_path)

    assert before[1] == after[1]
    assert resumable_snapshot(completed_snapshot(before), after, tmp_path) is None


def test_changed_inputs_invalidate_the_completed_prefix(tmp_path):
    agent = make_agent(tmp_path, Step("Шаг 0", "command", "echo {источник}"))
    before = step_fingerprints(agent, {"источник": "docs/old.md"}, tmp_path)
    after = step_fingerprints(agent, {"источник": "docs/new.md"}, tmp_path)

    assert resumable_snapshot(completed_snapshot(before), after, tmp_path) is None


@pytest.mark.parametrize("source", ["file", "directory", "parts"])
def test_changed_input_file_contents_invalidate_their_step(tmp_path, source):
    docs = tmp_path / "docs"
    docs.mkdir()
    spec = docs / "spec.md"
    spec.write_text("Исходные требования", encoding="utf-8")
    step = Step("Шаг 0", "model", "Составь итог", files=("{источник}",))
    if source == "parts":
        step = replace(step, files=(), parts="{источник}", save="result.md")
    agent = make_agent(tmp_path, step)
    inputs = {"источник": "docs" if source == "directory" else "docs/spec.md"}
    before = step_fingerprints(agent, inputs, tmp_path)
    spec.write_text("Изменённые требования", encoding="utf-8")
    after = step_fingerprints(agent, inputs, tmp_path)

    assert before != after
    assert resumable_snapshot(completed_snapshot(before), after, tmp_path) is None


def test_new_and_removed_files_in_an_input_directory_invalidate_the_step(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    agent = make_agent(tmp_path, Step("Шаг 0", "model", "Составь итог", files=("docs",)))
    empty = step_fingerprints(agent, {}, tmp_path)
    (docs / "spec.md").write_text("Требования", encoding="utf-8")
    populated = step_fingerprints(agent, {}, tmp_path)

    assert empty != populated
    assert resumable_snapshot(completed_snapshot(empty), populated, tmp_path) is None
    (docs / "spec.md").unlink()
    assert resumable_snapshot(completed_snapshot(populated), step_fingerprints(agent, {}, tmp_path), tmp_path) is None


@pytest.mark.parametrize("path", ["missing.md", "../outside.md", "/absolute.md"])
def test_unavailable_inputs_never_produce_a_reusable_fingerprint(tmp_path, path):
    agent = make_agent(tmp_path, Step("Шаг 0", "model", "Составь итог", files=(path,)))
    before = step_fingerprints(agent, {}, tmp_path)

    assert before == [""]
    assert resumable_snapshot(completed_snapshot(before), before, tmp_path) is None


@pytest.mark.parametrize("outputs", [["missing.md"], ["../outside.md"], ["/absolute.md"], [None]])
def test_missing_or_outside_completed_outputs_stop_reuse(tmp_path, outputs):
    snapshot = completed_snapshot(["first", "second"])
    snapshot["steps"][1]["files"] = outputs

    resume = resumable_snapshot(snapshot, ["first", "second"], tmp_path)

    assert len(resume["steps"]) == 1


def test_completed_output_must_still_exist_and_resume_does_not_mutate_the_saved_snapshot(tmp_path):
    output = tmp_path / "result.md"
    output.write_text("Результат", encoding="utf-8")
    snapshot = completed_snapshot(["first"])
    snapshot["steps"][0]["files"] = ["result.md"]

    resume = resumable_snapshot(snapshot, ["first"], tmp_path)
    resume["steps"][0]["files"].clear()

    assert snapshot["steps"][0]["files"] == ["result.md"]
    output.unlink()
    assert resumable_snapshot(snapshot, ["first"], tmp_path) is None


def test_a_failed_step_ends_the_reusable_prefix_even_if_later_steps_are_done(tmp_path):
    snapshot = completed_snapshot(["first", "second", "third"])
    snapshot["steps"][1]["status"] = "failed"

    resume = resumable_snapshot(snapshot, ["first", "second", "third"], tmp_path)

    assert len(resume["steps"]) == 1
    assert resumable_snapshot({"steps": snapshot["steps"]}, ["first", "second", "third"], tmp_path) is None
    assert resumable_snapshot(None, [], tmp_path) is None


def test_fingerprints_are_stable_and_include_the_concrete_builtin_runtime(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, Step("Шаг 0", "script", "testcases.py"))
    (agent.folder / "scripts" / "testcases.py").write_text(TESTCASES_LAUNCHER, encoding="utf-8")
    before = step_fingerprints(agent, {}, tmp_path)
    assert before == step_fingerprints(agent, {}, tmp_path)
    fake = tmp_path / "new-runtime"
    fake.mkdir()
    (fake / "testcases.py").write_text("print('new runtime')\n", encoding="utf-8")
    monkeypatch.setattr("agents.recovery.files", lambda package: fake)

    assert before != step_fingerprints(agent, {}, tmp_path)


def test_workspace_changes_invalidate_fingerprints(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    agent = make_agent(tmp_path, Step("Шаг 0", "command", "true"))

    assert step_fingerprints(agent, {}, tmp_path) != step_fingerprints(agent, {}, other)


@pytest.mark.parametrize("source", ["file", "directory"])
def test_script_source_in_inputs_is_hashed_even_without_step_files(tmp_path, source):
    docs = tmp_path / "docs"
    docs.mkdir()
    spec = docs / "spec.md"
    spec.write_text("Исходные требования", encoding="utf-8")
    agent = make_agent(tmp_path, Step("Шаг 0", "script", "generate.py", params={"path": "{источник}"}))
    (agent.folder / "scripts" / "generate.py").write_text("print('done')\n", encoding="utf-8")
    inputs = {"источник": "docs" if source == "directory" else "docs/spec.md"}
    before = step_fingerprints(agent, inputs, tmp_path)
    spec.write_text("Изменённые требования", encoding="utf-8")
    after = step_fingerprints(agent, inputs, tmp_path)

    assert before != after
    assert resumable_snapshot(completed_snapshot(before), after, tmp_path) is None


def test_disappearing_implicit_input_file_invalidates_the_completed_prefix(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("Требования", encoding="utf-8")
    agent = make_agent(tmp_path, Step("Шаг 0", "command", "true"))
    inputs = {"источник": "spec.md"}
    before = step_fingerprints(agent, inputs, tmp_path)
    spec.unlink()
    after = step_fingerprints(agent, inputs, tmp_path)

    assert before != after
    assert resumable_snapshot(completed_snapshot(before), after, tmp_path) is None


@pytest.mark.parametrize("source", [".", "./", "docs/.."])
def test_whole_workspace_input_cannot_reuse_completed_steps(tmp_path, source):
    (tmp_path / "docs").mkdir()
    agent = make_agent(tmp_path, Step("Шаг 0", "command", "true"))
    fingerprints = step_fingerprints(agent, {"источник": source}, tmp_path)

    assert fingerprints == [""]
    assert resumable_snapshot(completed_snapshot(fingerprints), fingerprints, tmp_path) is None


def test_plain_text_inputs_are_not_required_to_be_file_paths(tmp_path):
    agent = make_agent(tmp_path, Step("Шаг 0", "command", "true"))
    inputs = {"задание": "Составь тест-кейсы на русском языке.", "url": "https://example.invalid/docs", "пусто": ""}
    fingerprints = step_fingerprints(agent, inputs, tmp_path)

    assert fingerprints[0]
    assert resumable_snapshot(completed_snapshot(fingerprints), fingerprints, tmp_path) is not None


def test_whole_workspace_in_step_files_cannot_reuse_completed_steps(tmp_path):
    agent = make_agent(tmp_path, Step("Шаг 0", "model", "Составь итог", files=(".",)))

    assert step_fingerprints(agent, {}, tmp_path) == [""]
