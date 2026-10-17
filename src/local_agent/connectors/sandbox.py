"""Песочница для скриптов, которые написал агент.

Скрипт получает аргументы JSON-объектом в stdin и пишет результат в stdout. Ограничения:
- таймаут, лимиты процессора, памяти и размера файлов, обрезанный вывод;
- чистое окружение без переменных пользователя (токенов, ключей);
- если есть bubblewrap: файловая система только для чтения, домашняя папка скрыта,
  рабочая папка смонтирована на запись только инструментам с подтверждением,
  сеть закрыта, если инструмент не заявил, что она ему нужна.
Без bubblewrap остаются таймаут, лимиты и чистое окружение — об этом пишется в лог.
Пакеты сверх стандартной библиотеки — из общего окружения песочницы (pyenv.py), его ставит пользователь.
На Windows нет ни bubblewrap, ни лимитов процесса (модуля resource): остаются таймаут, обрезанный вывод
и чистое окружение.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from local_agent.core.tool import ToolResult

try:
    import resource
except ImportError:  # Windows: лимиты процесса задать нечем
    resource = None

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 20_000
# Лимит адресного пространства, а не занятой памяти: библиотеки (onnxruntime, numpy) резервируют его под поток
# на каждое ядро, и с 2 ГБ markitdown на 24 ядрах даже не стартует. Потоки этих библиотек ограничены THREADS.
MEMORY_LIMIT_BYTES = 8 * 1024**3
THREADS = "4"
FILE_SIZE_LIMIT_BYTES = 50 * 1024**2
# Строка stderr длиннее этого не разбирается построчно; хранится только хвост строк — для текста ошибки.
LINE_LIMIT = 1024**2
KEPT_ERROR_LINES = 200


@dataclass(frozen=True)
class SandboxPolicy:
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    network: bool = False
    # Писать в рабочую папку может только инструмент, вызовы которого подтверждает пользователь.
    writable_workspace: bool = False


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


async def run_script(
    script: Path, arguments: dict, workspace: Path | None, policy: SandboxPolicy, python: Path | None = None,
    env: dict[str, str] | None = None, on_line: Callable[[str], None] | None = None,
) -> ToolResult:
    """python — интерпретатор окружения песочницы с поставленными пакетами; без него — интерпретатор приложения.
    env — переменные для скрипта: секреты, которые инструмент объявил в tool.md, или доступ шага скилла к модели;
    on_line — строки stderr по мере появления: так долгий скрипт показывает, чем занят."""
    script = script.resolve()
    # Абсолютный путь, но без раскрытия ссылок: bin/python окружения — ссылка на системный интерпретатор.
    python = Path(os.path.abspath(python or sys.executable))
    with tempfile.TemporaryDirectory(prefix="meepo-script-") as scratch:
        cwd = workspace.resolve() if workspace else Path(scratch)
        command = [str(python), "-I", "-B", str(script)]
        bwrap = bwrap_path()
        if bwrap:
            command = [*_bwrap_arguments(bwrap, script, cwd, Path(scratch), python, policy), *command]
        else:
            logger.warning("bubblewrap не найден: скрипт %s запускается без изоляции файловой системы и сети", script)
        variables = {**_environment(scratch), **(env or {})}
        # preexec_fn и новая сессия процесса есть только в POSIX.
        posix = {"preexec_fn": lambda: _limits(policy.timeout), "start_new_session": True} if resource else {}
        process = await asyncio.create_subprocess_exec(
            *command, cwd=cwd, env=variables, limit=LINE_LIMIT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **posix,
        )
        try:
            _, stdout, stderr, _ = await asyncio.wait_for(asyncio.gather(
                _feed(process, json.dumps(arguments, ensure_ascii=False).encode()),
                process.stdout.read(), _lines(process, on_line), process.wait(),
            ), policy.timeout + 5)
        except TimeoutError:
            _kill(process)
            await process.wait()
            return ToolResult(f"Скрипт не уложился в {policy.timeout} с и был остановлен", is_error=True)
        except asyncio.CancelledError:
            # «Стоп» посреди долгого скрипта: процесс не должен работать дальше сам по себе.
            _kill(process)
            raise
    output = _clip(stdout.decode("utf-8", errors="replace"))
    if process.returncode != 0:
        # Ход работы идёт в stderr, итог — в stdout: у такого скрипта нужнее итог и хвост stderr (там трассировка).
        details = "\n".join(part for part in (output, _tail(stderr, 1500)) if part) if on_line else \
            _clip(stderr, 4000) or output
        return ToolResult(f"Скрипт завершился с кодом {process.returncode}.\n{details}".strip(), is_error=True)
    return ToolResult(output or "Скрипт ничего не вывел")


async def _feed(process: asyncio.subprocess.Process, data: bytes) -> None:
    """Аргументы в stdin; скрипт может и не читать их — тогда он закроет вход раньше."""
    try:
        process.stdin.write(data)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()


async def _lines(process: asyncio.subprocess.Process, on_line: Callable[[str], None] | None) -> str:
    """stderr по строкам, пока скрипт работает; в памяти — только хвост."""
    kept: deque[str] = deque(maxlen=KEPT_ERROR_LINES)
    try:
        async for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            kept.append(line)
            if on_line is not None and line.strip():
                on_line(line)
    except (ValueError, asyncio.LimitOverrunError):
        # Строка длиннее предела буфера: дочитываем остаток как есть.
        kept.append((await process.stderr.read()).decode("utf-8", errors="replace"))
    return "\n".join(kept)


def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if resource is not None:
            # У скрипта своя группа процессов: убираем её целиком, вместе с bwrap и тем, что запустил скрипт.
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()


def _bwrap_arguments(bwrap: str, script: Path, cwd: Path, scratch: Path, python: Path, policy: SandboxPolicy) -> list[str]:
    arguments = [
        bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
        # Домашняя папка и сокеты пользователя (ключи, ssh-agent, dbus) скрыты.
        "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/run", "--tmpfs", "/tmp",
        "--bind", str(scratch), str(scratch),
    ]
    # Окружение (python и библиотеки) нужно скрипту только на чтение: приложения или песочницы,
    # смотря чьим интерпретатором запущен скрипт; bin/python лежит в <окружение>/bin.
    environment = Path(sys.prefix) if python == Path(sys.executable) else python.parent.parent
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


def _environment(scratch: str) -> dict[str, str]:
    """Чистое окружение без переменных пользователя (токенов, ключей)."""
    common = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    common |= {name: THREADS for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")}
    if os.name == "nt":
        # Без SYSTEMROOT Python на Windows не стартует (нет криптопровайдера и сокетов).
        system = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC") if name in os.environ}
        return {**system, "PATH": os.environ.get("PATH", ""), "TEMP": scratch, "TMP": scratch,
                "USERPROFILE": scratch, **common}
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": scratch, "TMPDIR": scratch,
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", **common}


def _limits(timeout: int) -> None:
    os.nice(5)
    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE_LIMIT_BYTES, FILE_SIZE_LIMIT_BYTES))


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"\n… (вывод обрезан до {limit} символов)"
