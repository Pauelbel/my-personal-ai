"""Уроки агента: пользователь говорит агенту «делай так впредь», агент дописывает правило в свой промпт.

Правило встаёт в раздел «Уроки» в конце промпта агента (data/agents/<id>.md) только после подтверждения
в карточке. Агент меняет только себя и только этот раздел: роль, инструменты и остальной промпт не трогаются.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

from local_agent.agent.models import Agent
from local_agent.capabilities.service import CapabilityService
from local_agent.tools.base import ToolResult

LESSONS_HEADER = "Уроки от пользователя (выполняй всегда):"
MAX_RULE_CHARS = 300
MAX_LESSONS = 30


def with_lesson(prompt: str, rule: str) -> str:
    """Промпт с новым правилом в разделе «Уроки»; раздела нет — он появляется в конце."""
    line = f"- {rule}"
    if LESSONS_HEADER not in prompt:
        return f"{prompt.rstrip()}\n\n{LESSONS_HEADER}\n{line}"
    head, lessons = prompt.split(LESSONS_HEADER, 1)
    return f"{head}{LESSONS_HEADER}\n{lessons.strip()}\n{line}".rstrip()


def lessons_of(prompt: str) -> list[str]:
    if LESSONS_HEADER not in prompt:
        return []
    section = prompt.split(LESSONS_HEADER, 1)[1]
    return [line[2:].strip() for line in section.splitlines() if line.startswith("- ")]


class LearnTool:
    id = "learn"
    name = "Запомнить урок"
    requires_approval = True
    description = (
        "Запомнить правило работы навсегда: дописать его в раздел «Уроки» своего промпта. Вызывай, когда "
        "пользователь просит делать так впредь («запомни», «в следующий раз», «всегда делай…»). Правило — "
        "одна строка-указание; пользователь подтвердит его в карточке"
    )
    parameters = {
        "type": "object",
        "properties": {
            "rule": {"type": "string", "description": "Правило одной строкой, повелительно: «Тест-кейсы оформляй таблицей с предусловиями»"},
        },
        "required": ["rule"],
        "additionalProperties": False,
    }

    def __init__(self, capabilities: CapabilityService, agent: Agent) -> None:
        self._capabilities = capabilities
        self._agent_id = agent.id

    def validate(self, arguments: dict[str, object]) -> str | None:
        rule = arguments.get("rule")
        if not isinstance(rule, str) or not rule.strip():
            return "rule — правило одной строкой"
        arguments["rule"] = " ".join(rule.split())
        if len(arguments["rule"]) > MAX_RULE_CHARS:
            return f"Правило — не длиннее {MAX_RULE_CHARS} символов: сформулируй короче"
        agent = self._capabilities.agents.get(self._agent_id)
        if agent is None:
            return "Агент не найден"
        lessons = lessons_of(agent.system_prompt)
        if arguments["rule"] in lessons:
            return "Такое правило уже есть в уроках"
        if len(lessons) >= MAX_LESSONS:
            return f"Уроков уже {MAX_LESSONS}: попроси пользователя убрать лишние в «Возможности → Агенты → промпт»"
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        agent = self._capabilities.agents.get(self._agent_id)
        lessons = [*lessons_of(agent.system_prompt), arguments["rule"]] if agent else [arguments["rule"]]
        return {
            "kind": "tool_call",
            "title": f"Запомнить урок агенту «{agent.name if agent else self._agent_id}»",
            "preview": f"{LESSONS_HEADER}\n" + "\n".join(f"- {lesson}" for lesson in lessons),
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(problem, is_error=True)
        agent = self._capabilities.agents.get(self._agent_id)
        updated = replace(agent, system_prompt=with_lesson(agent.system_prompt, arguments["rule"]))
        try:
            await asyncio.to_thread(self._capabilities.save_agent, updated)
        except (OSError, ValueError) as exc:
            return ToolResult(f"Не удалось сохранить урок: {exc}", is_error=True)
        return ToolResult(f"Урок записан в промпт: «{arguments['rule']}». Он действует с этого момента и во всех следующих задачах.")
