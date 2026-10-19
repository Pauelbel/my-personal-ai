"""Делегирование законченной задачи специализированному агенту."""

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from langgraph.config import get_stream_writer

from agents.models import Agent
from agents.recovery import resumable_snapshot, step_fingerprints
from agents.registry import AgentRegistry
from agents.revisions import agent_revision
from agents.runtime import TESTCASES_LAUNCHER
from core.delegation import ACTIVE, MAX_ATTEMPTS, DelegationStore
from core.tool import CURRENT_TURN, ToolResult
from llm.base import context_length
from llm.registry import LLMRegistry
from memory.service import MemoryService
from steps.engine import FILE_TOOLS, READ_TOOLS, TOOL_SETS, RunContext, StepRunner, Toolkit, problems, report
from tools.workflows import llm_env, workflow_inputs

logger = logging.getLogger(__name__)


class DelegateAgentTool:
    id = "delegate_agent"
    name = "Специализированные агенты"
    requires_approval = True
    description = (
        "Передать законченную задачу подходящему специализированному агенту. Агент работает в отдельном "
        "контексте по своим шагам и возвращает итог и созданные файлы. Передавай уже известные данные в inputs."
    )

    def __init__(
        self, agents: AgentRegistry, providers: LLMRegistry, kit: Toolkit, default_window: int,
        memory: MemoryService | None = None, project_id: str = "",
        *, runs: DelegationStore | None = None, session_id: str = "",
    ) -> None:
        self._agents, self._providers, self._kit = agents, providers, kit
        self._default_window = default_window
        self._memory, self._project_id = memory, project_id
        self._runs, self._session_id = runs, session_id

    def _runnable(self) -> list[Agent]:
        return [agent for agent in self._agents.all() if agent.steps is not None]

    def _kit_for(self, agent: Agent) -> Toolkit:
        """AGENT.md задаёт верхнюю границу; steps.md может только сузить её для конкретного шага."""
        declared = {name.casefold() for name in agent.tools}

        def allowed(tool) -> bool:
            if tool.id.casefold() in declared:
                return True
            for name in declared:
                group = TOOL_SETS.get(name)
                if group == "files" and tool.id in FILE_TOOLS:
                    return True
                if group == "read" and tool.id in READ_TOOLS:
                    return True
                if group in ("browser", "search") and getattr(tool, "server", None) == group:
                    return True
                if group == "search" and tool.id == "fetch_url":
                    return True
            return False

        return Toolkit(
            tool=lambda name: (tool if (tool := self._kit.tool(name)) is not None and allowed(tool) else None),
            tools=lambda: [tool for tool in self._kit.tools() if allowed(tool)],
            read_only=self._kit.read_only,
            env=self._kit.env,
        )

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": [agent.id for agent in self._runnable()] or [""]},
                "inputs": {"type": "object", "additionalProperties": {"type": "string"}},
                "run_id": {"type": "string", "description": "Поручение для восстановления; исполнитель остаётся тем же"},
            },
            "required": ["agent"], "additionalProperties": False,
        }

    def validate(self, arguments: dict[str, object]) -> str | None:
        agent = self._agents.get(arguments.get("agent"))
        if agent is None or agent.steps is None:
            return f"Агент недоступен. Доступные: {', '.join(item.id for item in self._runnable()) or 'нет'}"
        if run_id := arguments.get("run_id"):
            run = self._runs.get(run_id, self._session_id) if self._runs else None
            if run is None or run["agent"] != agent.id:
                return "Поручение не найдено в этой сессии или закреплено за другим агентом"
            if run["status"] in ("done", "stopped"):
                return "Поручение уже завершено или остановлено"
            if run["attempts"] >= MAX_ATTEMPTS:
                return f"Исчерпан лимит запусков выбранного агента: {MAX_ATTEMPTS}"
            inputs = {**run["inputs"], **(arguments.get("inputs") if isinstance(arguments.get("inputs"), dict) else {})}
            if run.get("parent"):
                parent = self._runs.get(run["parent"], self._session_id)
                targets = parent.get("review_targets", []) if parent else []
                index = parent.get("reviewed", 0) if parent else 0
                if (not parent or parent["status"] != "review_pending" or index >= len(targets)
                        or inputs.get("тест-кейсы") != targets[index]
                        or any(inputs.get(key) != run["inputs"].get(key) for key in ("тест-кейсы", "требования", "источник"))):
                    return "Обязательное ревью закреплено за созданными тест-кейсами и исходными требованиями; менять их при восстановлении запрещено"
            arguments["inputs"] = inputs
        arguments["inputs"] = workflow_inputs(agent.as_workflow(), arguments.get("inputs"))
        return None

    def approval_payload(self, arguments: dict[str, object]) -> dict:
        agent = self._agents.get(arguments["agent"])
        workflow = agent.as_workflow()
        payload = {
            "kind": "steps", "title": f"Поручить агенту: «{agent.name}»",
            "run": {**agent.view(), **agent.steps.view(), "values": workflow_inputs(workflow, arguments.get("inputs")),
                    "effects": agent.steps.effects(), "problems": problems(workflow, self._kit_for(agent)),
                    "revision": agent_revision(agent)},
        }
        if arguments.get("run_id") and self._runs and (run := self._runs.get(arguments["run_id"], self._session_id)):
            payload["title"] = f"Восстановить поручение агенту: «{agent.name}»"
            payload["run"]["about"] = (
                f"Повтор поручения {run['id']}, запуск {run['attempts'] + 1} из {MAX_ATTEMPTS}. "
                "Сохраняются подтверждённые готовые шаги с неизменными входами и кодом. "
                "Прерванный или изменённый шаг будет выполнен заново.\n" + payload["run"].get("about", "")
            )
        return payload

    def _result(self, snapshot: dict, agent: str, run: dict | None) -> ToolResult:
        meta = {"steps": snapshot, "agent": agent}
        if run:
            meta["delegation"] = run
        return ToolResult(report(snapshot), is_error=snapshot["status"] != "done", meta=meta)

    def retry_approved(self, run: dict) -> bool:
        agent = self._agents.get(run["agent"])
        return bool(agent and run.get("retry_safe") and agent_revision(agent) == run["revision"])

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(problem, is_error=True)
        if workspace is None:
            return ToolResult("Агенту нужна папка проекта", is_error=True)
        turn = CURRENT_TURN.get() or {}
        provider = self._providers.get(turn.get("provider_id", ""))
        if provider is None or not turn.get("model"):
            return ToolResult("Агенту нужна модель текущей сессии", is_error=True)
        agent = self._agents.get(arguments["agent"])
        workflow = agent.as_workflow()
        revision = agent_revision(agent)
        if turn.get("approved_agent_revision") and turn["approved_agent_revision"] != revision:
            return ToolResult("Агент изменился после подтверждения. Нужно проверить и подтвердить актуальную версию.", is_error=True)
        run = None
        resume = None
        if self._runs:
            # Повтор узла графа возвращает уже сохранённый результат; он не запускает побочные эффекты снова.
            if previous := self._runs.by_call(self._session_id, turn.get("call_id", ""), turn.get("request_id", "")):
                if previous.get("snapshot"):
                    return self._result(previous["snapshot"], agent.id, previous)
                return ToolResult("Запуск прерван: сначала нужно проверить состояние его шагов", is_error=True,
                                  meta={"agent": agent.id, "delegation": previous})
            run = self._runs.get(arguments["run_id"], self._session_id) if arguments.get("run_id") else None
            if run and run["workspace"] != str(workspace.resolve()):
                return ToolResult("Папка проекта изменилась: восстановление поручения в другой папке запрещено", is_error=True)
            if not run:
                run = self._runs.create(
                    session_id=self._session_id, task_id=turn.get("request_id", ""), agent=agent.id,
                    revision=revision, inputs=arguments["inputs"], workspace=str(workspace.resolve()),
                    call_id=turn.get("call_id", ""),
                )
            else:
                resume = resumable_snapshot(run.get("snapshot"), step_fingerprints(agent, arguments["inputs"], workspace), workspace)
            retry_safe = all(
                step.kind == "model" or (
                    step.kind == "script" and agent.folder is not None
                    and (agent.folder / "scripts" / step.text).is_file()
                    and (agent.folder / "scripts" / step.text).read_text(encoding="utf-8").strip() == TESTCASES_LAUNCHER.strip()
                ) for step in agent.steps.steps
            )
            run = self._runs.update(
                run["id"], status="running", attempts=run["attempts"] + 1, revision=revision,
                inputs=arguments["inputs"], calls=list(dict.fromkeys([*run["calls"], turn.get("call_id", "")])),
                call_keys=list(dict.fromkeys([*run.get("call_keys", []), f"{turn.get('request_id', '')}:{turn.get('call_id', '')}"])),
                retry_safe=retry_safe, diagnosis_read=False, parent=run.get("parent") or turn.get("review_parent", ""),
            )
        if self._memory is not None and self._project_id:
            query = " ".join(str(value) for value in arguments["inputs"].values())
            experience = self._memory.qa.search(self._project_id, query, limit=5, confirmed_only=True)
            if experience:
                workflow = replace(workflow, instructions=workflow.instructions + (
                    "\n\nПодтверждённый QA-опыт этого проекта; применяй только если он подходит текущей задаче:\n- "
                    + "\n- ".join(item.compact() for item in experience)
                ))
        try:
            write = get_stream_writer()
        except (RuntimeError, KeyError):
            def write(event):
                return None
        def progress(value):
            if run and self._runs:
                self._runs.update(run["id"], snapshot=value)
            write({"type": "steps", "run": value})

        try:
            window = await context_length(provider, turn["model"]) or self._default_window
            snapshot = await StepRunner(
                workflow, arguments["inputs"],
                RunContext(provider, turn["model"], workspace, window, self._kit_for(agent), {
                    **llm_env(provider, turn["model"]), "MEEPO_AGENT_REVISION": revision,
                }),
                on_progress=progress,
                resume=resume,
                fingerprints=lambda: step_fingerprints(agent, arguments["inputs"], workspace),
            ).run()
        except asyncio.CancelledError:
            # Остановка сервера сохраняет поручение для восстановления. Явный Stop отдельно
            # помечает его stopped через AgentRuntime.mark_stopped после отмены процесса.
            raise
        except Exception as exc:
            logger.exception("Агент %s завершился внутренней ошибкой", agent.id)
            snapshot = {
                "workflow": agent.id, "name": agent.name, "status": "failed", "current": 0,
                "error": f"{type(exc).__name__}: {exc}", "seconds": 0,
                "inputs": arguments["inputs"], "steps": [],
            }
        if run:
            run = self._runs.finish(run["id"], snapshot)
            if snapshot["status"] == "done" and agent.id == "test-cases":
                targets = self._review_targets(workspace, arguments["inputs"], snapshot)
                if targets:
                    run = self._runs.update(run["id"], status="review_pending", review_targets=targets, reviewed=0)
                else:
                    snapshot = {**snapshot, "status": "failed", "error": "Агент не создал проверяемый Markdown-файл с тест-кейсами"}
                    run = self._runs.finish(run["id"], snapshot)
        return self._result(snapshot, agent.id, run)

    @staticmethod
    def _review_targets(workspace: Path, inputs: dict, snapshot: dict) -> list[str]:
        root = workspace.resolve()
        candidates = [root / item for step in snapshot.get("steps", []) for item in step.get("files", []) if item.endswith(".md")]
        source = inputs.get("источник", "")
        if source and not Path(source).is_absolute() and ".." not in Path(source).parts:
            target = root / "testgen" / "test_cases" / source
            candidates += list(target.rglob("*.md")) if target.is_dir() else [target]
        return sorted({path.relative_to(root).as_posix() for candidate in candidates
                       if (path := candidate.resolve()).is_relative_to(root) and path.is_file() and path.stat().st_size})


class StopDelegationTool:
    id = "stop_delegation"
    name = "Отменить поручение агенту"
    description = "Только по просьбе пользователя отменить незавершённое поручение выбранному агенту"
    requires_approval = True
    parameters = {
        "type": "object", "properties": {"run_id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["run_id", "reason"], "additionalProperties": False,
    }

    def __init__(self, runs: DelegationStore, session_id: str) -> None:
        self._runs, self._session_id = runs, session_id

    def validate(self, arguments: dict) -> str | None:
        run = self._runs.get(arguments.get("run_id"), self._session_id)
        if run is None or run["status"] not in ACTIVE:
            return "Незавершённое поручение в этой сессии не найдено"
        if not isinstance(arguments.get("reason"), str) or not arguments["reason"].strip():
            return "Нужна причина отмены поручения"
        return None

    async def execute(self, arguments: dict, workspace: Path | None) -> ToolResult:
        if problem := self.validate(arguments):
            return ToolResult(problem, is_error=True)
        self._runs.stop_run(arguments["run_id"], self._session_id)
        return ToolResult("Поручение отменено пользователем; задача остаётся невыполненной.",
                          meta={"delegation_stopped": arguments["run_id"]})
