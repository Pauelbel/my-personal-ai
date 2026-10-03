"""Файлы рабочей папки проекта: дерево по уровням, чтение, правка, удаление и перемещение внутри неё."""

import os
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from local_agent.api.routes.projects import get_project_service
from local_agent.projects.service import ProjectService
from local_agent.tools.filesystem import _inside_workspace

router = APIRouter(prefix="/projects/{project_id}/files", tags=["files"])

MAX_ENTRIES = 1000
MAX_PREVIEW_BYTES = 256 * 1024


class FileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool


class FileContent(BaseModel):
    path: str
    content: str
    truncated: bool


def _workspace(project_id: str, service: ProjectService) -> Path:
    project = service.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return Path(project.workspace)


def _target(workspace: Path, path: str) -> Path:
    try:
        return _inside_workspace(workspace, path or ".")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Не найдено") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Недоступный путь") from exc


@router.get("", response_model=list[FileEntry])
def list_files(
    project_id: str, service: Annotated[ProjectService, Depends(get_project_service)], path: str = "",
) -> list[FileEntry]:
    workspace = _workspace(project_id, service)
    folder = _target(workspace, path)
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail="Это не папка")
    root = workspace.resolve()
    entries = []
    try:
        with os.scandir(folder) as scan:
            for entry in scan:
                try:
                    # Ссылки, ведущие за пределы папки проекта, не показываем.
                    if not Path(entry.path).resolve(strict=True).is_relative_to(root):
                        continue
                    entries.append(FileEntry(
                        name=entry.name, is_dir=entry.is_dir(),
                        path=(Path(path) / entry.name).as_posix() if path else entry.name,
                    ))
                except OSError:
                    continue
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Нет доступа к папке") from exc
    entries.sort(key=lambda item: (not item.is_dir, item.name.casefold()))
    return entries[:MAX_ENTRIES]


@router.get("/content", response_model=FileContent)
def read_file(
    project_id: str, path: str, service: Annotated[ProjectService, Depends(get_project_service)],
) -> FileContent:
    file = _target(_workspace(project_id, service), path)
    if not file.is_file():
        raise HTTPException(status_code=400, detail="Это не файл")
    try:
        with file.open("rb") as handle:
            data = handle.read(MAX_PREVIEW_BYTES + 1)
    except OSError as exc:
        raise HTTPException(status_code=403, detail="Не удалось прочитать файл") from exc
    truncated = len(data) > MAX_PREVIEW_BYTES
    try:
        text = data[:MAX_PREVIEW_BYTES].decode("utf-8", errors="strict" if not truncated else "ignore")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="Двоичный файл: предпросмотр недоступен") from exc
    if "\x00" in text:
        raise HTTPException(status_code=415, detail="Двоичный файл: предпросмотр недоступен")
    return FileContent(path=path, content=text, truncated=truncated)


class FileUpdate(BaseModel):
    content: str


@router.put("/content", response_model=FileContent)
def write_file(
    project_id: str, path: str, payload: FileUpdate,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> FileContent:
    # Правится только уже существующий текстовый файл внутри рабочей папки: новых файлов и путей не создаётся.
    file = _target(_workspace(project_id, service), path)
    if not file.is_file():
        raise HTTPException(status_code=400, detail="Это не файл")
    data = payload.content.encode("utf-8")
    if len(data) > MAX_PREVIEW_BYTES:
        raise HTTPException(status_code=413, detail="Файл слишком большой для редактирования")
    try:
        file.write_bytes(data)
    except OSError as exc:
        raise HTTPException(status_code=403, detail="Не удалось сохранить файл") from exc
    return FileContent(path=path, content=payload.content, truncated=False)


class MoveRequest(BaseModel):
    new_path: str


def _lexical(workspace: Path, path: str) -> Path:
    """Путь без разыменования ссылки в конце: удаляется и переименовывается сама ссылка, а не её цель."""
    if not path or path in (".", "./"):
        raise HTTPException(status_code=400, detail="Нельзя менять корень проекта")
    parent = _target(workspace, str(Path(path).parent))
    target = parent / Path(path).name
    if not target.exists() and not target.is_symlink():
        raise HTTPException(status_code=404, detail="Не найдено")
    return target


@router.delete("", status_code=204)
def delete_entry(
    project_id: str, path: str, service: Annotated[ProjectService, Depends(get_project_service)],
) -> None:
    target = _lexical(_workspace(project_id, service), path)
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise HTTPException(status_code=403, detail="Не удалось удалить") from exc


@router.post("/move", response_model=FileEntry)
def move_entry(
    project_id: str, path: str, payload: MoveRequest,
    service: Annotated[ProjectService, Depends(get_project_service)],
) -> FileEntry:
    """Переименование и перенос — одна операция: новый путь целиком, внутри той же рабочей папки."""
    workspace = _workspace(project_id, service)
    source = _lexical(workspace, path)
    new_path = Path(payload.new_path)
    if not payload.new_path.strip() or new_path.name in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Укажите новое имя")
    destination = _target(workspace, str(new_path.parent)) / new_path.name
    if not destination.parent.is_dir():
        raise HTTPException(status_code=400, detail="Папка назначения не найдена")
    if destination.exists() or destination.is_symlink():
        raise HTTPException(status_code=409, detail="Элемент с таким именем уже есть")
    if source.is_dir() and not source.is_symlink() and destination.parent.is_relative_to(source.resolve()):
        raise HTTPException(status_code=400, detail="Нельзя переместить папку внутрь себя")
    try:
        source.rename(destination)
    except OSError as exc:
        raise HTTPException(status_code=403, detail="Не удалось переместить") from exc
    return FileEntry(name=destination.name, path=new_path.as_posix(), is_dir=destination.is_dir())
