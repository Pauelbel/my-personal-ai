"""JSONL-хранилище ведёт отдельный читаемый архив сообщений для каждой сессии."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from pathlib import Path
from threading import RLock

from local_agent.memory.models import Message

SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class JsonlConversationStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = RLock()
        self._end_offsets: dict[str, dict[str, int]] = {}

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, message: Message) -> Message:
        path = self._path(message.session_id)
        payload = self._serialize(message)
        with self._lock:
            self.initialize()
            self._ensure_index(message.session_id)
            if message.id in self._end_offsets[message.session_id]:
                raise ValueError("Сообщение уже существует")
            with path.open("ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                end_offset = stream.tell()
            self._end_offsets[message.session_id][message.id] = end_offset
        return message

    def list(self, session_id: str) -> list[Message]:
        path = self._path(session_id)
        with self._lock:
            if not path.exists():
                return []
            with path.open("rb") as stream:
                return [self._parse(line) for line in stream if line.strip()]

    def recent(self, session_id: str, limit: int) -> list[Message]:
        path = self._path(session_id)
        with self._lock:
            if not path.exists():
                return []
            with path.open("rb") as stream:
                lines = deque((line for line in stream if line.strip()), maxlen=limit)
            return [self._parse(line) for line in lines]

    def count(self, session_id: str) -> int:
        with self._lock:
            self._ensure_index(session_id)
            return len(self._end_offsets[session_id])

    def after(self, session_id: str, message_id: str | None) -> list[Message]:
        if message_id is None:
            return self.list(session_id)
        path = self._path(session_id)
        with self._lock:
            self._ensure_index(session_id)
            end_offset = self._end_offsets[session_id].get(message_id)
            if end_offset is None:
                raise ValueError("Сообщение из checkpoint памяти не найдено")
            with path.open("rb") as stream:
                stream.seek(end_offset)
                return [self._parse(line) for line in stream if line.strip()]

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        with self._lock:
            path.unlink(missing_ok=True)
            self._end_offsets.pop(session_id, None)

    def _ensure_index(self, session_id: str) -> None:
        if session_id in self._end_offsets:
            return
        path = self._path(session_id)
        offsets: dict[str, int] = {}
        if path.exists():
            with path.open("rb") as stream:
                while line := stream.readline():
                    if not line.strip():
                        continue
                    message = self._parse(line)
                    if message.id in offsets:
                        raise ValueError("В архиве диалога повторяется ID сообщения")
                    offsets[message.id] = stream.tell()
        self._end_offsets[session_id] = offsets

    def _path(self, session_id: str) -> Path:
        if not SAFE_SESSION_ID.fullmatch(session_id):
            raise ValueError("Недопустимый ID сессии")
        return self.root / f"{session_id}.jsonl"

    @staticmethod
    def _serialize(message: Message) -> bytes:
        data = {
            "id": message.id,
            "session_id": message.session_id,
            "role": message.role,
            "content": message.content,
            "timestamp": message.created_at.isoformat(),
        }
        if message.tool_calls:
            data["tool_calls"] = [call.model_dump() for call in message.tool_calls]
        if message.tool_call_id:
            data["tool_call_id"] = message.tool_call_id
            data["tool_name"] = message.tool_name
        if message.is_error:
            data["is_error"] = True
        return (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    @staticmethod
    def _parse(line: bytes) -> Message:
        data = json.loads(line.decode("utf-8"))
        data["created_at"] = data.pop("timestamp")
        return Message.model_validate(data)
