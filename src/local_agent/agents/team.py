"""Команда агентов: основной агент поручает задачи любому саб-агенту, саб-агенты обращаются друг к другу.

По умолчанию все саб-агенты могут обращаться ко всем. Строка agents в файле агента — ограничение:
тогда он обращается только к перечисленным. Каждый саб-агент в сессии проекта знает состав команды
и правила из своего промпта, а справочник «Команда» рисует те же связи.
"""

from local_agent.agents.main import MAIN_AGENT_DESCRIPTION, MAIN_AGENT_ID, MAIN_AGENT_NAME
from local_agent.agents.models import Agent
from local_agent.agents.registry import AgentRegistry


def reachable(agent: Agent, registry: AgentRegistry) -> tuple[str, ...]:
    """К кому саб-агент может обращаться: ко всем, кроме себя, или только к перечисленным в agents."""
    others = [item.id for item in registry.all() if item.id != agent.id]
    return tuple(item for item in agent.agents if item in others) if agent.agents else tuple(others)


def team_prompt(agent: Agent, registry: AgentRegistry) -> str:
    """Блок «ты в команде» для саб-агента в сессии проекта: кто руководит, к кому можно обратиться и как отвечать."""
    targets = [item for item in (registry.get(target) for target in reachable(agent, registry)) if item]
    lines = [
        "Ты работаешь в команде агентов.",
        f"- {MAIN_AGENT_NAME} агент — руководитель: говорит с пользователем и поручает задачи агентам.",
    ]
    if targets:
        lines.append("Агенты, к которым ты можешь обратиться через ask_agent (поручить часть задачи или спросить):")
        lines += [f"- {item.id} — {item.name}: {item.description or 'без описания'}" for item in targets]
        lines.append(
            "Свою задачу делай сам. К другому агенту обращайся только за частью, которая по его профилю, а не за всей "
            "задачей; передавай всё нужное целиком и жди ответа. Ответ коллеги используй в своей работе, а не пересылай как есть."
        )
    lines.append(
        "Тебе пишут основной агент и другие агенты: отвечай тому, кто обратился, по делу и с результатом."
    )
    lines.append(
        "Не хватает данных, доступа или решения и сам не выяснить — спроси пользователя через ask_user и дождись ответа. "
        "Нужен инструмент, которого у тебя нет, — попроси его через request_tools. Не получилось — так и напиши "
        "в итоге, с причиной; результат не выдумывай."
    )
    return "\n".join(lines)


def team_graph(registry: AgentRegistry) -> dict:
    """Справочник «Команда»: основной агент и саб-агенты, кто кому может поручать (направленные связи)."""
    agents = registry.all()
    nodes = [{"id": MAIN_AGENT_ID, "name": MAIN_AGENT_NAME, "description": MAIN_AGENT_DESCRIPTION, "builtin": True,
              "restricted": False, "tools": []}]
    nodes += [{"id": agent.id, "name": agent.name, "description": agent.description, "builtin": False,
               "restricted": bool(agent.agents), "tools": list(agent.tools)} for agent in agents]
    edges = [{"from": MAIN_AGENT_ID, "to": agent.id} for agent in agents]
    edges += [{"from": agent.id, "to": target} for agent in agents for target in reachable(agent, registry)]
    return {"nodes": nodes, "edges": edges}
