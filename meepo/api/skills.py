"""«Настройки» → «Скиллы»: список, карточка скилла, ручная правка файлов, секреты, история и откат.

Значения секретов наружу не отдаются никогда: только «задан / не задан».
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from meepo.skills.env import requirement_name

router = APIRouter(prefix="/api/skills")


class FilesBody(BaseModel):
    files: dict[str, str]


class SecretBody(BaseModel):
    value: str


class RollbackBody(BaseModel):
    version: str


def _services(request: Request):
    return request.app.state.services


def _skill(request: Request, name: str):
    skill = _services(request).skills.get(name)
    if skill is None:
        raise HTTPException(404, "Скилл не найден")
    return skill


@router.get("")
def list_skills(request: Request) -> list[dict]:
    return [{"name": skill.name, "description": skill.description, "error": skill.error}
            for skill in _services(request).skills.list()]


@router.get("/{name}")
def skill_detail(name: str, request: Request) -> dict:
    services = _services(request)
    skill = _skill(request, name)
    installed = services.skills_env.packages()
    return {
        "name": skill.name, "description": skill.description, "error": skill.error,
        "timeout": skill.timeout, "files": services.skills.read_files(name),
        "requires": [{"spec": spec, "installed": installed.get(requirement_name(spec) or "")} for spec in skill.requires],
        "secrets": services.secrets.status(name, skill.secrets),
        "history": services.history.versions(name),
    }


@router.put("/{name}/files")
def save_files(name: str, body: FilesBody, request: Request) -> dict:
    """Ручная правка: прежняя версия уходит в историю, новая проверяется так же, как правка агента."""
    services = _services(request)
    skill = _skill(request, name)
    services.history.snapshot(skill.folder)
    try:
        services.skills.write(name, body.files, replace=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return skill_detail(name, request)


@router.put("/{name}/secrets/{secret}", status_code=204)
def set_secret(name: str, secret: str, body: SecretBody, request: Request) -> None:
    skill = _skill(request, name)
    if secret not in skill.secrets:
        raise HTTPException(400, f"Скилл {name} не объявляет секрет {secret}")
    try:
        _services(request).secrets.set(name, secret, body.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/{name}/secrets/{secret}", status_code=204)
def delete_secret(name: str, secret: str, request: Request) -> None:
    _skill(request, name)
    _services(request).secrets.delete(name, secret)


@router.post("/{name}/rollback")
def rollback(name: str, body: RollbackBody, request: Request) -> dict:
    skill = _skill(request, name)
    try:
        _services(request).history.restore(skill.folder, body.version)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return skill_detail(name, request)
