"""Вкладка «Файлы»: папка проекта — дерево по уровням, чтение, правка, удаление и перемещение внутри неё.

Это ручная работа пользователя, поэтому без карточек; выйти за папку проекта нельзя так же, как агенту.
"""

import os
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from meepo.agent.tools.base import inside

router = APIRouter(prefix="/api/projects/{project_id}/files")

MAX_ENTRIES = 1000
MAX_PREVIEW_BYTES = 256 * 1024


class FileUpdate(BaseModel):
    content: str


class MoveRequest(BaseModel):
    new_path: str


def _workspace(request: Request, project_id: str) -> Path:
    project = request.app.state.services.projects.get(project_id)
    if project is None:
        raise HTTPException(404, "Проект не найден")
    return Path(project["path"])


def _target(workspace: Path, path: str) -> Path:
    try:
        return inside(workspace, path or ".")
    except ValueError as exc:
        raise HTTPException(404 if str(exc).startswith("Не найдено") else 400, str(exc)) from exc


def _relative(workspace: Path, target: Path) -> str:
    return target.relative_to(workspace.resolve()).as_posix()


@router.get("")
def list_files(project_id: str, request: Request, path: str = "") -> list[dict]:
    workspace = _workspace(request, project_id)
    folder = _target(workspace, path)
    if not folder.is_dir():
        raise HTTPException(400, "Это не папка")
    root = workspace.resolve()
    entries = []
    try:
        with os.scandir(folder) as scan:
            for entry in scan:
                try:
                    # Ссылки, ведущие за пределы папки проекта, не показываем.
                    if not Path(entry.path).resolve(strict=True).is_relative_to(root):
                        continue
                    entries.append({"name": entry.name, "is_dir": entry.is_dir(),
                                    "path": (Path(path) / entry.name).as_posix() if path else entry.name})
                except OSError:
                    continue
    except PermissionError as exc:
        raise HTTPException(403, "Нет доступа к папке") from exc
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].casefold()))
    return entries[:MAX_ENTRIES]


@router.get("/content")
def read_file(project_id: str, path: str, request: Request) -> dict:
    file = _target(_workspace(request, project_id), path)
    if not file.is_file():
        raise HTTPException(400, "Это не файл")
    try:
        with file.open("rb") as handle:
            data = handle.read(MAX_PREVIEW_BYTES + 1)
    except OSError as exc:
        raise HTTPException(403, "Не удалось прочитать файл") from exc
    truncated = len(data) > MAX_PREVIEW_BYTES
    try:
        text = data[:MAX_PREVIEW_BYTES].decode("utf-8", errors="ignore" if truncated else "strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(415, "Двоичный файл: предпросмотр недоступен") from exc
    if "\x00" in text:
        raise HTTPException(415, "Двоичный файл: предпросмотр недоступен")
    return {"path": path, "content": text, "truncated": truncated}


@router.put("/content")
def write_file(project_id: str, path: str, payload: FileUpdate, request: Request) -> dict:
    """Правится только уже существующий текстовый файл: новых файлов и путей отсюда не создаётся."""
    file = _target(_workspace(request, project_id), path)
    if not file.is_file():
        raise HTTPException(400, "Это не файл")
    data = payload.content.encode("utf-8")
    if len(data) > MAX_PREVIEW_BYTES:
        raise HTTPException(413, "Файл слишком большой для редактирования")
    try:
        file.write_bytes(data)
    except OSError as exc:
        raise HTTPException(403, "Не удалось сохранить файл") from exc
    return {"path": path, "content": payload.content, "truncated": False}


def _lexical(workspace: Path, path: str) -> Path:
    """Путь без разыменования ссылки в конце: удаляется и переименовывается сама ссылка, а не её цель."""
    if not path or path in (".", "./"):
        raise HTTPException(400, "Нельзя менять корень проекта")
    target = _target(workspace, str(Path(path).parent)) / Path(path).name
    if not target.exists() and not target.is_symlink():
        raise HTTPException(404, "Не найдено")
    return target


@router.delete("", status_code=204)
def delete_entry(project_id: str, path: str, request: Request) -> None:
    target = _lexical(_workspace(request, project_id), path)
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise HTTPException(403, "Не удалось удалить") from exc


@router.post("/move")
def move_entry(project_id: str, path: str, payload: MoveRequest, request: Request) -> dict:
    """Переименование и перенос — одна операция: новый путь целиком, внутри той же папки проекта."""
    workspace = _workspace(request, project_id)
    source = _lexical(workspace, path)
    new_path = Path(payload.new_path)
    if not payload.new_path.strip() or new_path.name in ("", ".", ".."):
        raise HTTPException(400, "Укажите новое имя")
    destination = _target(workspace, str(new_path.parent)) / new_path.name
    if not destination.parent.is_dir():
        raise HTTPException(400, "Папка назначения не найдена")
    if destination.exists() or destination.is_symlink():
        raise HTTPException(409, "Элемент с таким именем уже есть")
    if source.is_dir() and not source.is_symlink() and destination.parent.is_relative_to(source.resolve()):
        raise HTTPException(400, "Нельзя переместить папку внутрь себя")
    try:
        source.rename(destination)
    except OSError as exc:
        raise HTTPException(403, "Не удалось переместить") from exc
    return {"name": destination.name, "path": new_path.as_posix(), "is_dir": destination.is_dir()}
