"""process_document: основной агент проходит большой документ проекта по частям — задание для каждой части отдельно.

Это скилл из одного шага «по частям», собранный на лету: та же карточка запуска (документ, задание и файл
результата можно поправить), тот же прогресс в чате и то же продолжение с места остановки, что у шагов скиллов.
"""

import contextlib
from pathlib import Path, PurePosixPath

from local_agent.core.tool import CURRENT_TURN, ToolResult
from local_agent.llm.base import context_length
from local_agent.llm.registry import LLMRegistry
from local_agent.shared.document import split_parts
from local_agent.skills.models import Skill
from local_agent.steps.engine import PART_TOKENS, PARTS_SHARE, RunContext, StepRunner, Toolkit, problems
from local_agent.steps.models import Step, StepInput, Steps
from local_agent.tools.skills import llm_env, run_view, steps_writer

DOCUMENT, TASK, SAVE = "документ", "задание", "сохранить"
PROCESS_SKILL = Skill(
    id="process_document",
    name="Документ по частям",
    description="Задание выполняется для каждой части документа отдельно, с чистым контекстом; ответы подряд "
                "записываются в файл результата.",
    instructions="",
    steps=Steps(
        inputs=(
            StepInput(DOCUMENT, "файл проекта, например docs/spec.md"),
            StepInput(TASK, "что сделать с каждой частью"),
            StepInput(SAVE, "куда записать результат, например docs/spec-result.md"),
        ),
        steps=(Step(
            title="Обработать документ по частям", kind="model", text=f"{{{TASK}}}",
            parts=f"{{{DOCUMENT}}}", save=f"{{{SAVE}}}",
        ),),
    ),
)


class ProcessDocumentTool:
    id = "process_document"
    name = "Документ по частям"
    requires_approval = True
    description = (
        "Обработать большой документ проекта целиком: он делится на части, задание task выполняется для каждой "
        "части отдельным запросом к модели, ответы подряд записываются в файл save. Для работы по всему документу, "
        "который не помещается в окно: тест-кейсы, выписка требований, конспект, поиск ошибок. PDF и DOCX сначала "
        "переведи в Markdown. Остановленная работа с теми же path, task и save продолжается с места остановки"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Документ: относительный путь к текстовому файлу (.md, .txt)"},
            "task": {"type": "string", "description": "Что сделать с каждой частью — так, как написал бы пользователь"},
            "save": {"type": "string", "description": "Файл результата; по умолчанию <документ>-result.md рядом"},
        },
        "required": ["path", "task"],
        "additionalProperties": False,
    }

    def __init__(self, providers: LLMRegistry, kit: Toolkit, window: int, workspace: Path) -> None:
        """window — окно модели сессии, известное с прошлого ответа (или по умолчанию): по нему карточка
        считает части; при запуске окно берётся у модели."""
        self._providers = providers
        self._kit = kit
        self._window = window
        self._workspace = workspace

    def validate(self, arguments: dict[str, object]) -> str | None:
        for name in ("path", "task"):
            if not isinstance(arguments.get(name), str) or not arguments[name].strip():
                return f"Нужен {name}: {self.parameters['properties'][name]['description']}"
        if not isinstance(arguments.get("save"), str) or not arguments["save"].strip():
            path = PurePosixPath(arguments["path"].strip().strip("`"))
            arguments["save"] = str(path.with_name(f"{path.stem}-result.md"))
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        values = _values(arguments)
        return {
            "kind": "steps",
            "title": f"Обработать по частям: {values[DOCUMENT]}",
            "run": {
                **run_view(PROCESS_SKILL),
                "description": self._size(values[DOCUMENT]),
                "values": values,
                "effects": PROCESS_SKILL.steps.effects(),
                "problems": problems(PROCESS_SKILL, self._kit),
            },
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(problem, is_error=True)
        if workspace is None:
            return ToolResult("Документ обрабатывается в папке проекта: откройте сессию проекта", is_error=True)
        turn = CURRENT_TURN.get() or {}
        provider = self._providers.get(turn.get("provider_id", ""))
        if provider is None or not turn.get("model"):
            return ToolResult("Нужна модель текущей сессии", is_error=True)
        values = _values(arguments)
        window = await context_length(provider, turn["model"]) or self._window
        write = steps_writer()
        runner = StepRunner(
            PROCESS_SKILL, values,
            RunContext(provider, turn["model"], workspace, window, self._kit, llm_env(provider, turn["model"])),
            on_progress=lambda snapshot: write({"type": "steps", "run": snapshot}),
        )
        snapshot = await runner.run()
        step = snapshot["steps"][0]
        if snapshot["status"] == "done":
            text = (
                f"{step['output']} Пользователь видит отчёт в чате. Коротко скажи, где лежит результат; "
                "сам документ заново не обрабатывай. Результат читай через read_file, только если пользователь "
                "просит пересказать или проверить его."
            )
        else:
            text = (
                f"Обработка документа {values[DOCUMENT]} остановлена: {snapshot['error']}. Готовые части уже "
                f"записаны в {values[SAVE]}; если вызвать process_document снова с теми же path, task и save, "
                "работа продолжится с места остановки. Коротко объясни пользователю причину."
            )
        return ToolResult(text, is_error=snapshot["status"] != "done", meta={"steps": snapshot})

    def _size(self, relative: str) -> str:
        """Сколько в документе частей — пользователь видит до запуска, сколько будет запросов к модели."""
        target = (self._workspace / relative.strip().strip("`")).resolve()
        if not target.is_relative_to(self._workspace.resolve()) or not target.is_file():
            return PROCESS_SKILL.description
        with contextlib.suppress(OSError, UnicodeError):
            text = target.read_bytes().decode("utf-8")
            count = len(split_parts(text, min(PART_TOKENS, int(self._window * PARTS_SHARE))))
            return (f"{relative}: {len(text)} символов — около {count} частей, на каждую — отдельный запрос к модели. "
                    "Ответы подряд записываются в файл результата.")
        return PROCESS_SKILL.description


def _values(arguments: dict[str, object]) -> dict[str, str]:
    """Поля карточки: из параметров вызова, поверх — то, что пользователь поправил в карточке (inputs)."""
    values = {DOCUMENT: str(arguments.get("path") or ""), TASK: str(arguments.get("task") or ""),
              SAVE: str(arguments.get("save") or "")}
    edited = arguments.get("inputs")
    if isinstance(edited, dict):
        values.update({name: str(value) for name, value in edited.items() if name in values and str(value).strip()})
    return {name: value.strip() for name, value in values.items()}
