"""Общее Python-окружение скиллов `data/skills-env/`: в нём пакеты, которые пользователь разрешил поставить.

Окружение отдельное от окружения приложения: пакет скилла не сломает сервер. Создаётся при первой
надобности тем же интерпретатором, что у приложения. Ставит пакеты uv, если он есть, иначе pip окружения.
Принимаются только имена пакетов PyPI с версией: ссылки, пути и флаги pip отклоняются — иначе через
«пакет» можно было бы выполнить что угодно.
"""

import asyncio
import os
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

INSTALL_TIMEOUT_SECONDS = 600
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


def check_requirements(requirements: object) -> str | None:
    if not isinstance(requirements, list):
        return "Пакеты — списком, например [\"pypdf==5.1.0\"]"
    if len(requirements) > MAX_PACKAGES:
        return f"Не больше {MAX_PACKAGES} пакетов"
    bad = [str(spec) for spec in requirements if not isinstance(spec, str) or requirement_name(spec) is None]
    if bad:
        return f"Это не имена пакетов PyPI: {', '.join(bad)}. Ссылки, пути и флаги pip не принимаются"
    return None


class SkillsEnv:
    def __init__(self, path: Path) -> None:
        # Без раскрытия ссылок: bin/python окружения — ссылка на системный интерпретатор.
        self.path = Path(os.path.abspath(path))
        self._lock = asyncio.Lock()

    @property
    def python(self) -> Path:
        return self.path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @property
    def exists(self) -> bool:
        return self.python.exists()

    def packages(self) -> dict[str, str]:
        """Установленные пакеты: нормализованное имя → версия; pip и setuptools самого окружения не в счёт."""
        if not self.exists:
            return {}
        paths = [self.path / "Lib" / "site-packages"] if os.name == "nt" else sorted(self.path.glob("lib/python*/site-packages"))
        found = {}
        for dist in metadata.distributions(path=[str(path) for path in paths]):
            name = requirement_name(dist.metadata["Name"] or "")
            if name and name not in {"pip", "setuptools"}:
                found[name] = dist.version
        return dict(sorted(found.items()))

    def missing(self, requirements: list[str]) -> list[str]:
        installed = self.packages()
        return [spec for spec in requirements if requirement_name(spec) not in installed]

    async def ensure(self) -> None:
        """Создать окружение, если его ещё нет. Ошибка — RuntimeError с выводом команды."""
        async with self._lock:
            if self.exists:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            uv = shutil.which("uv")
            base = getattr(sys, "_base_executable", sys.executable)
            command = [uv, "venv", "--python", base, str(self.path)] if uv else [base, "-m", "venv", str(self.path)]
            await _run(command)

    async def install(self, requirements: list[str]) -> str:
        """Ставит пакеты; возвращает «имя версия» через запятую. Ошибка — ValueError или RuntimeError."""
        if problem := check_requirements(requirements):
            raise ValueError(problem)
        if not requirements:
            return ""
        await self.ensure()
        async with self._lock:
            uv = shutil.which("uv")
            command = [uv, "pip", "install", "--python", str(self.python)] if uv \
                else [str(self.python), "-m", "pip", "install", "--disable-pip-version-check"]
            await _run([*command, *requirements])
        installed = self.packages()
        return ", ".join(f"{name} {installed.get(name, '?')}" for name in map(requirement_name, requirements))


async def _run(command: list[str]) -> str:
    # Ключи и токены пользователя установщику не нужны; PATH и HOME — нужны (кэш uv и pip), прокси — тоже.
    keep = ("PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP", "LANG", "USERPROFILE", "LOCALAPPDATA", "APPDATA")
    env = {name: os.environ[name] for name in keep if name in os.environ}
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
        raise RuntimeError(f"Установка не уложилась в {INSTALL_TIMEOUT_SECONDS} с и была остановлена") from None
    text = output.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise RuntimeError(f"Команда завершилась с кодом {process.returncode}:\n{text[-3000:]}")
    return text
