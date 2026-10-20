"""«Настройки» → «Инструменты»: встроенные инструменты агента по файлам-темам — что делают, параметры,
нужна ли карточка подтверждения, исходный код. У браузера — своя настройка: показывать ли окно."""

import sys
from pathlib import Path

from fastapi import APIRouter, Request

from meepo.settings import ROOT

router = APIRouter(prefix="/api")

# Названия тем для списка; порядок — как в списке.
TITLES = {"files": "Файлы", "search": "Поиск", "command": "Команды", "browser": "Браузер", "skills": "Скиллы",
          "memory": "Память", "plan": "План"}
# Файлы темы кроме самого модуля: браузер снимает страницу скриптом snapshot.js.
EXTRA_FILES = {"browser": ["snapshot.js"]}


def _source(path: Path) -> dict:
    return {"file": path.relative_to(ROOT).as_posix(), "source": path.read_text(encoding="utf-8")}


def _parameters(tool) -> dict[str, str]:
    properties = tool.parameters.get("properties", {})
    return {name: spec.get("description") or ", ".join(spec.get("enum", [])) for name, spec in properties.items()}


@router.get("/tools")
def list_tools(request: Request) -> list[dict]:
    services = request.app.state.services
    themes: dict[str, dict] = {}
    for tool in services.agent.tools.values():
        module = sys.modules[tool.run.__module__]
        key = module.__name__.rsplit(".", 1)[-1]
        if key not in themes:
            path = Path(module.__file__)
            themes[key] = {
                "id": key, "title": TITLES.get(key, key), "doc": (module.__doc__ or "").strip(),
                "files": [_source(path), *(_source(path.with_name(name)) for name in EXTRA_FILES.get(key, []))],
                "tools": [],
            }
        themes[key]["tools"].append({"name": tool.name, "description": tool.description, "group": tool.group,
                                     "parameters": _parameters(tool)})
    if "browser" in themes:
        themes["browser"]["settings"] = {"browser_headed": services.settings().browser_headed}
    order = list(TITLES)
    return sorted(themes.values(), key=lambda theme: order.index(theme["id"]) if theme["id"] in order else len(order))
