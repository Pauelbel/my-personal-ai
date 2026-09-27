"""JSON-хранилище сохраняет глобальный список включённых инструментов."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4


class JsonToolSettings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def enabled_ids(self) -> set[str]:
        with self._lock:
            if not self.path.exists():
                return set()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("enabled_tools"), list):
                raise ValueError("Invalid tool settings file")
            if any(not isinstance(item, str) for item in data["enabled_tools"]):
                raise ValueError("Invalid tool id in settings file")
            return set(data["enabled_tools"])

    def set_enabled(self, tool_id: str, enabled: bool) -> None:
        with self._lock:
            current = self.enabled_ids()
            if enabled:
                current.add(tool_id)
            else:
                current.discard(tool_id)
            self.replace(current)

    def replace(self, enabled_ids: set[str]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"version": 1, "enabled_tools": sorted(enabled_ids)},
                ensure_ascii=False,
                indent=2,
            ) + "\n"
            temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
