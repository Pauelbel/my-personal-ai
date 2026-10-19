"""Реестр скиллов: папки `data/skills/<name>/` с `SKILL.md` и `scripts/`.

Заголовок SKILL.md — блок между строками `---`: `name`, `description`, `entry`, `requires`, `secrets`, `timeout`.
Списки пишутся в квадратных скобках через запятую: `requires: [markitdown[pdf,docx]==0.1.2, httpx]`.
Битая папка не роняет приложение: в списке она видна с ошибкой, запустить её нельзя.
"""

import ast
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from meepo.skills.env import check_requirements
from meepo.skills.secrets import check_secret_names

logger = logging.getLogger(__name__)

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
KEYS = ("name", "description", "entry", "requires", "secrets", "timeout")
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 6 * 3600
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 20
# Файлы скилла: SKILL.md и всё внутри scripts/ (вложенные папки можно).
FILE_PATH = re.compile(r"^(SKILL\.md|scripts/[A-Za-z0-9_./-]+)$")


@dataclass
class Skill:
    name: str
    folder: Path
    description: str = ""
    entry: str = "scripts/main.py"
    requires: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    timeout: int = DEFAULT_TIMEOUT
    text: str = ""
    # Почему скилл не загрузился; пусто — скилл исправен.
    error: str = ""

    def files(self) -> list[str]:
        """Файлы скилла относительно его папки, кроме служебных (__pycache__)."""
        return sorted(path.relative_to(self.folder).as_posix() for path in self.folder.rglob("*")
                      if path.is_file() and "__pycache__" not in path.parts)


def split_list(value: str) -> list[str]:
    """[a, b[x,y]] → ["a", "b[x,y]"]: запятая внутри скобок пакета — часть пакета."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [item.strip().strip("'\"") for item in re.split(r",(?![^\[]*\])", value) if item.strip()]


def parse_skill_md(text: str) -> tuple[dict, str]:
    """Заголовок и тело SKILL.md. Ошибка формата — ValueError с понятным текстом."""
    text = text.replace("\r\n", "\n")
    end = text.find("\n---", 3)
    if not text.startswith("---\n") or end == -1:
        raise ValueError("SKILL.md должен начинаться с блока заголовка между строками ---")
    header, body = text[4:end], text[end + 4:].partition("\n")[2]
    fields: dict = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or key not in KEYS:
            raise ValueError(f"Неизвестная строка заголовка SKILL.md: «{line.strip()}». Допустимо: {', '.join(KEYS)}")
        fields[key] = value.strip()
    for key in ("requires", "secrets"):
        fields[key] = split_list(fields.get(key, ""))
    try:
        fields["timeout"] = int(fields.get("timeout") or DEFAULT_TIMEOUT)
    except ValueError:
        raise ValueError("timeout в SKILL.md — число секунд") from None
    return fields, body


def render_header(fields: dict) -> str:
    lines = [f"{key}: [{', '.join(fields[key])}]" if isinstance(fields.get(key), list) else f"{key}: {fields[key]}"
             for key in KEYS if key in fields]
    return "---\n" + "\n".join(lines) + "\n---\n"


def set_header(text: str, **values) -> str:
    """SKILL.md с заменёнными полями заголовка (requires и secrets задаёт инструмент, а не текст модели)."""
    fields, body = parse_skill_md(text)
    fields.update(values)
    return render_header(fields) + body


def check_files(name: str, files: dict[str, str]) -> str | None:
    """Проверка нового или изменённого скилла целиком: имя, SKILL.md, entry, синтаксис Python, пакеты, секреты."""
    if not NAME.fullmatch(name or ""):
        return "name — латиница в нижнем регистре, цифры и дефис, 2–41 символ, например testcase-generate"
    if "SKILL.md" not in files:
        return "Нужен файл SKILL.md"
    if len(files) > MAX_FILES:
        return f"Не больше {MAX_FILES} файлов в скилле"
    for path, content in files.items():
        if not FILE_PATH.fullmatch(path) or ".." in path.split("/"):
            return f"Недопустимый путь файла «{path}»: только SKILL.md и файлы в scripts/"
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return f"Файл {path} — текст не больше {MAX_FILE_BYTES // 1024} КБ"
    try:
        fields, _ = parse_skill_md(files["SKILL.md"])
    except ValueError as exc:
        return str(exc)
    if fields.get("name") != name:
        return f"В заголовке SKILL.md name должен быть {name}"
    if not fields.get("description"):
        return "В заголовке SKILL.md нет description — одна строка: что делает и когда применять"
    entry = fields.get("entry") or "scripts/main.py"
    if entry not in files:
        return f"Нет файла entry {entry}"
    for path, content in files.items():
        if path.endswith(".py"):
            try:
                ast.parse(content, filename=path)
            except SyntaxError as exc:
                return f"Синтаксическая ошибка в {path}, строка {exc.lineno}: {exc.msg}"
    if fields["requires"] and (problem := check_requirements(fields["requires"])):
        return f"requires: {problem}"
    if fields["secrets"] and (problem := check_secret_names(fields["secrets"])):
        return f"secrets: {problem}"
    if not 1 <= fields["timeout"] <= MAX_TIMEOUT:
        return f"timeout — от 1 до {MAX_TIMEOUT} секунд"
    return None


class SkillRegistry:
    def __init__(self, root: Path, builtin: Path | None = None) -> None:
        self.root = root
        self.builtin = builtin

    def install_builtin(self) -> None:
        """Стартовые скиллы копируются один раз: удалённый пользователем стартовый скилл не возвращается."""
        if self.builtin is None or not self.builtin.is_dir():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / ".builtin-installed"
        done = set(marker.read_text(encoding="utf-8").split()) if marker.is_file() else set()
        for folder in sorted(path for path in self.builtin.iterdir() if (path / "SKILL.md").is_file()):
            if folder.name not in done and not (self.root / folder.name).exists():
                shutil.copytree(folder, self.root / folder.name, ignore=shutil.ignore_patterns("__pycache__"))
            done.add(folder.name)
        marker.write_text("\n".join(sorted(done)) + "\n", encoding="utf-8")

    def list(self) -> list[Skill]:
        if not self.root.is_dir():
            return []
        return [self.load(folder) for folder in sorted(self.root.iterdir())
                if folder.is_dir() and not folder.name.startswith(".")]

    def get(self, name: str) -> Skill | None:
        if not NAME.fullmatch(name or ""):
            return None
        folder = self.root / name
        return self.load(folder) if folder.is_dir() else None

    def load(self, folder: Path) -> Skill:
        skill = Skill(folder.name, folder)
        try:
            skill.text = (folder / "SKILL.md").read_text(encoding="utf-8")
            fields, _ = parse_skill_md(skill.text)
            skill.description = fields.get("description", "")
            skill.entry = fields.get("entry") or skill.entry
            skill.requires, skill.secrets, skill.timeout = fields["requires"], fields["secrets"], fields["timeout"]
            if fields.get("name") != folder.name:
                skill.error = f"name в SKILL.md ({fields.get('name')}) не совпадает с именем папки"
            elif not (folder / skill.entry).is_file():
                skill.error = f"нет файла entry {skill.entry}"
        except (OSError, UnicodeError, ValueError) as exc:
            skill.error = str(exc)
        if skill.error:
            logger.warning("Скилл %s не загружен: %s", folder.name, skill.error)
        return skill

    def read_files(self, name: str) -> dict[str, str]:
        skill = self.get(name)
        if skill is None:
            raise ValueError(f"Скилла {name} нет")
        return {path: (skill.folder / path).read_text(encoding="utf-8", errors="replace") for path in skill.files()}

    def write(self, name: str, files: dict[str, str], *, replace: bool) -> Skill:
        """Новая версия папки: пишется во временную папку и подменяет старую разом. replace=False — только новый скилл."""
        if problem := check_files(name, files):
            raise ValueError(problem)
        target = self.root / name
        if target.exists() and not replace:
            raise ValueError(f"Скилл {name} уже есть: правка — через update_skill")
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / f".{name}.{uuid4().hex}"
        for path, content in files.items():
            file = staging / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding="utf-8", newline="\n")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        return self.load(target)

    def prompt_list(self) -> str:
        return "\n".join(f"- {skill.name} — {skill.description}" for skill in self.list() if not skill.error)
