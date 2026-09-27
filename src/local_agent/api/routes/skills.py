"""Маршруты навыков: список для UI, чтение и правка инструкций."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from local_agent.agent.loader import save_skill
from local_agent.agent.models import Skill
from local_agent.agent.registry import SkillRegistry

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillInfo(BaseModel):
    id: str
    name: str
    description: str


class SkillDetail(SkillInfo):
    instructions: str


class SkillUpdate(BaseModel):
    instructions: str


def get_skill_registry(request: Request) -> SkillRegistry:
    return request.app.state.skill_registry


def _detail(skill: Skill) -> SkillDetail:
    return SkillDetail(
        id=skill.id, name=skill.name, description=skill.description, instructions=skill.instructions
    )


@router.get("", response_model=list[SkillInfo])
def list_skills(
    registry: Annotated[SkillRegistry, Depends(get_skill_registry)],
) -> list[SkillInfo]:
    return [SkillInfo(id=skill.id, name=skill.name, description=skill.description) for skill in registry.all()]


@router.get("/{skill_id}", response_model=SkillDetail)
def read_skill(
    skill_id: str,
    registry: Annotated[SkillRegistry, Depends(get_skill_registry)],
) -> SkillDetail:
    skill = registry.get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Навык не найден")
    return _detail(skill)


@router.put("/{skill_id}", response_model=SkillDetail)
def save_skill_instructions(
    skill_id: str,
    payload: SkillUpdate,
    request: Request,
    registry: Annotated[SkillRegistry, Depends(get_skill_registry)],
) -> SkillDetail:
    # Сохраняем только навыки из реестра: так id из URL не может указать на произвольный файл.
    if registry.get(skill_id) is None:
        raise HTTPException(status_code=404, detail="Навык не найден")
    try:
        skill = save_skill(request.app.state.settings.skills_path, skill_id, payload.instructions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    registry.replace(skill)
    return _detail(skill)
