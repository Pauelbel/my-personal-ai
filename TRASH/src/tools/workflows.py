"""Общие функции карточек и запуска пошаговых процессов."""

from langgraph.config import get_stream_writer

from llm.base import LLMProvider
from steps.workflow import Workflow

MAX_INPUT_CHARS = 20_000


def run_view(workflow: Workflow) -> dict:
    return {"id": workflow.id, "name": workflow.name, "description": workflow.description, **workflow.steps.view()}


def llm_env(provider: LLMProvider, model: str) -> dict[str, str]:
    env = {
        "MEEPO_LLM_URL": str(getattr(provider, "base_url", "")),
        "MEEPO_LLM_MODEL": model,
        "MEEPO_LLM_VERIFY_SSL": "true" if getattr(provider, "verify_ssl", True) else "false",
    }
    if key := getattr(provider, "api_key", ""):
        env["MEEPO_LLM_KEY"] = key
    return env


def workflow_inputs(workflow: Workflow, raw: object) -> dict[str, str]:
    values = {key: _text(value) for key, value in (raw if isinstance(raw, dict) else {}).items()}
    result = {item.name: values.get(item.name, "") for item in workflow.steps.inputs}
    names = set(result)
    strangers = [value for key, value in values.items() if key not in names and value]
    empty = [item.name for item in workflow.steps.inputs if not item.optional and not result[item.name]]
    if strangers and len(strangers) == len(empty):
        result.update(zip(empty, strangers, strict=True))
    return result


def _text(value: object) -> str:
    if isinstance(value, list):
        value = "\n".join(str(part) for part in value)
    return str(value).strip()[:MAX_INPUT_CHARS] if value is not None else ""


def steps_writer():
    try:
        return get_stream_writer()
    except (RuntimeError, KeyError):
        return lambda event: None
