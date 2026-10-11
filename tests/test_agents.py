"""Саб-агенты: файл агента не даёт лишних прав, архив и восстановление, основной агент встроен."""

import pytest

from local_agent.agent.loader import archive_agent, archived_agents, load_agents, restore_agent, write_agent
from local_agent.agent.main_agent import MAIN_AGENT_ID, main_agent
from local_agent.agent.models import Agent

AGENT = Agent(
    id="reviewer", name="Ревьюер", system_prompt="Ты ревьюер кода.", llm_provider="lm_studio", model="",
    tools=("read_file", "git"), description="Смотрит изменения", skills=(),
)
KNOWN_TOOLS = {"read_file", "git", "write_file"}


def write(root, agent, create=True):
    return write_agent(root, agent, known_tools=KNOWN_TOOLS, known_skills={"notes"}, create=create)


def test_system_starts_only_with_built_in_main_agent(tmp_path):
    assert load_agents(tmp_path / "agents") == []
    assert main_agent().id == MAIN_AGENT_ID and "propose" in main_agent().system_prompt


def test_agent_is_written_and_read_back(tmp_path):
    root = tmp_path / "agents"
    write(root, AGENT)
    with pytest.raises(FileExistsError):
        write(root, AGENT)
    write(root, Agent(**{**AGENT.__dict__, "tools": ("read_file",), "max_tool_rounds": 3}), create=False)

    [agent] = load_agents(root)
    assert agent.tools == ("read_file",) and agent.max_tool_rounds == 3 and agent.skills == ()


@pytest.mark.parametrize(("fields", "message"), [
    ({"tools": ("read_file", "rm_rf")}, "Неизвестные инструменты: rm_rf"),
    ({"skills": ("ghost",)}, "Неизвестные навыки: ghost"),
    ({"name": "Имя\ntools: write_file"}, "в одну строку"),
    ({"description": "Описание\rtools: write_file, git"}, "в одну строку"),
    ({"id": "../main"}, "ID агента"),
    ({"id": "Reviewer"}, "ID агента"),
    ({"max_tool_rounds": 0}, "Раундов инструментов"),
])
def test_agent_file_cannot_gain_unexpected_rights(tmp_path, fields, message):
    """Инструменты — права агента: в файл попадают только известные, а перевод строки не дописывает заголовок."""
    with pytest.raises(ValueError, match=message):
        write(tmp_path / "agents", Agent(**{**AGENT.__dict__, **fields}))
    assert load_agents(tmp_path / "agents") == []


def test_archived_agent_is_listed_and_restored(tmp_path):
    root = tmp_path / "agents"
    write(root, AGENT)
    archive_agent(root, "reviewer")
    [(archive_id, archived)] = archived_agents(root)

    assert load_agents(root) == [] and archived.id == "reviewer"
    restored = restore_agent(root, archive_id)
    assert restored.id == "reviewer" and [agent.id for agent in load_agents(root)] == ["reviewer"]
    archive_agent(root, "reviewer")
    write(root, AGENT)
    with pytest.raises(FileExistsError):
        restore_agent(root, archived_agents(root)[0][0])
