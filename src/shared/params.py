"""Список «- имя — что это»: так пишутся «## Нужно от вас» в steps.md и «## Параметры» в tool.md.

Пометки в скобках после описания: «(необязательно)» — значение можно не передавать; «(число)» — целое число
(только у параметров инструмента). Из параметров инструмента строится JSON Schema его вызова.
"""

import re
from dataclasses import dataclass

# Описание — после тире или двоеточия; без описания после имени могут стоять только пометки в скобках.
ITEM_LINE = re.compile(r"^[-*]\s+`?([^\W\d][\w-]*)`?\s*(?:(?:—|–|-|:)\s*(.*)|(\(.*\)))?$")
OPTIONAL = re.compile(r"\s*\((?:необязательно|optional)\)\s*", re.IGNORECASE)
NUMBER = re.compile(r"\s*\((?:число|integer)\)\s*", re.IGNORECASE)
# Имя параметра инструмента — латиницей: маленькая модель переводит русские имена («папка» → «folder»).
PARAMETER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


@dataclass(frozen=True)
class Item:
    name: str
    hint: str = ""
    optional: bool = False
    number: bool = False


def parse_items(lines: list[str], where: str, section: str) -> list[Item]:
    """Строки списка по порядку; пустые пропускаются, всё остальное — ошибка с понятной причиной."""
    items: list[Item] = []
    for line in lines:
        if not line.strip():
            continue
        match = ITEM_LINE.match(line.strip())
        if match is None:
            raise ValueError(f"{where}: в «{section}» строка «{line.strip()}» — пишите «- имя — что это»")
        name, hint = match[1], (match[2] or match[3] or "").strip()
        if any(item.name == name for item in items):
            raise ValueError(f"{where}: «{name}» в «{section}» указано дважды")
        optional = "необязательно" in hint.casefold() or "optional" in hint.casefold()
        number = NUMBER.search(hint) is not None
        hint = NUMBER.sub(" ", OPTIONAL.sub(" ", hint)).strip()
        items.append(Item(name=name, hint=hint, optional=optional, number=number))
    return items


def parameter_schema(items: list[Item], where: str) -> dict:
    """JSON Schema вызова инструмента: строки, «(число)» — integer; лишние параметры запрещены."""
    properties: dict[str, dict] = {}
    for item in items:
        if not PARAMETER_NAME.fullmatch(item.name):
            raise ValueError(
                f"{where}: параметр «{item.name}» — имя латиницей в нижнем регистре, например jql или limit"
            )
        properties[item.name] = {"type": "integer" if item.number else "string"}
        if item.hint:
            properties[item.name]["description"] = item.hint
    schema: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required := [item.name for item in items if not item.optional]:
        schema["required"] = required
    return schema


def parameter_lines(schema: object) -> str:
    """Обратно: JSON Schema → список «- имя — что это». Так схема, которую модель по привычке
    прислала объектом, становится текстом для tool.md."""
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return ""
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    lines = []
    for name, spec in schema["properties"].items():
        spec = spec if isinstance(spec, dict) else {}
        hint = " ".join(str(spec.get("description") or "").split())
        marks = (" (число)" if spec.get("type") == "integer" else "") + ("" if name in required else " (необязательно)")
        lines.append(f"- {name}" + (f" — {hint}" if hint else "") + marks)
    return "\n".join(lines)
