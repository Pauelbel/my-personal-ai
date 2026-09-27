"""Markdown-хранилище сохраняет читаемую память и технические checkpoint-файлы."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from local_agent.memory.operations import AddOperation, DeleteOperation, MemoryPatch, UpdateOperation

DEFAULT_DOCUMENTS = {
    "profile.md": "Профиль",
    "preferences.md": "Предпочтения",
    "projects.md": "Проекты",
    "decisions.md": "Решения",
}
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*\.md$")
ENTRY = re.compile(r"^- <!-- memory:id=([a-f0-9]{32}) --> (.*)$")


class MemoryDocument(BaseModel):
    name: str
    title: str
    content: str


class SessionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_processed_message_id: str


class MemoryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    sessions: dict[str, SessionCheckpoint] = Field(default_factory=dict)


class MarkdownMemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / ".state.json"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for name, title in DEFAULT_DOCUMENTS.items():
            path = self.root / name
            if not path.exists():
                self._atomic_write(path, f"# {title}\n")

    def list_documents(self) -> list[MemoryDocument]:
        self.initialize()
        return [self.read(path.name) for path in sorted(self.root.glob("*.md"))]

    def read(self, name: str) -> MemoryDocument:
        path = self._path(name)
        if not path.exists():
            raise FileNotFoundError(name)
        content = path.read_text(encoding="utf-8")
        title = next(
            (line[2:].strip() for line in content.splitlines() if line.startswith("# ")),
            path.stem.replace("_", " ").replace("-", " ").title(),
        )
        return MemoryDocument(name=name, title=title, content=content)

    def write(self, name: str, content: str) -> MemoryDocument:
        path = self._path(name)
        self.initialize()
        self._atomic_write(path, content)
        return self.read(name)

    def context(self, max_chars: int) -> str:
        parts: list[str] = []
        remaining = max_chars
        documents = {document.name: document for document in self.list_documents()}
        for name in DEFAULT_DOCUMENTS:
            if remaining <= 0:
                break
            document = documents[name]
            chunk = f"## {document.title}\n{document.content.strip()}\n"
            parts.append(chunk[:remaining])
            remaining -= len(parts[-1])
        return "\n".join(parts).strip()

    def checkpoint(self, session_id: str) -> str | None:
        checkpoint = self._read_state().sessions.get(session_id)
        return checkpoint.last_processed_message_id if checkpoint else None

    def apply(
        self,
        patch: MemoryPatch,
        *,
        session_id: str,
        last_processed_message_id: str,
    ) -> int:
        self.initialize()
        documents = {document.name: document.content for document in self.list_documents()}
        changed: set[str] = set()

        for index, operation in enumerate(patch.operations):
            if operation.file not in documents:
                documents[operation.file] = f"# {operation.file.removesuffix('.md').replace('_', ' ').replace('-', ' ').title()}\n"
            original = documents[operation.file]
            if isinstance(operation, AddOperation):
                entry_id = self._new_entry_id(session_id, operation.source_message_ids, index)
                updated = self._add(original, operation.section, entry_id, operation.content)
            elif isinstance(operation, UpdateOperation):
                updated = self._update(original, operation.entry_id, operation.content)
            elif isinstance(operation, DeleteOperation):
                updated = self._delete(original, operation.entry_id)
            else:  # pragma: no cover - the discriminated model prevents this branch
                raise ValueError("Unsupported memory operation")
            documents[operation.file] = updated
            if updated != original:
                changed.add(operation.file)

        for name in sorted(changed):
            self._atomic_write(self._path(name), documents[name])

        state = self._read_state()
        state.sessions[session_id] = SessionCheckpoint(
            last_processed_message_id=last_processed_message_id
        )
        self._atomic_write(
            self.state_path,
            json.dumps(state.model_dump(), ensure_ascii=False, indent=2) + "\n",
        )
        return len(patch.operations)

    def _read_state(self) -> MemoryState:
        if not self.state_path.exists():
            return MemoryState()
        return MemoryState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def _path(self, name: str) -> Path:
        if not SAFE_NAME.fullmatch(name):
            raise ValueError("Invalid memory document name")
        return self.root / name

    @staticmethod
    def _new_entry_id(session_id: str, source_ids: list[str], index: int) -> str:
        from uuid import NAMESPACE_URL, uuid5

        return uuid5(NAMESPACE_URL, f"{session_id}:{','.join(source_ids)}:{index}").hex

    @staticmethod
    def _add(text: str, section: str, entry_id: str, content: str) -> str:
        marker = f"<!-- memory:id={entry_id} -->"
        if marker in text:
            return text
        line = f"- {marker} {content}"
        lines = text.rstrip().splitlines()
        heading = f"## {section}"
        try:
            heading_index = lines.index(heading)
        except ValueError:
            return "\n".join(lines + ["", heading, "", line]) + "\n"

        insert_at = len(lines)
        for index in range(heading_index + 1, len(lines)):
            if lines[index].startswith("## "):
                insert_at = index
                break
        while insert_at > heading_index + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, line)
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _update(text: str, entry_id: str, content: str) -> str:
        lines = text.splitlines()
        matches = [index for index, line in enumerate(lines) if (match := ENTRY.match(line)) and match.group(1) == entry_id]
        if len(matches) != 1:
            raise ValueError(f"Memory entry {entry_id} was not found exactly once")
        lines[matches[0]] = f"- <!-- memory:id={entry_id} --> {content}"
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _delete(text: str, entry_id: str) -> str:
        lines = text.splitlines()
        matches = [index for index, line in enumerate(lines) if (match := ENTRY.match(line)) and match.group(1) == entry_id]
        if len(matches) != 1:
            raise ValueError(f"Memory entry {entry_id} was not found exactly once")
        del lines[matches[0]]
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
