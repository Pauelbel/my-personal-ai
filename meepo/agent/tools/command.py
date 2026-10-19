"""run_command: команда в папке проекта (`pytest`, `git status`, `python x.py`), группа доверия command.

Команда идёт через оболочку системы (cmd на Windows, sh на Linux). PATH: сначала venv проекта (.venv или venv),
затем общее окружение скиллов — так `pytest` из пакетов, поставленных для скиллов, доступен и командам;
окружение самого Мипо из PATH убирается. Вывод модели — начало и конец; полный — в `.meepo/logs/`.
По таймауту и «Стоп» убивается всё дерево процессов.
"""

import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from meepo.agent.tools.base import Tool, ToolContext, ToolResult
from meepo.skills.runner import kill_tree

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1800
HEAD_CHARS = 2000
TAIL_CHARS = 4000


def _bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def environment(project: Path, skills_env: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    own = str(_bin(Path(sys.prefix)))
    paths = [item for item in env.get("PATH", "").split(os.pathsep) if item and item != own]
    env.pop("VIRTUAL_ENV", None)
    extra = [_bin(project / name) for name in (".venv", "venv") if _bin(project / name).is_dir()][:1]
    if extra:
        env["VIRTUAL_ENV"] = str(extra[0].parent)
    if skills_env is not None and _bin(skills_env).is_dir():
        extra.append(_bin(skills_env))
    env["PATH"] = os.pathsep.join([*map(str, extra), *paths])
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _timeout(args: dict) -> int:
    value = args.get("timeout", DEFAULT_TIMEOUT)
    if not 1 <= value <= MAX_TIMEOUT:
        raise ValueError(f"timeout — от 1 до {MAX_TIMEOUT} секунд")
    return value


def preview(args: dict, ctx: ToolContext) -> dict:
    if not args["command"].strip():
        raise ValueError("Пустая команда")
    return {"title": "Выполнить команду в папке проекта", "command": args["command"].strip(),
            "text": f"Таймаут {_timeout(args)} с."}


async def run_command(args: dict, ctx: ToolContext) -> ToolResult:
    command = args["command"].strip()
    if not command:
        raise ValueError("Пустая команда")
    timeout = _timeout(args)
    skills_env = ctx.services.get("env")
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = await asyncio.create_subprocess_shell(
        command, cwd=ctx.project, env=environment(ctx.project, skills_env.path if skills_env else None),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **group,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        stopped = ""
    except TimeoutError:
        await kill_tree(process)
        output, stopped = b"", f"\nКоманда не уложилась в {timeout} с и была остановлена."
    except asyncio.CancelledError:
        await kill_tree(process)
        raise
    text = output.decode("utf-8", errors="replace").strip()
    log = _save_log(ctx.project, command, text + stopped)
    shown = text if len(text) <= HEAD_CHARS + TAIL_CHARS else (
        f"{text[:HEAD_CHARS].rstrip()}\n\n… (вывод длинный: {len(text)} символов; целиком — в {log}) …\n\n"
        f"{text[-TAIL_CHARS:].lstrip()}")
    code = process.returncode
    head = f"$ {command}\nКод возврата: {code}{stopped}"
    return ToolResult(f"{head}\n\n{shown or '(вывода нет)'}", is_error=bool(stopped) or code != 0, meta={"log": log})


def _save_log(project: Path, command: str, text: str) -> str:
    folder = project / ".meepo" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w.-]+", "-", command)[:40].strip("-") or "command"
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.log"
    path.write_text(f"$ {command}\n\n{text}\n", encoding="utf-8")
    return path.relative_to(project).as_posix()


TOOLS = [
    Tool("run_command", "Выполнить команду в папке проекта: pytest, git status, python script.py и т. п. Вывод "
         "обрезается с концов, полный — в .meepo/logs/. Пользователь подтверждает команду.",
         {"type": "object", "properties": {
             "command": {"type": "string", "description": "Команда целиком, как в терминале"},
             "timeout": {"type": "integer", "description": f"Секунд на выполнение, по умолчанию {DEFAULT_TIMEOUT}"},
         }, "required": ["command"], "additionalProperties": False},
         run_command, "command", preview),
]
