"""Контракт отделяет настройки доступа к инструментам от места их хранения."""

from typing import Protocol


class ToolSettings(Protocol):
    def enabled_ids(self) -> set[str]: ...

    def set_enabled(self, tool_id: str, enabled: bool) -> None: ...
