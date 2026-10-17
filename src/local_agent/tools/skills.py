"""run_skill: основной агент запускает шаги скилла, пользователь видит карточку и нажимает «Запустить».

Карточка показывает шаги, поля «Нужно от вас» (агент заполняет их из разговора, пользователь правит) и что шаги
будут делать с проектом. После «Запустить» шаги идут сами, по порядку; прогресс уходит в поток хода событием steps,
итог — результатом вызова (в meta — отчёт по шагам для чата).
"""

from pathlib import Path

from langgraph.config import get_stream_writer

from local_agent.core.tool import CURRENT_TURN, ToolResult
from local_agent.llm.base import LLMProvider, context_length
from local_agent.llm.registry import LLMRegistry
from local_agent.skills.models import Skill
from local_agent.skills.registry import SkillRegistry
from local_agent.steps.engine import RunContext, StepRunner, Toolkit, problems, report

MAX_INPUT_CHARS = 20_000


class RunSkillTool:
    id = "run_skill"
    name = "Шаги скилла"
    requires_approval = True
    description = (
        "Запустить скилл по шагам — готовую работу (в реестре у таких скиллов «▶ по шагам»). В inputs передай то, "
        "что уже известно из разговора, по именам из «нужно»; чего нет — оставь, пользователь заполнит в карточке. "
        "Шаги идут сами: тебе останется коротко пересказать итог"
    )

    def __init__(self, skills: SkillRegistry, providers: LLMRegistry, kit: Toolkit, default_window: int) -> None:
        self._skills = skills
        self._providers = providers
        self._kit = kit
        self._default_window = default_window

    def _runnable(self) -> list[Skill]:
        return [skill for skill in self._skills.all() if skill.steps is not None]

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "enum": [skill.id for skill in self._runnable()] or [""],
                          "description": "id скилла с шагами"},
                "inputs": {"type": "object", "additionalProperties": {"type": "string"},
                           "description": "Значения «нужно» скилла: имя → значение"},
            },
            "required": ["skill"],
            "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, object]) -> str | None:
        skill = self._skills.get(arguments.get("skill"))
        if skill is None or skill.steps is None:
            known = ", ".join(item.id for item in self._runnable())
            return f"У скилла «{arguments.get('skill')}» нет шагов. Скиллы с шагами: {known or 'ни одного'}"
        arguments["inputs"] = _inputs(skill, arguments.get("inputs"))
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        skill = self._skills.get(arguments["skill"])
        return {
            "kind": "steps",
            "title": f"Запустить по шагам: «{skill.name}»",
            "run": {
                **run_view(skill),
                "values": _inputs(skill, arguments.get("inputs")),
                "effects": skill.steps.effects(),
                "problems": problems(skill, self._kit),
            },
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(problem, is_error=True)
        if workspace is None:
            return ToolResult("Шаги скилла работают в папке проекта: откройте сессию проекта", is_error=True)
        turn = CURRENT_TURN.get() or {}
        provider = self._providers.get(turn.get("provider_id", ""))
        if provider is None or not turn.get("model"):
            return ToolResult("Шагам нужна модель текущей сессии", is_error=True)
        skill: Skill = self._skills.get(arguments["skill"])
        window = await context_length(provider, turn["model"]) or self._default_window
        write = steps_writer()
        runner = StepRunner(
            skill, arguments["inputs"],
            RunContext(provider, turn["model"], workspace, window, self._kit, llm_env(provider, turn["model"])),
            on_progress=lambda snapshot: write({"type": "steps", "run": snapshot}),
        )
        snapshot = await runner.run()
        return ToolResult(report(snapshot), is_error=snapshot["status"] != "done", meta={"steps": snapshot})


def run_view(skill: Skill) -> dict:
    """Скилл с шагами одним словарём: id, имя, описание, что получится, поля и шаги — так его рисует карточка."""
    return {"id": skill.id, "name": skill.name, "description": skill.description, **skill.steps.view()}


def llm_env(provider: LLMProvider, model: str) -> dict[str, str]:
    """Модель сессии для скрипта шага: адрес OpenAI-совместимого API, модель, ключ и проверка сертификата."""
    env = {
        "MEEPO_LLM_URL": str(getattr(provider, "base_url", "")),
        "MEEPO_LLM_MODEL": model,
        "MEEPO_LLM_VERIFY_SSL": "true" if getattr(provider, "verify_ssl", True) else "false",
    }
    if key := getattr(provider, "api_key", ""):
        env["MEEPO_LLM_KEY"] = key
    return env


def _inputs(skill: Skill, raw: object) -> dict[str, str]:
    """Значения по именам «нужно». Маленькая модель переводит имена («папка» → «folder»): если чужих значений
    столько же, сколько пустых обязательных полей, они встают в эти поля по порядку; остальное лишнее отбрасываем."""
    values = {key: _text(value) for key, value in (raw if isinstance(raw, dict) else {}).items()}
    result = {item.name: values.get(item.name, "") for item in skill.steps.inputs}
    names = set(result)
    strangers = [value for key, value in values.items() if key not in names and value]
    empty = [item.name for item in skill.steps.inputs if not item.optional and not result[item.name]]
    if strangers and len(strangers) == len(empty):
        result.update(zip(empty, strangers, strict=True))
    return result


def _text(value: object) -> str:
    if isinstance(value, list):
        value = "\n".join(str(part) for part in value)
    return str(value).strip()[:MAX_INPUT_CHARS] if value is not None else ""


def steps_writer():
    """Поток хода, если инструмент вызван внутри графа; в остальных случаях прогресс просто не показывается."""
    try:
        return get_stream_writer()
    except (RuntimeError, KeyError):
        return lambda event: None
