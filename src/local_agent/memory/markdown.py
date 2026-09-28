"""Markdown-хранилище сохраняет читаемую память и технические checkpoint-файлы."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
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
LEGACY_MARKER = re.compile(r"(?m)^- <!-- memory:id=[a-f0-9]{32} --> ")


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
        self.pending_path = root / ".pending.json"
        self.log_path = root / ".updates.jsonl"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover_pending()
        legacy = {}
        for path in self.root.glob("*.md"):
            original = path.read_text(encoding="utf-8")
            cleaned = LEGACY_MARKER.sub("- ", original)
            if cleaned != original:
                legacy[path.name] = cleaned
        if legacy:
            self._commit(legacy, {"kind": "remove_legacy_ids", "files": sorted(legacy)})
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
        self._path(name)
        self.initialize()
        self._commit(
            {name: content},
            {"kind": "manual_edit", "files": [name]},
        )
        return self.read(name)

    def context(self, max_chars: int | None) -> str:
        """Собирает все файлы памяти; обрезает только целыми строками, чтобы записи не рвались."""
        documents = {document.name: document for document in self.list_documents()}
        order = [name for name in DEFAULT_DOCUMENTS if name in documents]
        order += [name for name in documents if name not in DEFAULT_DOCUMENTS]
        lines: list[str] = []
        used = 0
        for name in order:
            document = documents[name]
            body = [
                line for line in document.content.strip().splitlines()
                if not line.startswith("# ")
            ]
            if not any(line.strip() for line in body):
                continue
            for line in [f"# {document.title}", *body, ""]:
                if max_chars is not None and used + len(line) + 1 > max_chars:
                    return "\n".join(lines).strip()
                lines.append(line)
                used += len(line) + 1
        return "\n".join(lines).strip()

    def checkpoint(self, session_id: str) -> str | None:
        self.initialize()
        checkpoint = self._read_state().sessions.get(session_id)
        return checkpoint.last_processed_message_id if checkpoint else None

    def forget(self, session_id: str) -> None:
        """Удаляет checkpoint удалённой сессии; сами записи памяти остаются."""
        self.initialize()
        state = self._read_state()
        if state.sessions.pop(session_id, None) is not None:
            self._write_state(state)

    def apply(
        self,
        patch: MemoryPatch,
        *,
        session_id: str,
        last_processed_message_id: str,
    ) -> int:
        self.initialize()
        documents = {document.name: document.content for document in self.list_documents()}
        original_documents = documents.copy()
        changed: set[str] = set()

        for operation in patch.operations:
            if operation.file not in documents:
                documents[operation.file] = f"# {operation.file.removesuffix('.md').replace('_', ' ').replace('-', ' ').title()}\n"
            original = documents[operation.file]
            if isinstance(operation, AddOperation):
                updated = self._add(original, operation.section, operation.content)
            elif isinstance(operation, UpdateOperation):
                updated = self._update(original, operation.old_content, operation.content)
            elif isinstance(operation, DeleteOperation):
                updated = self._delete(original, operation.old_content)
            else:  # pragma: no cover - the discriminated model prevents this branch
                raise ValueError("Неподдерживаемая операция памяти")
            documents[operation.file] = updated
            if updated != original:
                changed.add(operation.file)

        original_state = self.state_path.read_text(encoding="utf-8") if self.state_path.exists() else None
        state = self._read_state()
        state.sessions[session_id] = SessionCheckpoint(
            last_processed_message_id=last_processed_message_id
        )
        targets = {name: documents[name] for name in sorted(changed)}
        targets[self.state_path.name] = self._state_content(state)
        self._commit(
            targets,
            {
                "kind": "model_update",
                "session_id": session_id,
                "last_processed_message_id": last_processed_message_id,
                "files": sorted(changed),
                "operations": [operation.op for operation in patch.operations],
            },
            expected_before={
                **{name: original_documents.get(name) for name in changed},
                self.state_path.name: original_state,
            },
        )
        return len(patch.operations)

    def _write_state(self, state: MemoryState) -> None:
        self._atomic_write(self.state_path, self._state_content(state))

    @staticmethod
    def _state_content(state: MemoryState) -> str:
        return json.dumps(state.model_dump(), ensure_ascii=False, indent=2) + "\n"

    def _commit(
        self,
        targets: dict[str, str],
        event: dict[str, object],
        *,
        expected_before: dict[str, str | None] | None = None,
    ) -> None:
        """Фиксирует намерение до первой записи; восстановление повторяет только не изменённые вручную файлы."""
        if self.pending_path.exists():
            self._recover_pending()
        changes = {}
        for name, content in targets.items():
            path = self.root / name
            before = path.read_text(encoding="utf-8") if path.exists() else None
            if expected_before is not None and before != expected_before[name]:
                raise ValueError(f"Файл памяти {name} изменился во время обновления")
            changes[name] = {
                "before": before,
                "after": content,
            }
        record = {
            "id": uuid4().hex,
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "changes": changes,
        }
        self._atomic_write(
            self.pending_path,
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        )
        self._recover_pending()

    def _recover_pending(self) -> None:
        if not self.pending_path.exists():
            return
        record = json.loads(self.pending_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not isinstance(record.get("changes"), dict):
            raise ValueError("Повреждён pending-журнал памяти")
        changes = record["changes"]
        if not isinstance(record.get("id"), str) or not isinstance(record.get("event"), dict):
            raise ValueError("Повреждён pending-журнал памяти")
        for name, change in changes.items():
            if not isinstance(name, str) or (
                name != self.state_path.name and not SAFE_NAME.fullmatch(name)
            ):
                raise ValueError("Недопустимый путь в pending-журнале памяти")
            if not isinstance(change, dict) or not isinstance(change.get("after"), str) or (
                change.get("before") is not None and not isinstance(change["before"], str)
            ):
                raise ValueError("Повреждён pending-журнал памяти")
        # Сначала проверяем все файлы: при ручной правке после сбоя нельзя частично продолжить patch.
        for name, change in changes.items():
            path = self.root / name
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current not in (change["before"], change["after"]):
                raise ValueError(f"Файл памяти {name} изменён после сбоя; требуется ручная проверка")
        ordered = sorted(changes, key=lambda name: (name == self.state_path.name, name))
        for name in ordered:
            change = changes[name]
            path = self.root / name
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current not in (change["before"], change["after"]):
                raise ValueError(f"Файл памяти {name} изменён после сбоя; требуется ручная проверка")
            if current != change["after"]:
                self._atomic_write(path, change["after"])
        self._append_log(record)
        self.pending_path.unlink()

    def _append_log(self, record: dict[str, object]) -> None:
        previous = self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""
        if any(json.loads(line).get("id") == record["id"] for line in previous.splitlines()):
            return
        event = {key: record[key] for key in ("id", "timestamp", "event")}
        self._atomic_write(
            self.log_path,
            previous + json.dumps(event, ensure_ascii=False) + "\n",
        )

    def _read_state(self) -> MemoryState:
        if not self.state_path.exists():
            return MemoryState()
        return MemoryState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def _path(self, name: str) -> Path:
        if not SAFE_NAME.fullmatch(name):
            raise ValueError("Недопустимое имя файла памяти")
        return self.root / name

    @staticmethod
    def _add(text: str, section: str, content: str) -> str:
        line = f"- {content}"
        if line in text.splitlines():
            raise ValueError("Такая запись памяти уже есть в файле")
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
    def _update(text: str, old_content: str, content: str) -> str:
        lines = text.splitlines()
        matches = [index for index, line in enumerate(lines) if line == f"- {old_content}"]
        if len(matches) != 1:
            raise ValueError("Прежний текст записи памяти должен встречаться ровно один раз")
        lines[matches[0]] = f"- {content}"
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _delete(text: str, old_content: str) -> str:
        lines = text.splitlines()
        matches = [index for index, line in enumerate(lines) if line == f"- {old_content}"]
        if len(matches) != 1:
            raise ValueError("Прежний текст записи памяти должен встречаться ровно один раз")
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
