"""Запуск скрипта скилла.

Команда: `<python из data/skills-env> -s -X utf8 <папка скилла>/<entry>`, рабочая папка — папка проекта.
stdin — JSON аргументов; stdout — результат (модели — не больше OUTPUT_CHARS, длиннее — целиком в
`.meepo/skills/<name>/last-output.md`, модели — начало, конец и путь); stderr — строки прогресса, идут в UI
построчно. Код возврата не 0 — ошибка, модель получает хвост stderr.

Окружение чистое (без переменных пользователя), плюс MEEPO_* для доступа к модели и папке состояния,
PYTHONPATH с помощником meepo_skill и значения объявленных секретов. Флаг -I не используется: он отключил бы
PYTHONPATH и импорт соседних файлов scripts/; изоляцию от переменных пользователя даёт чистое окружение.
По таймауту и по «Стоп» убивается всё дерево процессов (Windows и Linux).
"""

import asyncio
import contextlib
import json
import os
import signal
import subprocess
from collections import deque
from collections.abc import Callable
from pathlib import Path

from meepo.skills.registry import Skill

SDK_DIR = Path(__file__).resolve().parent / "sdk"
OUTPUT_CHARS = 8000
HEAD_CHARS = 3000
TAIL_CHARS = 2000
ERROR_TAIL_CHARS = 2000
LINE_LIMIT = 1024 * 1024
KEPT_ERROR_LINES = 200


def state_dir(project: Path, name: str) -> Path:
    return project / ".meepo" / "skills" / name


def environment(project: Path, skill: Skill, llm: dict[str, str], secrets: dict[str, str]) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "LANG")
    env = {name: os.environ[name] for name in keep if name in os.environ}
    # Прокси нужен скиллам, которые ходят в сеть из корпоративной сети.
    env |= {name: value for name, value in os.environ.items() if "PROXY" in name.upper()}
    env |= {
        "MEEPO_LLM_URL": llm.get("url", ""), "MEEPO_LLM_MODEL": llm.get("model", ""), "MEEPO_LLM_KEY": llm.get("key", ""),
        "MEEPO_PROJECT": str(project), "MEEPO_SKILL_DIR": str(skill.folder),
        "MEEPO_STATE_DIR": str(state_dir(project, skill.name)),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(SDK_DIR), "PYTHONDONTWRITEBYTECODE": "1",
    }
    return {**env, **secrets}


async def run(skill: Skill, arguments: dict, *, project: Path, python: Path, env: dict[str, str],
              on_progress: Callable[[str], None]) -> tuple[str, bool]:
    """(текст для модели, ошибка ли)."""
    state_dir(project, skill.name).mkdir(parents=True, exist_ok=True)
    command = [str(python), "-s", "-X", "utf8", str(skill.folder / skill.entry)]
    # Своя группа процессов: по таймауту и «Стоп» убирается вместе со всем, что запустил скрипт.
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = await asyncio.create_subprocess_exec(
        *command, cwd=project, env=env, limit=LINE_LIMIT,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **group,
    )
    try:
        _, stdout, stderr, _ = await asyncio.wait_for(asyncio.gather(
            _feed(process, json.dumps(arguments, ensure_ascii=False).encode("utf-8")),
            process.stdout.read(), _lines(process, on_progress), process.wait(),
        ), skill.timeout)
    except TimeoutError:
        await kill_tree(process)
        return (f"Скилл {skill.name} не уложился в {skill.timeout} с и был остановлен. Повторный запуск продолжит "
                "с места остановки, если скрипт хранит прогресс в MEEPO_STATE_DIR."), True
    except asyncio.CancelledError:
        await kill_tree(process)
        raise
    output = stdout.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        details = "\n".join(part for part in (_tail(output, 1000), _tail(stderr, ERROR_TAIL_CHARS)) if part)
        return f"Скилл {skill.name} завершился с кодом {process.returncode}.\n{details}".strip(), True
    return _clip(output, project, skill.name) or "Скилл ничего не вывел в stdout", False


def _clip(output: str, project: Path, name: str) -> str:
    """Длинный результат — целиком в файл проекта, модели — начало, конец и путь."""
    if len(output) <= OUTPUT_CHARS:
        return output
    path = state_dir(project, name) / "last-output.md"
    path.write_text(output, encoding="utf-8")
    relative = path.relative_to(project).as_posix()
    return (f"{output[:HEAD_CHARS].rstrip()}\n\n… (результат длинный: {len(output)} символов; целиком — в файле "
            f"{relative}, читай его через read_file) …\n\n{output[-TAIL_CHARS:].lstrip()}")


async def _feed(process: asyncio.subprocess.Process, data: bytes) -> None:
    """Аргументы в stdin; скрипт может их и не читать — тогда он закроет вход раньше."""
    try:
        process.stdin.write(data)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()


async def _lines(process: asyncio.subprocess.Process, on_progress: Callable[[str], None]) -> str:
    """stderr по строкам, пока скрипт работает; в памяти — только хвост для текста ошибки."""
    kept: deque[str] = deque(maxlen=KEPT_ERROR_LINES)
    try:
        async for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            kept.append(line)
            if line.strip():
                on_progress(line)
    except (ValueError, asyncio.LimitOverrunError):
        kept.append((await process.stderr.read()).decode("utf-8", errors="replace"))
    return "\n".join(kept)


async def kill_tree(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(process.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    await process.wait()


def _tail(text: str | bytes, limit: int) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
