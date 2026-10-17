"""Загрузчик читает агентов и навыки из Markdown-файлов: заголовок с параметрами и текст инструкций."""

import logging
import re
from collections.abc import Collection
from pathlib import Path
from uuid import uuid4

from local_agent.agent.models import Agent, Skill
from local_agent.tools.registry import normalize_tool_ids

AGENT_KEYS = {"name", "description", "provider", "model", "tools", "skills", "max_tool_rounds", "agents"}
SKILL_KEYS = {"name", "description"}
SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
AGENT_ID = SKILL_ID
MAX_TOOL_ROUNDS_LIMIT = 200
ARCHIVED_SKILL_ID = re.compile(r"(?P<id>[a-z0-9][a-z0-9_-]*)\.[0-9a-f]{32}\.md")

logger = logging.getLogger(__name__)


def load_agents(root: Path) -> list[Agent]:
    """Каждый `<id>.md` в папке — саб-агент. Основной агент встроен в приложение, поэтому папка может быть пустой."""
    root.mkdir(parents=True, exist_ok=True)
    return [_parse_agent(path) for path in sorted(root.glob("*.md"))]


def load_skills(root: Path) -> list[Skill]:
    """Каждый `<id>.md` в папке — навык. Система стартует без навыков: необязательный набор лежит в extras/skills."""
    root.mkdir(parents=True, exist_ok=True)
    return [_parse_skill(path) for path in sorted(root.glob("*.md"))]


def archive_skill(root: Path, skill_id: str) -> None:
    """Убирает навык из каталога, сохраняя его файл в архиве для восстановления."""
    if not SKILL_ID.fullmatch(skill_id):
        raise ValueError("Недопустимый ID навыка")
    _archive(root, skill_id)


def write_skill(root: Path, skill_id: str, name: str, description: str, instructions: str) -> Skill:
    """Создаёт или перезаписывает навык с заданным id: так агент оформляет утверждённое предложение."""
    if not SKILL_ID.fullmatch(skill_id):
        raise ValueError("ID навыка — строчные латинские буквы, цифры, - и _")
    name, description, instructions = name.strip(), description.strip(), instructions.strip()
    if not name or not description or not instructions:
        raise ValueError("Название, описание и инструкции навыка не могут быть пустыми")
    if any("\n" in value or "\r" in value for value in (name, description)):
        raise ValueError("Название и описание навыка должны быть в одну строку")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{skill_id}.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n", encoding="utf-8", newline="\n")
    return _parse_skill(path)


def archived_agents(root: Path) -> list[tuple[str, Agent]]:
    """Саб-агенты в архиве: (id записи архива, агент)."""
    archive = root / ".deleted"
    if not archive.exists():
        return []
    result = []
    for path in sorted(archive.iterdir()):
        if path.is_file() and (match := ARCHIVED_SKILL_ID.fullmatch(path.name)):
            try:
                agent = _parse_agent(path)
            except ValueError:
                continue
            result.append((path.name, Agent(**{**agent.__dict__, "id": match["id"]})))
    return result


def restore_agent(root: Path, archive_id: str) -> Agent:
    """Возвращает саб-агента из архива, не заменяя агента с тем же ID."""
    match = ARCHIVED_SKILL_ID.fullmatch(archive_id)
    if match is None:
        raise ValueError("Недопустимый ID записи архива")
    source = root / ".deleted" / archive_id
    if not source.is_file():
        raise FileNotFoundError("Записи архива нет")
    target = root / f"{match['id']}.md"
    if target.exists():
        raise FileExistsError("Агент с таким ID уже существует")
    source.rename(target)
    return _parse_agent(target)


def archive_agent(root: Path, agent_id: str) -> None:
    """Убирает агента, сохраняя его файл в `.deleted`: вернуть его можно, переложив файл обратно."""
    if not AGENT_ID.fullmatch(agent_id):
        raise ValueError("Недопустимый ID агента")
    _archive(root, agent_id)


def _archive(root: Path, item_id: str) -> None:
    path = root / f"{item_id}.md"
    archive = root / ".deleted"
    archive.mkdir(exist_ok=True)
    path.rename(archive / f"{item_id}.{uuid4().hex}.md")


def write_agent(
    root: Path,
    agent: Agent,
    *,
    known_tools: Collection[str],
    known_skills: Collection[str],
    create: bool,
) -> Agent:
    """Записывает файл агента целиком. Новый файл не перезаписывает существующий."""
    _check_agent(agent, known_tools, known_skills)
    path = root / f"{agent.id}.md"
    text = _agent_text(agent)
    if create:
        root.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as target:
            target.write(text)
    else:
        if not path.is_file():
            raise FileNotFoundError("Агент не найден")
        path.write_text(text, encoding="utf-8", newline="\n")
    return _parse_agent(path)


def _check_agent(agent: Agent, known_tools: Collection[str], known_skills: Collection[str]) -> None:
    if not AGENT_ID.fullmatch(agent.id):
        raise ValueError("ID агента — строчные латинские буквы, цифры, - и _")
    if not agent.name.strip():
        raise ValueError("Имя агента не может быть пустым")
    # Перевод строки в поле дописал бы в заголовок файла свою строку, например «tools: write_file».
    if any("\n" in value or "\r" in value for value in (agent.name, agent.description, agent.llm_provider, agent.model)):
        raise ValueError("Имя, описание, провайдер и модель должны быть в одну строку")
    if not agent.system_prompt.strip():
        raise ValueError("Системный промпт не может быть пустым")
    # Инструменты — граница прав агента: в файл попадают только те, что есть в реестре.
    if unknown := sorted(set(agent.tools) - set(known_tools)):
        raise ValueError(f"Неизвестные инструменты: {', '.join(unknown)}")
    if unknown := sorted(set(agent.skills or ()) - set(known_skills)):
        raise ValueError(f"Неизвестные навыки: {', '.join(unknown)}")
    if agent.id in agent.agents or not all(AGENT_ID.fullmatch(item) for item in agent.agents):
        raise ValueError("Связи агента — id других саб-агентов")
    if agent.max_tool_rounds is not None and not 1 <= agent.max_tool_rounds <= MAX_TOOL_ROUNDS_LIMIT:
        raise ValueError(f"Раундов инструментов — от 1 до {MAX_TOOL_ROUNDS_LIMIT}")


def _agent_text(agent: Agent) -> str:
    lines = [f"name: {agent.name}"]
    if agent.description:
        lines.append(f"description: {agent.description}")
    # Модель выбирают в чате; строки provider и model есть только у агента, которому её закрепили в файле вручную.
    if agent.model:
        lines += [f"provider: {agent.llm_provider}", f"model: {agent.model}"]
    lines.append(f"tools: {', '.join(agent.tools)}")
    # Без строки skills агенту доступны все навыки, пустая строка — ни одного.
    if agent.skills is not None:
        lines.append(f"skills: {', '.join(agent.skills)}")
    if agent.max_tool_rounds:
        lines.append(f"max_tool_rounds: {agent.max_tool_rounds}")
    if agent.agents:
        lines.append(f"agents: {', '.join(agent.agents)}")
    return "---\n" + "\n".join(lines) + "\n---\n" + agent.system_prompt.strip() + "\n"


def archived_skills(root: Path) -> list[tuple[str, Skill]]:
    """Перечисляет только файлы, созданные штатным архивированием навыков."""
    archive = root / ".deleted"
    if not archive.exists():
        return []
    return [
        (path.name, _parse_skill(path, match["id"]))
        for path in sorted(archive.iterdir())
        if path.is_file() and (match := ARCHIVED_SKILL_ID.fullmatch(path.name))
    ]


def restore_skill(root: Path, archive_id: str) -> Skill:
    """Возвращает навык из архива, не заменяя существующий навык с тем же ID."""
    match = ARCHIVED_SKILL_ID.fullmatch(archive_id)
    if match is None:
        raise ValueError("Недопустимый ID записи архива")
    source = root / ".deleted" / archive_id
    skill = _parse_skill(source, match["id"])
    target = root / f"{skill.id}.md"
    if target.exists():
        raise FileExistsError("Навык с таким ID уже существует")
    source.rename(target)
    return skill


def save_skill(root: Path, skill_id: str, instructions: str) -> Skill:
    """Меняет только инструкции навыка; название и описание правятся в самом файле."""
    path = root / f"{skill_id}.md"
    _replace_body(path, instructions, "Инструкции навыка не могут быть пустыми")
    return _parse_skill(path)


def _replace_body(path: Path, body: str, empty_error: str) -> None:
    if not body.strip():
        raise ValueError(empty_error)
    header, _ = _split(path)
    text = f"---\n{header}\n---\n{body.strip()}\n".replace("\r\n", "\n")
    path.write_text(text, encoding="utf-8", newline="\n")


def _split(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[3:]:
        raise ValueError(f"{path}: файл должен начинаться с блока --- параметры ---")
    header, body = text[4:].split("\n---\n", 1)
    return header, body


def _fields(path: Path, header: str, known: set[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or key not in known:
            raise ValueError(f"{path}: неизвестный параметр «{line.strip()}»")
        fields[key] = value.strip()
    return fields


def _parse_agent(path: Path) -> Agent:
    header, prompt = _split(path)
    fields = _fields(path, header, AGENT_KEYS)
    if not prompt.strip():
        raise ValueError(f"{path}: пустой системный промпт")
    rounds = fields.get("max_tool_rounds")
    if rounds and not (rounds.isdigit() and int(rounds) > 0):
        raise ValueError(f"{path}: max_tool_rounds — целое число больше нуля")
    return Agent(
        id=path.stem,
        name=fields.get("name") or path.stem,
        system_prompt=prompt.strip(),
        llm_provider=fields.get("provider") or "lm_studio",
        # Пусто — агент работает на модели, выбранной в чате.
        model=fields.get("model", ""),
        tools=tuple(normalize_tool_ids(_items(fields.get("tools", "")))),
        description=fields.get("description", ""),
        # Строка «skills:» без значений означает «без навыков», а отсутствие строки — «все навыки».
        skills=tuple(_items(fields["skills"])) if "skills" in fields else None,
        max_tool_rounds=int(rounds) if rounds else None,
        agents=tuple(_items(fields.get("agents", ""))),
    )


def warn_unknown_references(agents: list[Agent], tool_ids: set[str], skill_ids: set[str]) -> None:
    """Опечатка в файле агента не должна ронять приложение: неизвестные имена просто не сработают."""
    for agent in agents:
        for kind, names, known in (("инструмент", agent.tools, tool_ids), ("навык", agent.skills or (), skill_ids)):
            for name in names:
                if name not in known:
                    logger.warning("Агент %s ссылается на неизвестный %s: %s", agent.id, kind, name)


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_skill(path: Path, skill_id: str | None = None) -> Skill:
    skill_id = path.stem if skill_id is None else skill_id
    if not SKILL_ID.fullmatch(skill_id):
        raise ValueError(f"{path}: имя файла навыка — строчные латинские буквы, цифры, - и _")
    header, instructions = _split(path)
    fields = _fields(path, header, SKILL_KEYS)
    if not fields.get("description"):
        raise ValueError(f"{path}: у навыка должно быть описание (description) — по нему модель его выбирает")
    if not instructions.strip():
        raise ValueError(f"{path}: пустые инструкции навыка")
    return Skill(
        id=skill_id,
        name=fields.get("name") or path.stem,
        description=fields["description"],
        instructions=instructions.strip(),
    )
