"""Браузер папок для выбора рабочей папки: браузер не отдаёт странице настоящий путь к папке,
поэтому папки показывает сервер. Отдаются только имена папок — не файлы и не их содержимое."""

import os
import string
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/folders", tags=["folders"])

MAX_FOLDERS = 1000


class Folder(BaseModel):
    name: str
    path: str


class FolderListing(BaseModel):
    # None — корневой уровень: диски и домашняя папка.
    path: str | None
    parent: str | None
    folders: list[Folder]


@router.get("", response_model=FolderListing)
def list_folders(path: str | None = None) -> FolderListing:
    if not path:
        return FolderListing(path=None, parent=None, folders=_roots())
    folder = Path(path).expanduser()
    if not folder.is_absolute():
        raise HTTPException(status_code=400, detail="Нужен абсолютный путь")
    try:
        folder = folder.resolve(strict=True)
        if not folder.is_dir():
            raise HTTPException(status_code=400, detail="Это не папка")
        children = []
        with os.scandir(folder) as entries:
            for entry in entries:
                # Скрытые и системные папки ($Recycle.Bin, .git) рабочей папкой не бывают.
                if entry.name.startswith((".", "$")):
                    continue
                try:
                    if entry.is_dir():
                        children.append(Folder(name=entry.name, path=str(folder / entry.name)))
                except OSError:
                    continue
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Папка не найдена") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Нет доступа к этой папке") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Не удалось открыть папку") from exc
    children.sort(key=lambda item: item.name.casefold())
    parent = str(folder.parent) if folder.parent != folder else None
    return FolderListing(path=str(folder), parent=parent, folders=children[:MAX_FOLDERS])


def _roots() -> list[Folder]:
    home = Path.home()
    roots = [Folder(name=f"Домашняя папка ({home.name})", path=str(home))]
    if os.name == "nt":
        roots += [
            Folder(name=f"Диск {letter}:", path=f"{letter}:\\")
            for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()
        ]
    else:
        roots.append(Folder(name="Корень /", path="/"))
    return roots
