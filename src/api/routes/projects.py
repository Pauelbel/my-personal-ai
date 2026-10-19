"""Маршруты проектов: список, создание, изменение и удаление."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from projects.models import Project
from projects.service import ProjectError, ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Пусто — имя папки.
    name: str = Field(default="", max_length=100)
    workspace: str = Field(min_length=1)
    # Пусто — провайдер по умолчанию из настроек моделей.
    provider: str | None = None
    model: str | None = None


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(default="", max_length=100)
    workspace: str = Field(min_length=1)
    mode: Literal["full", "read"] | None = None


def get_project_service(request: Request) -> ProjectService:
    return request.app.state.project_service


@router.get("", response_model=list[Project])
def list_projects(service: Annotated[ProjectService, Depends(get_project_service)]) -> list[Project]:
    return service.list()


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    request: Request,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> Project:
    try:
        return service.create(
            name=payload.name,
            workspace=payload.workspace,
            provider=payload.provider or request.app.state.default_provider,
            model=payload.model if payload.model is not None else request.app.state.settings.default_model,
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{project_id}", response_model=Project)
def update_project(
    project_id: str,
    payload: ProjectUpdate,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> Project:
    try:
        project = service.update(project_id, name=payload.name, workspace=payload.workspace, mode=payload.mode)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> None:
    # Вместе с проектом удаляются его сессии; папка на диске остаётся.
    try:
        deleted = service.delete(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Проект не найден")
