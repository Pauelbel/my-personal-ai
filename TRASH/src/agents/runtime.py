"""Подготовка встроенного Python-кода к запуску в изолированной песочнице."""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

TESTCASES_LAUNCHER = '''"""Точка запуска общего генератора тест-кейсов для шага агента."""

from qa.testcases import main

if __name__ == "__main__":
    raise SystemExit(main())
'''

# Точное содержимое старой встроенной заготовки. Дописанный пользователем код не мигрируем.
LEGACY_TESTCASES_LAUNCHER = '''"""Встроенная реализация генератора; общий код временно оставлен совместимым со старыми данными."""

import runpy
from pathlib import Path

import local_agent

source = Path(next(iter(local_agent.__path__))).resolve() / "skills" / "library" / "test-cases" / "scripts" / "testcases.py"
runpy.run_path(str(source), run_name="__main__")
'''


@contextmanager
def prepared_agent_script(script: Path) -> Iterator[Path]:
    """Точный встроенный launcher получает автономный runtime; пользовательский код запускается как есть.

    Генератор использует только стандартную библиотеку. Его временная копия доступна bubblewrap как обычный
    скрипт и не требует ни пакетов приложения в PythonEnv, ни доступа песочницы ко всему src.
    """
    if script.name != "testcases.py" or script.read_bytes().replace(b"\r\n", b"\n") != TESTCASES_LAUNCHER.encode("utf-8"):
        yield script
        return
    source = files("qa").joinpath("testcases.py").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="meepo-agent-runtime-") as scratch:
        executable = Path(scratch) / script.name
        executable.write_text(source, encoding="utf-8", newline="\n")
        yield executable
