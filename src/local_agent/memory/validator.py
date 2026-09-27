"""Валидатор проверяет ответ модели по контракту памяти до любых изменений (docs/memory-contract.md)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from local_agent.memory.markdown import ENTRY
from local_agent.memory.models import Message
from local_agent.memory.operations import (
    MAX_OPERATIONS,
    PATCH_VERSION,
    AddOperation,
    DeleteOperation,
    MemoryOperation,
    UpdateOperation,
)

CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
LIST_ITEM = re.compile(r"^[-*+]\s+(.*)$")
OPERATION_MODELS = {"add": AddOperation, "update": UpdateOperation, "delete": DeleteOperation}
PATCH_FIELDS = {"version", "operations"}
ERROR_TEXTS = {
    "extra_forbidden": "лишнее поле",
    "missing": "обязательное поле отсутствует",
}


class MemoryPatchRejected(Exception):
    """Patch нарушает контракт: не применяется целиком, checkpoint остаётся на месте."""

    def __init__(self, reason: str, index: int | None = None) -> None:
        self.reason = reason
        # Номер операции с нуля; None — ошибка всего patch (JSON, версия, поля верхнего уровня).
        self.index = index
        super().__init__(reason if index is None else f"операция {index}: {reason}")


def validate_patch(
    raw: str,
    *,
    session_id: str,
    new_messages: Sequence[Message],
    documents: Mapping[str, str],
) -> list[MemoryOperation]:
    """Возвращает операции, только если весь patch соблюдает контракт; иначе MemoryPatchRejected.

    new_messages — сообщения текущей сессии после checkpoint, documents — имя файла памяти → его текст.
    """
    items = _parse(raw)
    operations = [_operation(index, item) for index, item in enumerate(items)]

    sources = {message.id: message for message in new_messages}
    entries = _managed_entries(documents)
    texts = _existing_texts(documents)
    touched: set[str] = set()
    for index, operation in enumerate(operations):
        if operation.file not in documents:
            raise MemoryPatchRejected(f"файл {operation.file} не входит в память", index)
        for source_id in operation.source_message_ids:
            _check_source(index, sources.get(source_id), source_id, session_id)
        if isinstance(operation, AddOperation):
            key = _normalize(operation.content)
            if key in texts:
                raise MemoryPatchRejected(f"запись уже есть в памяти: «{operation.content}»", index)
            texts.add(key)
        else:
            _check_entry(index, operation, entries.get(operation.entry_id, []))
            if operation.entry_id in touched:
                raise MemoryPatchRejected(f"запись {operation.entry_id} уже затронута в этом patch", index)
            touched.add(operation.entry_id)
    return operations


def _parse(raw: str) -> list:
    text = raw.strip()
    if fenced := CODE_FENCE.match(text):
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise MemoryPatchRejected("ответ модели не является корректным JSON") from exc
    if not isinstance(data, dict):
        raise MemoryPatchRejected("patch должен быть JSON-объектом")
    if extra := sorted(set(data) - PATCH_FIELDS):
        raise MemoryPatchRejected(f"лишние поля patch: {', '.join(extra)}")
    version = data.get("version")
    # bool в Python — подкласс int: true не должен сойти за версию 1.
    if type(version) is not int or version != PATCH_VERSION:
        raise MemoryPatchRejected(f"неподдерживаемая версия контракта: {version!r}")
    items = data.get("operations")
    if not isinstance(items, list):
        raise MemoryPatchRejected("operations должен быть списком")
    if len(items) > MAX_OPERATIONS:
        raise MemoryPatchRejected(f"больше {MAX_OPERATIONS} операций в одном patch")
    return items


def _operation(index: int, item: object) -> MemoryOperation:
    if not isinstance(item, dict):
        raise MemoryPatchRejected("операция должна быть JSON-объектом", index)
    model = OPERATION_MODELS.get(item.get("op"))
    if model is None:
        raise MemoryPatchRejected(f"неизвестная операция {item.get('op')!r}", index)
    # null в чужом поле модели пишут вместо «поля нет» (entry_id: null у add); любое другое значение — лишнее поле.
    cleaned = {key: value for key, value in item.items() if key in model.model_fields or value is not None}
    try:
        return model.model_validate(cleaned)
    except ValidationError as exc:
        error = exc.errors()[0]
        field = ".".join(str(part) for part in error["loc"])
        raise MemoryPatchRejected(f"{field}: {ERROR_TEXTS.get(error['type'], error['msg'])}", index) from exc


def _check_source(index: int, message: Message | None, source_id: str, session_id: str) -> None:
    if message is None:
        raise MemoryPatchRejected(
            f"сообщение {source_id} не входит в новые сообщения сессии после checkpoint", index
        )
    if message.session_id != session_id:
        raise MemoryPatchRejected(f"сообщение {source_id} из другой сессии", index)
    if message.role != "user":
        raise MemoryPatchRejected(f"сообщение {source_id} написал не пользователь (role={message.role})", index)


def _check_entry(index: int, operation: UpdateOperation | DeleteOperation, files: list[str]) -> None:
    entry_id = operation.entry_id
    if not files:
        raise MemoryPatchRejected(
            f"управляемой записи {entry_id} нет; ручные записи без memory:id модели недоступны", index
        )
    if len(files) > 1:
        raise MemoryPatchRejected(f"запись {entry_id} встречается в памяти несколько раз ({len(files)})", index)
    if files[0] != operation.file:
        raise MemoryPatchRejected(f"запись {entry_id} находится в {files[0]}, а не в {operation.file}", index)


def _managed_entries(documents: Mapping[str, str]) -> dict[str, list[str]]:
    """entry_id → файлы, где он встречается; повтор означает испорченную память."""
    entries: dict[str, list[str]] = {}
    for name, content in documents.items():
        for line in content.splitlines():
            if match := ENTRY.match(line):
                entries.setdefault(match.group(1), []).append(name)
    return entries


def _existing_texts(documents: Mapping[str, str]) -> set[str]:
    """Тексты всех строк памяти без маркеров: и управляемых, и ручных."""
    texts: set[str] = set()
    for content in documents.values():
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if match := ENTRY.match(line):
                line = match.group(2)
            elif match := LIST_ITEM.match(line):
                line = match.group(1)
            texts.add(_normalize(line))
    return texts


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()
