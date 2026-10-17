"""Файлы скиллов: data/skills/<id>.md — параметры name и description, затем инструкции по шагам.

Готовые скиллы лежат рядом, в skills/library: их включают кнопкой «включить» — файл копируется в data/skills.
"""

from pathlib import Path

from local_agent.shared import frontmatter
from local_agent.skills.models import Skill

SKILL_KEYS = {"name", "description"}
SKILL_ID = frontmatter.ITEM_ID
LIBRARY = Path(__file__).resolve().parent / "library"


def load_skills(root: Path) -> list[Skill]:
    """Каждый `<id>.md` в папке — скилл. Система стартует без скиллов: готовые включаются из skills/library."""
    root.mkdir(parents=True, exist_ok=True)
    return [parse_skill(path) for path in sorted(root.glob("*.md"))]


def parse_skill(path: Path, skill_id: str | None = None) -> Skill:
    skill_id = path.stem if skill_id is None else skill_id
    if not SKILL_ID.fullmatch(skill_id):
        raise ValueError(f"{path}: имя файла скилла — строчные латинские буквы, цифры, - и _")
    header, instructions = frontmatter.split(path)
    fields = frontmatter.fields(path, header, SKILL_KEYS)
    if not fields.get("description"):
        raise ValueError(f"{path}: у скилла должно быть описание (description) — по нему модель его выбирает")
    if not instructions.strip():
        raise ValueError(f"{path}: пустые инструкции скилла")
    return Skill(
        id=skill_id,
        name=fields.get("name") or path.stem,
        description=fields["description"],
        instructions=instructions.strip(),
    )


def write_skill(root: Path, skill_id: str, name: str, description: str, instructions: str) -> Skill:
    """Создаёт или перезаписывает скилл с заданным id: так агент оформляет утверждённое предложение."""
    if not SKILL_ID.fullmatch(skill_id):
        raise ValueError("ID скилла — строчные латинские буквы, цифры, - и _")
    name, description, instructions = name.strip(), description.strip(), instructions.strip()
    if not name or not description or not instructions:
        raise ValueError("Название, описание и инструкции скилла не могут быть пустыми")
    if any("\n" in value or "\r" in value for value in (name, description)):
        raise ValueError("Название и описание скилла должны быть в одну строку")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{skill_id}.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n", encoding="utf-8", newline="\n")
    return parse_skill(path)


def archive_skill(root: Path, skill_id: str) -> None:
    """Убирает скилл из каталога, сохраняя его файл в архиве для восстановления."""
    if not SKILL_ID.fullmatch(skill_id):
        raise ValueError("Недопустимый ID скилла")
    frontmatter.archive(root, skill_id)


def archived_skills(root: Path) -> list[tuple[str, Skill]]:
    """Перечисляет только файлы, созданные штатным архивированием скиллов."""
    archive = root / ".deleted"
    if not archive.exists():
        return []
    return [
        (path.name, parse_skill(path, match["id"]))
        for path in sorted(archive.iterdir())
        if path.is_file() and (match := frontmatter.ARCHIVED_ID.fullmatch(path.name))
    ]


def restore_skill(root: Path, archive_id: str) -> Skill:
    """Возвращает скилл из архива, не заменяя существующий скилл с тем же ID."""
    match = frontmatter.ARCHIVED_ID.fullmatch(archive_id)
    if match is None:
        raise ValueError("Недопустимый ID записи архива")
    source = root / ".deleted" / archive_id
    skill = parse_skill(source, match["id"])
    target = root / f"{skill.id}.md"
    if target.exists():
        raise FileExistsError("Скилл с таким ID уже существует")
    source.rename(target)
    return skill


def library_skills() -> list[Skill]:
    """Готовые скиллы из skills/library: их можно включить."""
    return [parse_skill(path) for path in sorted(LIBRARY.glob("*.md"))] if LIBRARY.is_dir() else []
