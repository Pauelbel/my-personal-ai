"""Реестр скиллов, загруженных из data/skills."""

from local_agent.skills.models import Skill


class SkillRegistry:
    def __init__(self, skills: list[Skill]) -> None:
        self._skills = {skill.id: skill for skill in skills}

    def get(self, skill_id: object) -> Skill | None:
        return self._skills.get(skill_id) if isinstance(skill_id, str) else None

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def replace(self, skill: Skill) -> None:
        self._skills[skill.id] = skill

    def remove(self, skill_id: str) -> None:
        self._skills.pop(skill_id, None)
