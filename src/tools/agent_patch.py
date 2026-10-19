"""Проверяемая доработка выбранного агента с подтверждением и резервной копией."""

import difflib
import json
import logging
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agents.files import AGENT_FILE, MAX_FILE_BYTES, SCRIPT_FILE, STEPS_FILE, save_files
from agents.models import Agent
from agents.revisions import agent_revision, source_revision
from catalog.service import CapabilityService
from core.tool import ToolResult

MAX_PATCH_FILES = 12
MAX_DIFF_CHARS = 24_000
MAX_DIFF_LINES = 600
MAX_REASON_CHARS = 2_000
REVISION = re.compile(r"[0-9a-f]{64}")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedPatch:
    agent: Agent
    before: dict[str, str]
    files: dict[str, str | None]
    revision: str
    diffs: list[dict[str, str]]


class ProposeAgentPatchTool:
    id = "propose_agent_patch"
    name = "Предложить исправление агента"
    requires_approval = True
    description = (
        "Исправить файлы существующего выбранного агента после диагностики его сбоя, сохраняя его id. "
        "Сначала прочитай агента через read_capability(kind=agent): оттуда возьми expected_revision. "
        "Передай причину и полное новое содержимое только изменяемых файлов в files: AGENT.md, steps.md, "
        "scripts/<имя>; null удаляет файл. Предложение проверяется и показывается как разница до подтверждения. "
        "После применения повторяется работа того же агента; эта правка сама его не запускает."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "description": "id существующего выбранного агента"},
            "expected_revision": {"type": "string", "description": "revision из read_capability", "pattern": "^[0-9a-f]{64}$"},
            "reason": {"type": "string", "description": "Причина сбоя и как предлагаемая правка её устраняет"},
            "files": {
                "type": "object", "minProperties": 1, "maxProperties": MAX_PATCH_FILES,
                "description": "Изменяемые AGENT.md, steps.md, scripts/<имя>: полный текст или null для удаления",
                "additionalProperties": {"type": ["string", "null"]},
            },
        },
        "required": ["agent", "expected_revision", "reason", "files"],
        "additionalProperties": False,
    }

    def __init__(self, capabilities: CapabilityService) -> None:
        self._capabilities = capabilities

    def _prepare(self, arguments: dict[str, object]) -> PreparedPatch:
        item_id = arguments.get("agent")
        current = self._capabilities.agents.get(item_id)
        if current is None or current.folder is None or self._capabilities.paths.agents is None:
            raise ValueError("Нужен id существующего агента с файлами")
        expected = arguments.get("expected_revision")
        if not isinstance(expected, str) or not REVISION.fullmatch(expected):
            raise ValueError("expected_revision — revision из read_capability (64 шестнадцатеричных символа)")
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_CHARS:
            raise ValueError(f"reason — причина сбоя и исправления, от 1 до {MAX_REASON_CHARS} символов")
        files = arguments.get("files")
        if not isinstance(files, dict) or not 1 <= len(files) <= MAX_PATCH_FILES:
            raise ValueError(f"files — от 1 до {MAX_PATCH_FILES} изменяемых файлов")
        normalized: dict[str, str | None] = {}
        for name, content in files.items():
            if not isinstance(name, str) or not (
                name in (AGENT_FILE, STEPS_FILE)
                or name.startswith("scripts/") and SCRIPT_FILE.fullmatch(name.removeprefix("scripts/"))
            ):
                raise ValueError("Допустимы только AGENT.md, steps.md и scripts/<имя>")
            if content is not None and not isinstance(content, str):
                raise ValueError(f"{name}: нужен текст или null для удаления")
            if isinstance(content, str) and len(content.encode("utf-8")) > MAX_FILE_BYTES:
                raise ValueError(f"{name}: файл слишком большой")
            normalized[name] = content.replace("\r\n", "\n") if isinstance(content, str) else None
        if normalized.get(AGENT_FILE, "") is None:
            raise ValueError("AGENT.md удалить нельзя")
        folder, before = self._capabilities.source("agent", current.id)
        if source_revision(before) != expected:
            raise ValueError("Агент изменился после чтения. Снова вызови read_capability и подготовь правку новой версии")
        if not any(before.get(name) != content for name, content in normalized.items()):
            raise ValueError("Правка не меняет файлы агента")
        diffs: list[dict[str, str]] = []
        for name, content in normalized.items():
            if before.get(name) == content:
                continue
            lines = list(difflib.unified_diff(
                before.get(name, "").splitlines(), (content or "").splitlines(),
                fromfile=f"a/{name}" if name in before else "/dev/null",
                tofile=f"b/{name}" if content is not None else "/dev/null", lineterm="",
            ))
            # Даже правка завершающего перевода строки должна иметь видимое объяснение в карточке.
            diff = "\n".join(lines) or (
                "Удалён пустой файл" if content is None else "Создан пустой файл" if name not in before
                else "Изменён завершающий перевод строки"
            )
            diffs.append({"path": name, "diff": diff, "action": "delete" if content is None else "update" if name in before else "create"})
        if sum(len(item["diff"]) for item in diffs) > MAX_DIFF_CHARS or sum(
            len(item["diff"].splitlines()) for item in diffs
        ) > MAX_DIFF_LINES:
            raise ValueError("Правка слишком большая для проверки: раздели её на небольшие изменения")
        with tempfile.TemporaryDirectory(prefix="meepo-agent-check-") as temporary:
            root = Path(temporary)
            target = root / current.id
            target.mkdir()
            for name, text in before.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
            checked = save_files(root, current.id, normalized)
            if checked.steps is None:
                raise ValueError("У исправленного агента должны остаться исполняемые шаги в steps.md")
            for step in checked.steps.steps:
                if step.kind == "script" and not (checked.folder / "scripts" / step.text).is_file():
                    raise ValueError(f"В исправленном агенте нет scripts/{step.text}")
            # Возвращаем пути реального агента, а не удаляемой временной папки.
            checked = replace(checked, folder=folder)
        return PreparedPatch(checked, before, normalized, expected, diffs)

    def validate(self, arguments: dict[str, object]) -> str | None:
        try:
            self._prepare(arguments)
        except (OSError, ValueError, SyntaxError) as exc:
            return f"Исправление агента некорректно: {exc}"
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        prepared = self._prepare(arguments)
        return {
            "kind": "agent_patch", "title": f"Исправить агента «{prepared.agent.name}» ({prepared.agent.id})",
            "agent": prepared.agent.id, "expected_revision": prepared.revision,
            "reason": arguments["reason"], "diffs": prepared.diffs,
            "tools": list(prepared.agent.tools), "effects": prepared.agent.steps.effects(),
        }

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        try:
            prepared = self._prepare(arguments)
            backup = self._backup(prepared, str(arguments["reason"]))
            # Проверяем ещё раз после записи резервной копии: пока пользователь смотрел карточку,
            # файлы могли поменяться через редактор возможностей.
            if agent_revision(prepared.agent) != prepared.revision:
                raise ValueError("Агент изменился. Прочитай текущую версию и предложи исправление заново")
            after = dict(prepared.before)
            for name, text in prepared.files.items():
                if text is None:
                    after.pop(name, None)
                else:
                    after[name] = text
            revision = source_revision(after)
            self._capabilities.save_source(
                "agent", prepared.agent.id, prepared.files, expected_revision=prepared.revision,
            )
        except (OSError, ValueError, SyntaxError) as exc:
            return ToolResult(f"Исправление агента не применено: {exc}", is_error=True)
        try:
            manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
            manifest.update(status="applied", new_revision=revision)
            (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            # Файлы и реестр уже обновлены; сбой пометки копии не должен выдавать применённую правку за отказ.
            logger.exception("Не удалось пометить резервную копию агента %s", prepared.agent.id)
        return ToolResult(
            f"Агент {prepared.agent.id} исправлен. Резервная копия прежних файлов: {backup}. "
            "Повторите задачу тем же агентом после утверждения его шагов.",
            meta={"agent_patch": {"agent": prepared.agent.id, "revision": revision}},
        )

    def _backup(self, prepared: PreparedPatch, reason: str) -> Path:
        root = self._capabilities.paths.agents / ".history" / prepared.agent.id
        root.mkdir(parents=True, exist_ok=True)
        backup = root / uuid4().hex
        backup.mkdir()
        for name, text in prepared.before.items():
            path = backup / "files" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        (backup / "manifest.json").write_text(json.dumps({
            "agent": prepared.agent.id, "revision": prepared.revision, "reason": reason,
            "created_at": datetime.now(UTC).isoformat(), "status": "prepared", "changed_files": list(prepared.files),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return backup
