"""Продолжение поручения сохраняет только неизменный завершённый префикс шагов."""

import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path

from agents.models import Agent
from agents.runtime import TESTCASES_LAUNCHER
from shared.folders import SKIPPED_DIRS
from steps.files import PLACEHOLDER


def _workspace_path(workspace: Path, relative: str) -> Path:
    requested = Path(re.sub(r"\s*/\s*", "/", relative.strip().strip("`")))
    if not relative.strip() or requested.is_absolute():
        raise ValueError("Нужен путь относительно папки проекта")
    path = (workspace / requested).resolve()
    if not path.is_relative_to(workspace):
        raise ValueError("Путь выходит за пределы папки проекта")
    return path


def _file_digest(path: Path) -> str:
    if not path.is_file():
        raise ValueError("Входной файл недоступен")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _input_digest(workspace: Path, relative: str) -> object:
    path = _workspace_path(workspace, relative)
    if path.is_file():
        return {"path": path.relative_to(workspace).as_posix(), "digest": _file_digest(path)}
    if not path.is_dir():
        raise ValueError("Входная папка недоступна")
    if path == workspace:
        raise ValueError("Вход охватывает всю папку проекта")
    result = []

    def failed(error: OSError) -> None:
        raise error

    for current, directories, names in os.walk(path, onerror=failed):
        directories[:] = sorted(name for name in directories if name not in SKIPPED_DIRS and not name.startswith("."))
        for name in sorted(names):
            child = Path(current) / name
            if not child.resolve().is_relative_to(workspace):
                raise ValueError("Входной файл выходит за пределы папки проекта")
            result.append((child.relative_to(path).as_posix(), _file_digest(child)))
    return {"path": path.relative_to(workspace).as_posix(), "files": result}


def _implicit_input_digests(workspace: Path, values: dict[str, str]) -> dict:
    result = {}
    for name, value in values.items():
        if not value or "\n" in value or "\r" in value:
            continue
        try:
            path = _workspace_path(workspace, value)
        except (OSError, ValueError, RuntimeError):
            continue
        if not path.is_file() and not path.is_dir():
            continue
        result[name] = _input_digest(workspace, value)
    return result


def step_fingerprints(agent: Agent, inputs: dict, workspace: Path) -> list[str]:
    """Версия шага учитывает его код и фактические входные файлы, независимо от правок остальных шагов.

    Пустой fingerprint запрещает пропуск: отсутствующий или непрочитанный вход нельзя считать неизменным.
    """
    root = workspace.resolve()
    values = {name: str(value or "").strip() for name, value in inputs.items()}
    steps = agent.steps.steps if agent.steps else ()
    try:
        implicit_inputs = _implicit_input_digests(root, values)
    except (OSError, ValueError, RuntimeError):
        return [""] * len(steps)
    result = []
    for index, step in enumerate(steps):
        value = {
            "version": 1, "agent": agent.id, "workspace": str(root), "step": asdict(step), "inputs": values,
            "input_files": implicit_inputs,
        }
        if step.kind in ("model", "agent"):
            value["role"] = {
                "name": agent.name, "prompt": agent.system_prompt, "tools": agent.tools,
                "provider": agent.llm_provider, "model": agent.model, "position": [index, len(steps)],
            }
        try:
            if step.kind == "script":
                if agent.folder is None:
                    raise ValueError("Нет папки скрипта")
                script = agent.folder / "scripts" / step.text
                if not script.is_file():
                    raise ValueError("Нет файла скрипта")
                source = script.read_bytes()
                value["script"] = {"path": str(script.resolve()), "digest": hashlib.sha256(source).hexdigest()}
                if script.name == "testcases.py" and source.replace(b"\r\n", b"\n") == TESTCASES_LAUNCHER.encode("utf-8"):
                    runtime = files("qa").joinpath("testcases.py").read_bytes()
                    value["runtime"] = hashlib.sha256(runtime).hexdigest()
            paths = [*step.files, *([step.parts] if step.parts else [])]
            value["files"] = {
                path: _input_digest(root, PLACEHOLDER.sub(lambda match: values.get(match[1], match[0]), path))
                for path in paths
            }
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            result.append(hashlib.sha256(encoded).hexdigest())
        except (OSError, ValueError, RuntimeError):
            result.append("")
    return result


def resumable_snapshot(snapshot: dict | None, fingerprints: list[str], workspace: Path) -> dict | None:
    """Оставляет только подтверждённый непрерывный префикс done с существующими выходными файлами."""
    if not isinstance(snapshot, dict):
        return None
    previous = snapshot.get("step_fingerprints")
    states = snapshot.get("steps")
    if not isinstance(previous, list) or not isinstance(states, list):
        return None
    root = workspace.resolve()
    completed = []
    for state, before, current in zip(states, previous, fingerprints, strict=False):
        if not isinstance(state, dict) or state.get("status") != "done" or not current or before != current:
            break
        outputs = state.get("files", [])
        if not isinstance(outputs, list):
            break
        try:
            if not all(isinstance(path, str) and _workspace_path(root, path).is_file() for path in outputs):
                break
        except (OSError, ValueError, RuntimeError):
            break
        completed.append(state)
    return deepcopy({**snapshot, "steps": completed}) if completed else None
