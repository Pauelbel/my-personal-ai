"""Песочница для скриптов, которые написал агент.

Скрипт получает аргументы JSON-объектом в stdin и пишет результат в stdout. Ограничения:
- таймаут, лимиты процессора, памяти и размера файлов, обрезанный вывод;
- чистое окружение без переменных пользователя (токенов, ключей);
- если есть bubblewrap: файловая система только для чтения, домашняя папка скрыта,
  рабочая папка смонтирована на запись только инструментам с подтверждением,
  сеть закрыта, если инструмент не заявил, что она ему нужна.
Без bubblewrap остаются таймаут, лимиты и чистое окружение — об этом пишется в лог.
"""

import asyncio
import json
import logging
import os
import resource
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from local_agent.tools.base import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 20_000
MEMORY_LIMIT_BYTES = 2 * 1024**3
FILE_SIZE_LIMIT_BYTES = 50 * 1024**2


@dataclass(frozen=True)
class SandboxPolicy:
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    network: bool = False
    # Писать в рабочую папку может только инструмент, вызовы которого подтверждает пользователь.
    writable_workspace: bool = False


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


async def run_script(script: Path, arguments: dict, workspace: Path | None, policy: SandboxPolicy) -> ToolResult:
    script = script.resolve()
    python = Path(sys.executable)
    with tempfile.TemporaryDirectory(prefix="meepo-script-") as scratch:
        cwd = workspace.resolve() if workspace else Path(scratch)
        command = [str(python), "-I", "-B", str(script)]
        bwrap = bwrap_path()
        if bwrap:
            command = [*_bwrap_arguments(bwrap, script, cwd, Path(scratch), python, policy), *command]
        else:
            logger.warning("bubblewrap не найден: скрипт %s запускается без изоляции файловой системы и сети", script)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": scratch, "TMPDIR": scratch,
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        }
        process = await asyncio.create_subprocess_exec(
            *command, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            preexec_fn=lambda: _limits(policy.timeout), start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(arguments, ensure_ascii=False).encode()), policy.timeout + 5,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(f"Скрипт не уложился в {policy.timeout} с и был остановлен", is_error=True)
    output = _clip(stdout.decode("utf-8", errors="replace"))
    if process.returncode != 0:
        error = _clip(stderr.decode("utf-8", errors="replace"), 4000)
        return ToolResult(f"Скрипт завершился с кодом {process.returncode}.\n{error or output}".strip(), is_error=True)
    return ToolResult(output or "Скрипт ничего не вывел")


def _bwrap_arguments(bwrap: str, script: Path, cwd: Path, scratch: Path, python: Path, policy: SandboxPolicy) -> list[str]:
    arguments = [
        bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        # Домашняя папка и сокеты пользователя (ключи, ssh-agent, dbus) скрыты.
        "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/run", "--tmpfs", "/tmp",
        "--bind", str(scratch), str(scratch),
    ]
    # Окружение приложения (python и библиотеки) нужно скрипту только на чтение.
    environment = Path(sys.prefix)
    for path in dict.fromkeys([environment, python.parent, script.parent]):
        arguments += ["--ro-bind", str(path), str(path)]
    if policy.writable_workspace:
        arguments += ["--bind", str(cwd), str(cwd)]
    else:
        arguments += ["--ro-bind", str(cwd), str(cwd)]
    if not policy.network:
        arguments.append("--unshare-net")
    else:
        # Без /etc/resolv.conf из /run имена не разрешатся: возвращаем его, если он ссылка в /run.
        resolv = Path("/etc/resolv.conf").resolve()
        if str(resolv).startswith("/run/") and resolv.exists():
            arguments += ["--ro-bind", str(resolv), str(resolv)]
    return [*arguments, "--chdir", str(cwd), "--"]


def _limits(timeout: int) -> None:
    os.nice(5)
    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE_LIMIT_BYTES, FILE_SIZE_LIMIT_BYTES))


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"\n… (вывод обрезан до {limit} символов)"
