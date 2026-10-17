"""Скилл — папка data/skills/<id>/:

- SKILL.md — блок параметров name и description, затем инструкции: когда скилл нужен и как делать работу;
- steps.md — необязательно: та же работа по шагам (формат — steps/files.py), шаги выполняет движок;
- scripts/ — необязательно: скрипты, которые запускают шаги «скрипт: имя.py».

Готовые скиллы лежат рядом, в skills/library/<id>/: их включают кнопкой — папка копируется в data/skills.
Архив — data/skills/.deleted/<id>.<uuid>/, как у инструментов.
"""

import logging
import re
import shutil
from pathlib import Path
from uuid import uuid4

from local_agent.shared import frontmatter
from local_agent.skills.models import Skill
from local_agent.steps.files import parse_steps

SKILL_FILE = "SKILL.md"
STEPS_FILE = "steps.md"
SCRIPTS = "scripts"
SKILL_KEYS = {"name", "description"}
SKILL_ID = frontmatter.ITEM_ID
LIBRARY = Path(__file__).resolve().parent / "library"
ARCHIVE = ".deleted"
ARCHIVED = re.compile(r"(?P<id>[a-z0-9][a-z0-9_-]*)\.[0-9a-f]{32}")
# Файл в scripts/: имя без папок и без точки в начале.
SCRIPT_FILE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
MAX_FILE_BYTES = 512 * 1024

logger = logging.getLogger(__name__)


def load_skills(root: Path) -> list[Skill]:
    """Каждая папка с SKILL.md — скилл. Система стартует без скиллов: готовые включаются из skills/library."""
    root.mkdir(parents=True, exist_ok=True)
    return [read_skill(folder) for folder in skill_folders(root)]


def skill_folders(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")) if root.is_dir() else []


def read_skill(folder: Path, skill_id: str | None = None) -> Skill:
    skill_id = folder.name if skill_id is None else skill_id
    if not SKILL_ID.fullmatch(skill_id):
        raise ValueError(f"{folder}: имя папки скилла — строчные латинские буквы, цифры, - и _")
    path = folder / SKILL_FILE
    if not path.is_file():
        raise ValueError(f"{folder}: нет файла {SKILL_FILE}")
    header, instructions = frontmatter.split(path)
    fields = frontmatter.fields(path, header, SKILL_KEYS)
    if not fields.get("description"):
        raise ValueError(f"{path}: у скилла должно быть описание (description) — по нему модель его выбирает")
    steps_path = folder / STEPS_FILE
    steps = parse_steps(steps_path.read_text(encoding="utf-8"), steps_path) if steps_path.is_file() else None
    if not instructions.strip() and steps is None:
        raise ValueError(f"{path}: пустые инструкции скилла")
    return Skill(
        id=skill_id, name=fields.get("name") or skill_id, description=fields["description"],
        instructions=instructions.strip(), steps=steps, folder=folder,
    )


def skill_text(name: str, description: str, instructions: str) -> str:
    name, description = " ".join(name.split()), " ".join(description.split())
    return f"---\nname: {name}\ndescription: {description}\n---\n{instructions.strip()}\n"


def files_of(folder: Path) -> dict[str, str]:
    """Текстовые файлы скилла по порядку: SKILL.md, steps.md, затем скрипты — так их видят окно правки и агент."""
    found = {name: (folder / name).read_text(encoding="utf-8") for name in (SKILL_FILE, STEPS_FILE) if (folder / name).is_file()}
    scripts = folder / SCRIPTS
    for path in sorted(scripts.iterdir()) if scripts.is_dir() else []:
        if path.is_file() and SCRIPT_FILE.fullmatch(path.name) and path.stat().st_size <= MAX_FILE_BYTES:
            try:
                found[f"{SCRIPTS}/{path.name}"] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
    return found


def check_name(name: str) -> None:
    """Файл скилла: SKILL.md, steps.md или scripts/<имя> — других путей нет, за пределы папки не выйти."""
    if name in (SKILL_FILE, STEPS_FILE):
        return
    folder, _, file = name.partition("/")
    if folder != SCRIPTS or not SCRIPT_FILE.fullmatch(file):
        raise ValueError(f"Файл «{name}»: у скилла бывают SKILL.md, steps.md и scripts/<имя файла>")


def check_text(name: str, text: str) -> None:
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"{name}: файл больше {MAX_FILE_BYTES // 1024} КБ")
    if name.endswith(".py"):
        try:
            compile(text, name, "exec")
        except SyntaxError as exc:
            raise ValueError(f"{name}, строка {exc.lineno}: {exc.msg}") from None


def save_files(root: Path, skill_id: str, files: dict[str, str | None], *, create: bool = False) -> Skill:
    """Пишет файлы скилла (None — удалить файл). Правка сначала проверяется копией папки: сломанный скилл
    на диск не попадает, а ошибка называет настоящий файл, а не черновик."""
    if not SKILL_ID.fullmatch(skill_id):
        raise ValueError("id скилла — строчные латинские буквы, цифры, - и _")
    for name, text in files.items():
        check_name(name)
        if text is not None:
            check_text(name, text)
    if files.get(SKILL_FILE, "") is None:
        raise ValueError(f"{SKILL_FILE} удалить нельзя — без него нет скилла")
    target = root / skill_id
    if create and target.exists():
        raise FileExistsError(f"Скилл {skill_id} уже есть — выберите другое имя")
    if not create and not (target / SKILL_FILE).is_file():
        raise FileNotFoundError("Скилла нет")
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{skill_id}.{uuid4().hex}"
    try:
        if target.exists():
            shutil.copytree(target, staging, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            staging.mkdir()
        for name, text in files.items():
            path = staging / name
            if text is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        try:
            read_skill(staging, skill_id)
        except ValueError as exc:
            raise ValueError(str(exc).replace(str(staging), str(target))) from None
        if target.exists():
            old = root / f".{skill_id}.{uuid4().hex}.old"
            target.rename(old)
            staging.rename(target)
            shutil.rmtree(old)
        else:
            staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return read_skill(target)


def write_skill(
    root: Path, skill_id: str, name: str, description: str, instructions: str,
    steps: str | None = None, scripts: dict[str, str] | None = None,
) -> Skill:
    """Создаёт или обновляет скилл: SKILL.md пишется целиком, steps.md и скрипты — только переданные."""
    name, description = name.strip(), description.strip()
    if not name or not description:
        raise ValueError("Название и описание скилла не могут быть пустыми")
    if any("\n" in value or "\r" in value for value in (name, description)):
        raise ValueError("Название и описание скилла должны быть в одну строку")
    files: dict[str, str | None] = {SKILL_FILE: skill_text(name, description, instructions)}
    if steps is not None and steps.strip():
        files[STEPS_FILE] = steps.strip() + "\n"
    for file, code in (scripts or {}).items():
        files[f"{SCRIPTS}/{file}"] = code.rstrip() + "\n"
    return save_files(root, skill_id, files, create=not (root / skill_id / SKILL_FILE).is_file())


def archive_skill(root: Path, skill_id: str) -> None:
    """Убирает папку скилла в архив: вернуть можно оттуда же."""
    if not SKILL_ID.fullmatch(skill_id) or not (root / skill_id).is_dir():
        raise FileNotFoundError("Скилла нет")
    archive = root / ARCHIVE
    archive.mkdir(exist_ok=True)
    (root / skill_id).rename(archive / f"{skill_id}.{uuid4().hex}")


def archived_skills(root: Path) -> list[tuple[str, Skill]]:
    """(id записи архива, скилл); битые записи пропускаются."""
    archive = root / ARCHIVE
    items = []
    for path in sorted(archive.iterdir()) if archive.is_dir() else []:
        if path.is_dir() and (match := ARCHIVED.fullmatch(path.name)):
            try:
                items.append((path.name, read_skill(path, match["id"])))
            except ValueError:
                continue
    return items


def restore_skill(root: Path, archive_id: str) -> Skill:
    """Возвращает скилл из архива, не заменяя скилл с тем же id."""
    match = ARCHIVED.fullmatch(archive_id)
    source = root / ARCHIVE / archive_id
    if match is None or not source.is_dir():
        raise FileNotFoundError("Записи архива нет")
    target = root / match["id"]
    if target.exists():
        raise FileExistsError("Скилл с таким id уже есть")
    source.rename(target)
    return read_skill(target)


def library_skills() -> list[Skill]:
    """Готовые скиллы из skills/library: их можно включить."""
    return load_skills(LIBRARY) if LIBRARY.is_dir() else []
