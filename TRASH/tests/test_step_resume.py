"""Повтор поручения продолжает незавершённые шаги без повторных побочных эффектов готовых."""

import asyncio
import json
import shlex
import sys
from dataclasses import replace

import pytest
from tests.fakes import ScriptedProvider

from llm.base import LLMProviderError
from shared.document import split_parts
from steps.engine import PART_TOKENS, PARTS_DIR, PARTS_SHARE, RunContext, StepRunner, Toolkit
from steps.models import Step, Steps
from steps.workflow import Workflow


class RecoveringProvider(ScriptedProvider):
    async def chat(self, model, messages, tools=None, response_format=None, options=None):
        if self.steps and isinstance(self.steps[0], Exception):
            self.requests.append({"model": model, "messages": list(messages), "tools": tools})
            raise self.steps.pop(0)
        return await super().chat(model, messages, tools, response_format, options)


def command(code):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def run(workflow, provider, workspace, resume=None):
    context = RunContext(provider, "test", workspace, 8192, Toolkit(lambda name: None, list))
    return asyncio.run(StepRunner(workflow, {}, context, resume=resume).run())


@pytest.mark.parametrize("kind", ["command", "script", "model"])
def test_resume_keeps_done_steps_and_retries_the_failed_step_after_recovery(tmp_path, kind):
    workspace = tmp_path / "project"
    workspace.mkdir()
    agent = tmp_path / "agent"
    scripts = agent / "scripts"
    scripts.mkdir(parents=True)
    first_code = "open('effects.log', 'a').write('first\\n'); print('Первый итог')"
    second_code = (
        "from pathlib import Path; assert Path('ready').exists(), 'not ready'; "
        "Path('second.txt').write_text('done'); print('Второй итог')"
    )
    if kind == "script":
        (scripts / "first.py").write_text(first_code, encoding="utf-8")
        (scripts / "second.py").write_text(second_code, encoding="utf-8")
        first_text, second_text = "first.py", "second.py"
    elif kind == "command":
        first_text, second_text = command(first_code), command(second_code)
    else:
        first_text, second_text = "Напиши первый итог", "Напиши второй итог"
    workflow = Workflow("test", "Проверка", "", "", Steps((), (
        Step("Подготовить", kind, first_text, save="first.md"),
        Step("Завершить", kind, second_text, save="second.md"),
    )), agent)
    first_provider = RecoveringProvider("Первый итог", LLMProviderError("connection refused"))

    failed = run(workflow, first_provider, workspace)

    assert failed["status"] == "failed"
    assert [step["status"] for step in failed["steps"]] == ["done", "failed"]
    assert (workspace / "first.md").read_text(encoding="utf-8") == "Первый итог\n"
    assert not (workspace / "second.md").exists()
    # Видимое редактирование готового файла позволяет заметить его повторную запись.
    (workspace / "first.md").write_text("Сохранить результат пользователя\n", encoding="utf-8")
    if kind == "script":
        # Прямой API StepRunner получает уже проверенный вызывающим кодом снимок.
        (scripts / "second.py").write_text(
            second_code.replace("assert Path('ready').exists(), 'not ready'; ", ""), encoding="utf-8",
        )
    else:
        (workspace / "ready").touch()
    second_provider = ScriptedProvider("Второй итог")
    # Снимок проходит через JSON так же, как при сохранении и перезапуске приложения.
    resumed = run(workflow, second_provider, workspace, json.loads(json.dumps(failed)))

    assert resumed["status"] == "done"
    assert [step["status"] for step in resumed["steps"]] == ["done", "done"]
    assert resumed["steps"][0] == failed["steps"][0]
    assert (workspace / "first.md").read_text(encoding="utf-8") == "Сохранить результат пользователя\n"
    assert (workspace / "second.md").read_text(encoding="utf-8") == "Второй итог\n"
    if kind == "model":
        assert len(first_provider.requests) == 2
        assert len(second_provider.requests) == 1
        assert "Первый итог" in second_provider.requests[0]["messages"][-1].content
    else:
        assert (workspace / "effects.log").read_text(encoding="utf-8") == "first\n"
        assert (workspace / "second.txt").read_text(encoding="utf-8") == "done"
        assert not first_provider.requests and not second_provider.requests


@pytest.mark.parametrize("changed", ["title", "kind", "status"])
def test_resume_skips_only_a_done_step_with_matching_title_and_kind(tmp_path, changed):
    step = Step("Составить итог", "model", "Составь итог", save="result.md")
    workflow = Workflow("test", "Проверка", "", "", Steps((), (step,)))
    previous = run(workflow, ScriptedProvider("Старый итог"), tmp_path)
    if changed == "title":
        workflow = replace(workflow, steps=Steps((), (replace(step, title="Новый шаг"),)))
    elif changed == "kind":
        previous["steps"][0]["kind"] = "command"
    else:
        previous["steps"][0]["status"] = "failed"
    provider = ScriptedProvider("Новый итог")

    resumed = run(workflow, provider, tmp_path, previous)

    assert resumed["status"] == "done"
    assert len(provider.requests) == 1
    assert (tmp_path / "result.md").read_text(encoding="utf-8") == "Новый итог\n"


@pytest.mark.parametrize("changed", ["unchanged", "system", "model", "revision", "provider"])
def test_partial_document_checkpoint_never_mixes_chunks_from_a_changed_execution_context(tmp_path, changed):
    document = "\n\n".join(
        f"## Раздел {number}\n\n" + "Пользователь вводит логин и пароль. Система проверяет права доступа. " * 50
        for number in range(1, 4)
    )
    (tmp_path / "spec.md").write_text(document, encoding="utf-8")
    workflow = Workflow("test", "Проверка", "", "Исходные правила", Steps((), (
        Step("Составить требования", "model", "Выпиши требования", parts="spec.md", save="result.md"),
    )))
    window = 3000
    total = len(split_parts(document, min(PART_TOKENS, int(window * PARTS_SHARE))))
    assert total > 1
    original_provider = RecoveringProvider("Старый фрагмент", LLMProviderError("connection refused"))
    context = RunContext(original_provider, "model-1", tmp_path, window, Toolkit(lambda name: None, list), {
        "MEEPO_AGENT_REVISION": "revision-1", "MEEPO_LLM_URL": "http://first.invalid",
    })

    failed = asyncio.run(StepRunner(workflow, {}, context).run())

    assert failed["status"] == "failed"
    assert (tmp_path / "result.md").read_text(encoding="utf-8") == "Старый фрагмент\n"
    assert len(list((tmp_path / PARTS_DIR).glob("*.json"))) == 1
    provider = ScriptedProvider(*(["Новый фрагмент"] * total))
    context = replace(context, provider=provider, llm_env=dict(context.llm_env))
    if changed == "system":
        workflow = replace(workflow, instructions="Исправленные правила")
    elif changed == "model":
        context.model = "model-2"
    elif changed == "revision":
        context.llm_env["MEEPO_AGENT_REVISION"] = "revision-2"
    elif changed == "provider":
        context.llm_env["MEEPO_LLM_URL"] = "http://second.invalid"

    resumed = asyncio.run(StepRunner(workflow, {}, context).run())

    assert resumed["status"] == "done"
    output = (tmp_path / "result.md").read_text(encoding="utf-8")
    first_prompt = provider.requests[0]["messages"][-1].content
    if changed == "unchanged":
        assert len(provider.requests) == total - 1
        assert "часть 2 из" in first_prompt
        assert output.startswith("Старый фрагмент\n")
    else:
        assert len(provider.requests) == total
        assert "часть 1 из" in first_prompt
        assert "Старый фрагмент" not in output
        assert output.count("Новый фрагмент") == total
    assert not list((tmp_path / PARTS_DIR).glob("*.json"))
