"""Файлы агентов: data/agents/<id>.md — параметры (name, description, tools, skills, agents…), затем роль.

Основной агент встроен в приложение (agents/main.py); здесь — саб-агенты, которых можно править руками.
"""

import logging
from collections.abc import Collection, Iterable
from pathlib import Path

from local_agent.agents.models import Agent
from local_agent.shared import frontmatter

AGENT_KEYS = {"name", "description", "provider", "model", "tools", "skills", "max_tool_rounds", "agents"}
AGENT_ID = frontmatter.ITEM_ID
MAX_TOOL_ROUNDS_LIMIT = 200

logger = logging.getLogger(__name__)


def normalize_tool_ids(tool_ids: Iterable[str]) -> list[str]:
    """Старые разрешения чтения Git относятся к единому инструменту Git."""
    return list(dict.fromkeys("git" if item in {"git_log", "git_show"} else item for item in tool_ids))


def load_agents(root: Path) -> list[Agent]:
    """Каждый `<id>.md` в папке — саб-агент. Основной агент встроен в приложение, поэтому папка может быть пустой."""
    root.mkdir(parents=True, exist_ok=True)
    return [parse_agent(path) for path in sorted(root.glob("*.md"))]


def parse_agent(path: Path) -> Agent:
    header, prompt = frontmatter.split(path)
    fields = frontmatter.fields(path, header, AGENT_KEYS)
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
        tools=tuple(normalize_tool_ids(frontmatter.items(fields.get("tools", "")))),
        description=fields.get("description", ""),
        # Строка «skills:» без значений означает «без скиллов», а отсутствие строки — «все скиллы».
        skills=tuple(frontmatter.items(fields["skills"])) if "skills" in fields else None,
        max_tool_rounds=int(rounds) if rounds else None,
        agents=tuple(frontmatter.items(fields.get("agents", ""))),
    )


def write_agent(
    root: Path,
    agent: Agent,
    *,
    known_tools: Collection[str],
    known_skills: Collection[str],
    create: bool,
) -> Agent:
    """Записывает файл агента целиком. Новый файл не перезаписывает существующий."""
    check_agent(agent, known_tools, known_skills)
    path = root / f"{agent.id}.md"
    text = agent_text(agent)
    if create:
        root.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as target:
            target.write(text)
    else:
        if not path.is_file():
            raise FileNotFoundError("Агент не найден")
        path.write_text(text, encoding="utf-8", newline="\n")
    return parse_agent(path)


def check_agent(agent: Agent, known_tools: Collection[str], known_skills: Collection[str]) -> None:
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
        raise ValueError(f"Неизвестные скиллы: {', '.join(unknown)}")
    if agent.id in agent.agents or not all(AGENT_ID.fullmatch(item) for item in agent.agents):
        raise ValueError("Связи агента — id других саб-агентов")
    if agent.max_tool_rounds is not None and not 1 <= agent.max_tool_rounds <= MAX_TOOL_ROUNDS_LIMIT:
        raise ValueError(f"Раундов инструментов — от 1 до {MAX_TOOL_ROUNDS_LIMIT}")


def agent_text(agent: Agent) -> str:
    lines = [f"name: {agent.name}"]
    if agent.description:
        lines.append(f"description: {agent.description}")
    # Модель выбирают в чате; строки provider и model есть только у агента, которому её закрепили в файле вручную.
    if agent.model:
        lines += [f"provider: {agent.llm_provider}", f"model: {agent.model}"]
    lines.append(f"tools: {', '.join(agent.tools)}")
    # Без строки skills агенту доступны все скиллы, пустая строка — ни одного.
    if agent.skills is not None:
        lines.append(f"skills: {', '.join(agent.skills)}")
    if agent.max_tool_rounds:
        lines.append(f"max_tool_rounds: {agent.max_tool_rounds}")
    if agent.agents:
        lines.append(f"agents: {', '.join(agent.agents)}")
    return "---\n" + "\n".join(lines) + "\n---\n" + agent.system_prompt.strip() + "\n"


def archive_agent(root: Path, agent_id: str) -> None:
    """Убирает агента, сохраняя его файл в `.deleted`: вернуть его можно, переложив файл обратно."""
    if not AGENT_ID.fullmatch(agent_id):
        raise ValueError("Недопустимый ID агента")
    frontmatter.archive(root, agent_id)


def archived_agents(root: Path) -> list[tuple[str, Agent]]:
    """Саб-агенты в архиве: (id записи архива, агент)."""
    archive = root / ".deleted"
    if not archive.exists():
        return []
    result = []
    for path in sorted(archive.iterdir()):
        if path.is_file() and (match := frontmatter.ARCHIVED_ID.fullmatch(path.name)):
            try:
                agent = parse_agent(path)
            except ValueError:
                continue
            result.append((path.name, Agent(**{**agent.__dict__, "id": match["id"]})))
    return result


def restore_agent(root: Path, archive_id: str) -> Agent:
    """Возвращает саб-агента из архива, не заменяя агента с тем же ID."""
    match = frontmatter.ARCHIVED_ID.fullmatch(archive_id)
    if match is None:
        raise ValueError("Недопустимый ID записи архива")
    source = root / ".deleted" / archive_id
    if not source.is_file():
        raise FileNotFoundError("Записи архива нет")
    target = root / f"{match['id']}.md"
    if target.exists():
        raise FileExistsError("Агент с таким ID уже существует")
    source.rename(target)
    return parse_agent(target)


def warn_unknown_references(agents: list[Agent], tool_ids: set[str], skill_ids: set[str]) -> None:
    """Опечатка в файле агента не должна ронять приложение: неизвестные имена просто не сработают."""
    for agent in agents:
        for kind, names, known in (("инструмент", agent.tools, tool_ids), ("скилл", agent.skills or (), skill_ids)):
            for name in names:
                if name not in known:
                    logger.warning("Агент %s ссылается на неизвестный %s: %s", agent.id, kind, name)
