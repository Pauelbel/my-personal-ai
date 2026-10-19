"""Поручения переживают ход чата: исполнитель, диагностика, попытки и результаты на диске."""

import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

MAX_ATTEMPTS = 3
MAX_REPAIR_ROUNDS = 6
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
ACTIVE = {"running", "failed", "blocked", "review_pending"}


def diagnose(snapshot: dict) -> dict:
    """Классификация по фактам выполнения; неизвестный сбой не считается временным."""
    failed = next((step for step in snapshot.get("steps", []) if step["status"] == "failed"), {})
    details = failed.get("output") or snapshot.get("error") or "Причина не получена"
    text = details.casefold()
    kind, retry, action = "execution", False, "Прочитать файлы агента и исправить причину сбоя."
    if "modulenotfounderror" in text or "importerror" in text:
        kind, action = "runtime", "Проверить импорты скрипта и доступность его зависимостей в песочнице."
    elif any(item in text for item in ("syntaxerror", "nameerror", "typeerror", "attributeerror")):
        kind, action = "code", "Исправить скрипт выбранного агента и проверить его повторным запуском."
    elif any(item in text for item in ("не заполнены поля", "нет папки или файла", "path —", "аргументы —")):
        kind, action = "inputs", "Уточнить входные данные и повторить поручение тому же агенту."
    elif any(item in text for item in ("permissionerror", "permission denied", "только для чтения")):
        kind, action = "access", "Восстановить нужный доступ; расширение прав требует подтверждения."
    elif any(item in text for item in (
        "connection refused", "connection reset", "временно недоступ", "сервер модели недоступен",
        "timed out", "timeout", "не закончился за", "не уложился в", "http 429", "http 502", "http 503", "http 504",
    )):
        kind, retry, action = "transient", True, "Повторить тот же запуск с сохранёнными входными данными."
    return {"kind": kind, "retryable": retry, "details": details[-4000:], "action": action}


class DelegationStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = RLock()

    def get(self, run_id: str, session_id: str | None = None) -> dict | None:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            return None
        with self._lock:
            path = self.root / f"{run_id}.json"
            if not path.is_file():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if session_id is None or value["session_id"] == session_id else None

    def active(self, session_id: str) -> dict | None:
        with self._lock:
            runs = [self.get(path.stem, session_id) for path in self.root.glob("*.json")]
            return max((run for run in runs if run and run["status"] in ACTIVE),
                       key=lambda run: run["updated_at"], default=None)

    def by_call(self, session_id: str, call_id: str, task_id: str | None = None) -> dict | None:
        if not call_id:
            return None
        with self._lock:
            for path in self.root.glob("*.json"):
                run = self.get(path.stem, session_id)
                matches = run and (call_id in run.get("calls", []) if task_id is None
                                   else f"{task_id}:{call_id}" in run.get("call_keys", []))
                if matches:
                    return run
        return None

    def create(self, *, session_id: str, task_id: str, agent: str, revision: str,
               inputs: dict, workspace: str, call_id: str = "") -> dict:
        with self._lock:
            return self._write({
                "id": uuid4().hex, "session_id": session_id, "task_id": task_id,
                "agent": agent, "revision": revision, "inputs": dict(inputs), "workspace": workspace,
                "status": "running", "attempts": 0, "repair_rounds": 0,
                "calls": [call_id] if call_id else [], "diagnosis": {}, "snapshot": None, "history": [],
                "call_keys": [f"{task_id}:{call_id}"] if call_id else [],
            })

    def update(self, run_id: str, **changes) -> dict:
        with self._lock:
            run = self.get(run_id)
            if run is None:
                raise ValueError("Поручение не найдено")
            run.update(changes)
            return self._write(run)

    def finish(self, run_id: str, snapshot: dict) -> dict:
        with self._lock:
            run = self.get(run_id)
            if run is None:
                raise ValueError("Поручение не найдено")
            history = [*run["history"], {
                "attempt": run["attempts"], "revision": run["revision"], "status": snapshot["status"],
                "error": snapshot.get("error", ""),
            }]
            finished = self.update(run_id, status="done" if snapshot["status"] == "done" else "failed",
                                   snapshot=snapshot, history=history,
                                   diagnosis={} if snapshot["status"] == "done" else diagnose(snapshot))
            if snapshot["status"] == "done" and run.get("parent"):
                parent = self.get(run["parent"], run["session_id"])
                if parent and parent["status"] == "review_pending":
                    reviewed = parent.get("reviewed", 0) + 1
                    self.update(parent["id"], reviewed=reviewed,
                                status="done" if reviewed >= len(parent.get("review_targets", [])) else "review_pending")
            return finished

    def stop(self, session_id: str) -> None:
        with self._lock:
            while run := self.active(session_id):
                self.update(run["id"], status="stopped")

    def stop_run(self, run_id: str, session_id: str) -> None:
        """Отмена этапа отменяет и ожидающую его цепочку, включая обязательное ревью."""
        with self._lock:
            run = self.get(run_id, session_id)
            if run is None:
                raise ValueError("Поручение не найдено")
            while run.get("parent") and (parent := self.get(run["parent"], session_id)):
                run = parent
            root = run["id"]
            self.update(root, status="stopped")
            for path in self.root.glob("*.json"):
                child = self.get(path.stem, session_id)
                if child and child.get("parent") == root and child["status"] in ACTIVE:
                    self.update(child["id"], status="stopped")

    def _write(self, value: dict) -> dict:
        value = {**value, "updated_at": datetime.now(UTC).isoformat()}
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, raw = tempfile.mkstemp(prefix=".run-", dir=self.root)
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.root / f"{value['id']}.json")
        finally:
            temporary.unlink(missing_ok=True)
        return deepcopy(value)


def recovery_prompt(run: dict) -> str:
    diagnosis = run.get("diagnosis") or {}
    return (
        f"Восстановление поручения {run['id']}. Исполнитель закреплён: {run['agent']}. "
        f"Попыток запуска: {run['attempts']} из {MAX_ATTEMPTS}.\n"
        f"Диагностика ({diagnosis.get('kind', 'unknown')}):\n{diagnosis.get('details', '')}\n"
        f"Следующее действие: {diagnosis.get('action', '')}\n"
        "Задача пока НЕ выполнена. Сейчас выполняй только восстановление выбранного агента. "
        "Прочитай его через read_capability (kind agent), затем предложи минимальное исправление через "
        "propose_agent_patch. expected_revision возьми из прочитанной версии. После утверждения патча система "
        "сама предложит повторный запуск того же агента. Для исправления входных данных вызови delegate_agent "
        "с тем же agent и run_id, передав исправленные inputs. Смена исполнителя и самостоятельное написание "
        "результата запрещены. Если восстановление невозможно или нужны данные пользователя — объясни, чего "
        "не хватает; система сохранит поручение невыполненным. stop_delegation предлагай только по просьбе "
        "пользователя отменить поручение."
    )


def blocked_report(run: dict, reason: str = "") -> str:
    diagnosis = run.get("diagnosis") or {}
    return (
        f"Поручение агенту «{run['agent']}» не выполнено. Исполнитель сохранён.\n\n"
        f"Причина:\n{diagnosis.get('details') or 'Выполнение было прервано; результат шага не подтверждён.'}\n\n"
        f"{reason or diagnosis.get('action') or 'Нужно проверить состояние шага перед повторным запуском.'}\n"
        f"Попыток запуска: {run['attempts']} из {MAX_ATTEMPTS}. Поручение: {run['id']}."
    )
