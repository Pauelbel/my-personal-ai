"""tool.md инструмента-скрипта: параметры списком и подсказка модели — в одном файле с описанием."""

import json

import pytest

from connectors.scripts import read_script_tool
from core.tool import model_description
from core.toolbox import ActivateTool, Toolbox
from shared.params import parameter_schema, parse_items

TOOL = """---
name: Jira
description: Задачи Jira по JQL. Одна найденная — целиком
network: true
---
Коннектор к Jira только на чтение.

## Параметры
- jql — запрос JQL
- limit — сколько задач (число) (необязательно)

## Подсказка модели
- Тикет по ключу: key = QA-123
"""


def tool_folder(tmp_path, text=TOOL, parameters=None):
    folder = tmp_path / "jira"
    folder.mkdir(parents=True)
    (folder / "tool.md").write_text(text, encoding="utf-8")
    (folder / "script.py").write_text("print('ok')\n", encoding="utf-8")
    if parameters is not None:
        (folder / "parameters.json").write_text(json.dumps(parameters), encoding="utf-8")
    return folder


def test_parameters_and_hint_come_from_tool_md(tmp_path):
    tool = read_script_tool(tool_folder(tmp_path))
    assert tool.parameters == {
        "type": "object",
        "properties": {
            "jql": {"type": "string", "description": "запрос JQL"},
            "limit": {"type": "integer", "description": "сколько задач"},
        },
        "required": ["jql"],
        "additionalProperties": False,
    }
    assert tool.hint == "- Тикет по ключу: key = QA-123"
    assert tool.reason == "Коннектор к Jira только на чтение."


def test_hint_goes_to_schema_and_activation_but_not_to_registry_line(tmp_path):
    tool = read_script_tool(tool_folder(tmp_path))
    assert model_description(tool).endswith("key = QA-123")
    box = Toolbox(core=[], catalog=[tool], active=[])
    box.__post_init__()
    assert "key = QA-123" not in "\n".join(box.catalog_lines())
    box.activate(["jira"])
    assert "key = QA-123" in box.definitions()[0]["function"]["description"]

    import asyncio
    result = asyncio.run(ActivateTool(Toolbox(core=[], catalog=[tool], active=[])).execute({"names": ["jira"]}, None))
    assert "key = QA-123" in result.content


def test_tool_without_parameters_section_is_called_without_arguments(tmp_path):
    tool = read_script_tool(tool_folder(tmp_path, TOOL.split("## Параметры")[0]))
    assert tool.parameters == {"type": "object", "properties": {}, "additionalProperties": False}
    assert tool.hint == ""


def test_leftover_parameters_json_is_named(tmp_path):
    with pytest.raises(ValueError, match="перенесите параметры в tool.md"):
        read_script_tool(tool_folder(tmp_path, TOOL, {"type": "object"}))


@pytest.mark.parametrize(("text", "error"), [
    (TOOL.replace("- jql — запрос JQL", "jql запрос"), "пишите «- имя — что это»"),
    (TOOL.replace("- jql —", "- запрос —"), "латиницей"),
    (TOOL.replace("- Тикет по ключу: key = QA-123", "x" * 1600), "длиннее"),
    (TOOL + "\n## Параметры\n- other — x\n", "дважды"),
])
def test_mistakes_are_named(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        read_script_tool(tool_folder(tmp_path, text))


def test_same_list_format_as_steps_inputs():
    items = parse_items(["- папка — где документация (необязательно)", "", "* `jql`: запрос"], "steps.md", "Нужно от вас")
    assert [(item.name, item.hint, item.optional) for item in items] == [
        ("папка", "где документация", True), ("jql", "запрос", False),
    ]
    assert parameter_schema(items[1:], "tool.md")["required"] == ["jql"]


def test_marks_without_description_and_round_trip_from_old_schema():
    from shared.params import parameter_lines
    items = parse_items(["- limit (число) (необязательно)"], "tool.md", "Параметры")
    assert items == [type(items[0])(name="limit", hint="", optional=True, number=True)]
    schema = {"type": "object", "properties": {"path": {"type": "string", "description": "Путь"},
                                               "limit": {"type": "integer"}}, "required": ["path"]}
    lines = parameter_lines(schema)
    assert lines == "- path — Путь\n- limit (число) (необязательно)"
    assert parameter_schema(parse_items(lines.splitlines(), "tool.md", "Параметры"), "tool.md") == {
        **schema, "properties": {"path": {"type": "string", "description": "Путь"}, "limit": {"type": "integer"}},
        "additionalProperties": False,
    }
