"""Папки специализированных агентов: AGENT.md, steps.md и scripts/."""

import re
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from agents.models import Agent
from agents.runtime import LEGACY_TESTCASES_LAUNCHER, TESTCASES_LAUNCHER
from shared import frontmatter
from steps.files import parse_steps

AGENT_FILE = "AGENT.md"
STEPS_FILE = "steps.md"
SCRIPTS = "scripts"
AGENT_KEYS = {"name", "description", "tools"}
AGENT_ID = frontmatter.ITEM_ID
LIBRARY = Path(__file__).resolve().parent / "library"
ARCHIVE = ".deleted"
ARCHIVED = re.compile(r"(?P<id>[a-z0-9][a-z0-9_-]*)\.[0-9a-f]{32}")
SCRIPT_FILE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
MAX_FILE_BYTES = 512 * 1024
TESTCASES_MIGRATION = ".migrations/testcases-runtime-v1/test-cases/scripts/testcases.py"


def _list(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def load_agents(root: Path) -> list[Agent]:
    root.mkdir(parents=True, exist_ok=True)
    return [read_agent(path) for path in sorted(root.iterdir()) if path.is_dir() and not path.name.startswith(".")]


def read_agent(folder: Path, agent_id: str | None = None) -> Agent:
    agent_id = folder.name if agent_id is None else agent_id
    if not AGENT_ID.fullmatch(agent_id):
        raise ValueError(f"{folder}: неверный id агента")
    path = folder / AGENT_FILE
    if not path.is_file():
        raise ValueError(f"{folder}: нет файла {AGENT_FILE}")
    header, instructions = frontmatter.split(path)
    fields = frontmatter.fields(path, header, AGENT_KEYS)
    if not fields.get("description"):
        raise ValueError(f"{path}: у агента должно быть описание")
    steps_path = folder / STEPS_FILE
    steps = parse_steps(steps_path.read_text(encoding="utf-8"), steps_path) if steps_path.is_file() else None
    if not instructions.strip() and steps is None:
        raise ValueError(f"{path}: пустые инструкции агента")
    return Agent(
        agent_id, fields.get("name") or agent_id, instructions.strip(), "", "",
        fields["description"], steps, folder, _list(fields.get("tools", "")),
    )


def files_of(folder: Path) -> dict[str, str]:
    found = {name: (folder / name).read_text(encoding="utf-8") for name in (AGENT_FILE, STEPS_FILE) if (folder / name).is_file()}
    scripts = folder / SCRIPTS
    for path in sorted(scripts.iterdir()) if scripts.is_dir() else []:
        if path.is_file() and SCRIPT_FILE.fullmatch(path.name) and path.stat().st_size <= MAX_FILE_BYTES:
            found[f"{SCRIPTS}/{path.name}"] = path.read_text(encoding="utf-8")
    return found


def _check_name(name: str) -> None:
    if name in (AGENT_FILE, STEPS_FILE):
        return
    folder, _, file = name.partition("/")
    if folder != SCRIPTS or not SCRIPT_FILE.fullmatch(file):
        raise ValueError(f"Файл «{name}»: допустимы AGENT.md, steps.md и scripts/<имя>")


def save_files(root: Path, agent_id: str, files: dict[str, str | None], *, create: bool = False) -> Agent:
    if not AGENT_ID.fullmatch(agent_id):
        raise ValueError("id агента — строчные латинские буквы, цифры, - и _")
    for name, text in files.items():
        _check_name(name)
        if text is not None:
            if len(text.encode()) > MAX_FILE_BYTES:
                raise ValueError(f"{name}: файл слишком большой")
            if name.endswith(".py"):
                compile(text, name, "exec")
    if files.get(AGENT_FILE, "") is None:
        raise ValueError("AGENT.md удалить нельзя")
    target = root / agent_id
    if create and target.exists():
        raise FileExistsError(f"Агент {agent_id} уже есть")
    if not create and not (target / AGENT_FILE).is_file():
        raise FileNotFoundError("Агента нет")
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{agent_id}.{uuid4().hex}"
    try:
        shutil.copytree(target, staging) if target.exists() else staging.mkdir()
        for name, text in files.items():
            path = staging / name
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        read_agent(staging, agent_id)
        if target.exists():
            old = root / f".{agent_id}.{uuid4().hex}.old"
            target.rename(old)
            staging.rename(target)
            shutil.rmtree(old)
        else:
            staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return read_agent(target)


def archive_agent(root: Path, agent_id: str) -> None:
    target = root / agent_id
    if not target.is_dir():
        raise FileNotFoundError("Агента нет")
    archive = root / ARCHIVE
    archive.mkdir(exist_ok=True)
    target.rename(archive / f"{agent_id}.{uuid4().hex}")


def archived_agents(root: Path) -> list[tuple[str, Agent]]:
    archive = root / ARCHIVE
    found = []
    for path in sorted(archive.iterdir()) if archive.is_dir() else []:
        if path.is_dir() and (match := ARCHIVED.fullmatch(path.name)):
            try:
                found.append((path.name, read_agent(path, match["id"])))
            except ValueError:
                pass
    return found


def restore_agent(root: Path, archive_id: str) -> Agent:
    match = ARCHIVED.fullmatch(archive_id)
    source = root / ARCHIVE / archive_id
    if match is None or not source.is_dir():
        raise FileNotFoundError("Записи архива нет")
    target = root / match["id"]
    if target.exists():
        raise FileExistsError("Агент с таким id уже есть")
    source.rename(target)
    if match["id"] == "test-cases":
        migrate_legacy_testcases(root)
    return read_agent(target)


def library_agents() -> list[Agent]:
    return load_agents(LIBRARY) if LIBRARY.is_dir() else []


def migrate_legacy_testcases(root: Path) -> Path | None:
    """Обновляет только узнаваемый старый launcher, сохраняя его копию и остальные файлы агента."""
    script = root / "test-cases" / SCRIPTS / "testcases.py"
    if not script.is_file() or script.is_symlink():
        return None
    original = script.read_bytes()
    if original.replace(b"\r\n", b"\n") != LEGACY_TESTCASES_LAUNCHER.encode("utf-8"):
        return None
    backup = root / TESTCASES_MIGRATION
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists() and backup.read_bytes() != original:
        backup = backup.with_name(f"testcases.{uuid4().hex}.py")
    if not backup.exists():
        shutil.copy2(script, backup)
    with tempfile.TemporaryDirectory(dir=script.parent, prefix=".testcases-migration-") as scratch:
        replacement = Path(scratch) / script.name
        replacement.write_text(TESTCASES_LAUNCHER, encoding="utf-8", newline="\n")
        shutil.copymode(script, replacement)
        if script.read_bytes() != original:
            return None
        replacement.replace(script)
    return backup


def initialize_agents(root: Path) -> list[Agent]:
    """При первом запуске кладёт три редактируемых заготовки в data/agents; архивирование не включает их снова."""
    marker = root / ".initialized"
    if not marker.exists():
        root.mkdir(parents=True, exist_ok=True)
        for agent in library_agents():
            target = root / agent.id
            if not target.exists():
                shutil.copytree(agent.folder, target, ignore=shutil.ignore_patterns("__pycache__"))
        marker.write_text("1\n", encoding="utf-8")
    migrate_legacy_testcases(root)
    return load_agents(root)
