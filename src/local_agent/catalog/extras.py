"""Какие встроенные дополнения включены: список id в data/tools/extras.json.

Дополнение — инструмент в коде приложения, выключенный по умолчанию (например, tools/testgen). Какие они есть,
решает сборка (assembly.py); включают их кнопкой «включить» на вкладке «Готовые».
"""

import json
from pathlib import Path


class ExtrasStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def enabled(self) -> set[str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()

    def set(self, tool_id: str, enabled: bool) -> None:
        items = self.enabled()
        items.add(tool_id) if enabled else items.discard(tool_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(items), ensure_ascii=False) + "\n", encoding="utf-8")
