"""Общее Python-окружение песочницы: в нём живут пакеты, которые скриптам агента поставили с согласия пользователя.

Окружение отдельное от окружения приложения (data/sandbox-env): пакет, поставленный агентом, не сломает сервер.
Скрипты запускаются его интерпретатором; внутри bubblewrap оно смонтировано только на чтение.
Ставит пакеты uv, если он есть, иначе pip самого окружения. Принимаются только имена пакетов с версией
из PyPI: ссылки, пути и флаги pip отклоняются, иначе через «пакет» можно было бы выполнить что угодно.
"""

import asyncio
import logging
import os
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

from local_agent.tools.base import ToolResult

logger = logging.getLogger(__name__)

INSTALL_TIMEOUT_SECONDS = 300
MAX_PACKAGES = 10
# Имя пакета, необязательные extras и ограничения версии: pypdf, lxml>=5, pdfplumber==0.11.4, httpx[http2]<1.
REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:\s*(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+"
    r"(?:\s*,\s*(?:===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+)*)?$"
)


def requirement_name(spec: str) -> str | None:
    """Нормализованное имя пакета (PEP 503) или None, если строка — не требование из PyPI."""
    match = REQUIREMENT.fullmatch(spec.strip())
    return re.sub(r"[-_.]+", "-", match["name"]).lower() if match else None


class PythonEnv:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    @property
    def python(self) -> Path:
        # Путь не разрешаем: ссылка bin/python ведёт на системный интерпретатор, и без неё окружение не найдётся.
        return self.path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @property
    def exists(self) -> bool:
        return self.python.exists()

    def _site_packages(self) -> list[Path]:
        if os.name == "nt":
            return [self.path / "Lib" / "site-packages"]
        return sorted(self.path.glob("lib/python*/site-packages"))

    def packages(self) -> dict[str, str]:
        """Установленные пакеты: нормализованное имя → версия. pip и setuptools самого окружения не в счёт."""
        if not self.exists:
            return {}
        found = {}
        for dist in metadata.distributions(path=[str(path) for path in self._site_packages()]):
            name = requirement_name(dist.metadata["Name"] or "")
            if name and name not in {"pip", "setuptools"}:
                found[name] = dist.version
        return dict(sorted(found.items()))

    def missing(self, requirements: list[str]) -> list[str]:
        installed = self.packages()
        return [spec for spec in requirements if requirement_name(spec) not in installed]

    async def install(self, requirements: list[str]) -> ToolResult:
        if problem := check_requirements(requirements):
            return ToolResult(problem, is_error=True)
        async with self._lock:
            if not self.exists and (failure := await self._create()):
                return failure
            uv = shutil.which("uv")
            command = [uv, "pip", "install", "--python", str(self.python)] if uv \
                else [str(self.python), "-m", "pip", "install", "--disable-pip-version-check"]
            result = await _run([*command, *requirements])
        if result.is_error:
            return result
        installed = self.packages()
        versions = ", ".join(f"{name} {installed.get(name, '?')}" for name in map(requirement_name, requirements))
        return ToolResult(f"Установлено в песочницу: {versions}. Скрипты инструментов теперь могут их импортировать.")

    async def uninstall(self, name: str) -> ToolResult:
        normalized = requirement_name(name)
        if normalized is None or normalized not in self.packages():
            return ToolResult(f"Пакета {name} в песочнице нет", is_error=True)
        async with self._lock:
            uv = shutil.which("uv")
            command = [uv, "pip", "uninstall", "--python", str(self.python), normalized] if uv \
                else [str(self.python), "-m", "pip", "uninstall", "-y", normalized]
            return await _run(command)

    async def _create(self) -> ToolResult | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        uv = shutil.which("uv")
        # Тот же интерпретатор, что у приложения: версия Python у скриптов не меняется.
        base = getattr(sys, "_base_executable", sys.executable)
        command = [uv, "venv", "--python", base, str(self.path)] if uv else [base, "-m", "venv", str(self.path)]
        result = await _run(command)
        if result.is_error:
            return ToolResult(f"Не удалось создать окружение песочницы {self.path}: {result.content}", is_error=True)
        logger.info("Создано окружение песочницы %s", self.path)
        return None


def check_requirements(requirements: object) -> str | None:
    if not isinstance(requirements, list) or not requirements:
        return "Укажите пакеты списком, например [\"pypdf\"]"
    if len(requirements) > MAX_PACKAGES:
        return f"Не больше {MAX_PACKAGES} пакетов за раз"
    bad = [str(spec) for spec in requirements if not isinstance(spec, str) or requirement_name(spec) is None]
    if bad:
        return f"Это не имена пакетов PyPI: {', '.join(bad)}. Ссылки, пути и флаги pip не принимаются"
    return None


async def _run(command: list[str]) -> ToolResult:
    # Ключи и токены пользователя установщику не нужны; PATH и HOME — нужны (кэш uv и pip).
    env = {name: os.environ[name] for name in ("PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP", "LANG") if name in os.environ}
    env |= {name: value for name, value in os.environ.items() if name.startswith(("UV_", "PIP_")) or "PROXY" in name.upper()}
    process = await asyncio.create_subprocess_exec(
        *command, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), INSTALL_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ToolResult(f"Установка не уложилась в {INSTALL_TIMEOUT_SECONDS} с и была остановлена", is_error=True)
    text = output.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        return ToolResult(f"Команда завершилась с кодом {process.returncode}.\n{text[-3000:]}".strip(), is_error=True)
    return ToolResult(text[-3000:] or "Готово")
