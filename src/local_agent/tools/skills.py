"""Инструмент навыков отдаёт модели инструкции навыка, когда она решила им воспользоваться."""

from collections.abc import Collection
from pathlib import Path

from local_agent.agent.models import Skill
from local_agent.agent.registry import SkillRegistry
from local_agent.tools.base import ToolResult


class UseSkillTool:
    id = "use_skill"
    name = "Навыки"
    description = "Загрузить инструкции навыка по его id. Список навыков с описаниями — в системном промпте."
    requires_approval = False

    def __init__(
        self,
        skills: SkillRegistry,
        known_tools: Collection[str] = (),
        available_tools: Collection[str] = (),
        allowed_skills: Collection[str] | None = None,
    ) -> None:
        self._skills = skills
        # Все инструменты приложения и те, что доступны в этом ходе: по ним видно, чего навыку не хватит.
        self._known_tools = set(known_tools)
        self._available_tools = set(available_tools)
        # None — агенту доступен весь каталог.
        self._allowed_skills = None if allowed_skills is None else set(allowed_skills)

    def skills(self) -> list[Skill]:
        return [
            skill for skill in self._skills.all()
            if self._allowed_skills is None or skill.id in self._allowed_skills
        ]

    @property
    def parameters(self) -> dict[str, object]:
        # enum подсказывает модели точные id и не даёт выдумать несуществующий навык.
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [skill.id for skill in self.skills()],
                    "description": "id навыка",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        skills = {skill.id: skill for skill in self.skills()}
        skill = skills.get(arguments.get("name"))
        if skill is None:
            return ToolResult(f"Такого навыка нет. Доступные: {', '.join(skills)}", is_error=True)
        content = f"Навык «{skill.name}». Выполни задачу по этим инструкциям:\n\n{skill.instructions}"
        # Без нужных инструментов навык не выполнить: пусть модель скажет об этом, а не ответит «нет данных».
        missing = sorted(
            tool for tool in self._known_tools - self._available_tools if tool in skill.instructions
        )
        if missing:
            content += (
                "\n\nВНИМАНИЕ: в этой сессии недоступны инструменты, которые нужны навыку: "
                + ", ".join(missing)
                + ". Не пытайся выполнить навык без них. Сообщи пользователю, что для этой задачи нужно "
                "открыть сессию в проекте с папкой и включить эти инструменты в разделе «Инструменты»."
            )
        return ToolResult(content)
