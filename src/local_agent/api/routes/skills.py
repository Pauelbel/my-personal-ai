"""Маршруты навыков: список для UI, чтение и правка инструкций."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from local_agent.agent.loader import (
    archive_skill,
    archived_skills,
    clear_skill_archive,
    create_skill,
    restore_skill,
    save_skill,
)
from local_agent.agent.models import Skill
from local_agent.agent.registry import SkillRegistry

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillInfo(BaseModel):
    id: str
    name: str
    description: str


class SkillDetail(SkillInfo):
    instructions: str


class ArchivedSkillInfo(SkillInfo):
    archive_id: str


class SkillUpdate(BaseModel):
    instructions: str


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=64 * 1024)


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


@router.post("", response_model=SkillDetail, status_code=201)
def add_skill(
    payload: SkillCreate,
    request: Request,
    registry: Annotated[SkillRegistry, Depends(get_skill_registry)],
) -> SkillDetail:
    try:
        skill = create_skill(
            request.app.state.settings.skills_path, payload.name, payload.description, payload.instructions
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    registry.replace(skill)
    return _detail(skill)


@router.get("/archive", response_model=list[ArchivedSkillInfo])
def list_archive(request: Request) -> list[ArchivedSkillInfo]:
    return [
        ArchivedSkillInfo(archive_id=archive_id, id=skill.id, name=skill.name, description=skill.description)
        for archive_id, skill in archived_skills(request.app.state.settings.skills_path)
    ]


@router.delete("/archive")
def clear_archive(request: Request) -> dict[str, int]:
    return {"deleted": clear_skill_archive(request.app.state.settings.skills_path)}


@router.post("/archive/{archive_id}/restore", response_model=SkillDetail)
def restore_archived_skill(
    archive_id: str,
    request: Request,
    registry: Annotated[SkillRegistry, Depends(get_skill_registry)],
) -> SkillDetail:
    try:
        skill = restore_skill(request.app.state.settings.skills_path, archive_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Запись архива не найдена") from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    registry.replace(skill)
    return _detail(skill)


@router.get("/{skill_id}", response_model=SkillDetail)
def read_skill(
    skill_id: str,
    registry: Annotated[SkillRegistry, Depends(get_skill_registry)],
) -> SkillDetail:
    skill = registry.get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Навык не найден")
    return _detail(skill)


@router.delete("/{skill_id}", status_code=204)
def delete_skill(
    skill_id: str,
    request: Request,
    registry: Annotated[SkillRegistry, Depends(get_skill_registry)],
) -> Response:
    if registry.get(skill_id) is None:
        raise HTTPException(status_code=404, detail="Навык не найден")
    try:
        archive_skill(request.app.state.settings.skills_path, skill_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Файл навыка не найден") from exc
    registry.remove(skill_id)
    return Response(status_code=204)


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
