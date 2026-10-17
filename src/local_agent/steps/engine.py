"""Выполнение шагов скилла: шаги идут по порядку, каждый — своим способом, модель видит только задание своего шага.

- model — один запрос к модели: задание, данные пользователя, файлы шага и короткие итоги прошлых шагов;
  с «по частям: документ» — такой запрос для каждой части документа, ответы подряд пишутся в файл шага,
  а ход работы — в .meepo/parts: остановленный шаг продолжается с той части, на которой встал;
- agent — модель с инструментами шага, пока не напишет итог (или не кончатся действия);
- tool — один вызов инструмента с параметрами из steps.md;
- command — команда в папке проекта; код возврата не 0 — шаг не получился;
- script — скрипт из папки scripts/ скилла в песочнице: аргументы — JSON в stdin, итог — stdout, строки stderr —
  ход работы; модель сессии скрипт получает переменными MEEPO_LLM_*.

Запуск пользователь утверждает один раз, карточкой с шагами, данными и тем, что шаги будут делать
(записывать файлы, запускать команды и скрипты, открывать сайт): поэтому внутри шагов отдельных подтверждений нет.
Шаг, который не получился, останавливает работу — или, если так написано в steps.md, возвращает к прежнему шагу
с текстом ошибки («при ошибке: Написать тесты, 3»).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from local_agent.agents.main import LANGUAGE_RULE
from local_agent.connectors.pyenv import PythonEnv
from local_agent.connectors.sandbox import SandboxPolicy, run_script
from local_agent.core.context import drop_stale_snapshots, estimate_tokens, fit_turn
from local_agent.core.graph import NUDGE, describe_parameters, parse_arguments, prepare_arguments, promises_action
from local_agent.core.tool import Tool, ToolResult
from local_agent.llm.base import LLMEmptyResponse, LLMProvider, LLMProviderError, LLMRepetition, stream_chat
from local_agent.llm.models import ChatMessage, ChatResult
from local_agent.shared.document import MAX_DOCUMENT_BYTES, Part, split_parts
from local_agent.shared.folders import SKIPPED_DIRS
from local_agent.skills.models import Skill
from local_agent.steps.files import PLACEHOLDER
from local_agent.steps.models import Step, Steps

logger = logging.getLogger(__name__)

# Наборы инструментов для строки «инструменты:» — так шаг не перечисляет каждый инструмент браузера.
FILE_TOOLS = ("list_files", "read_file", "search_files", "write_file", "edit_file")
READ_TOOLS = ("list_files", "read_file", "search_files")
TOOL_SETS = {
    "браузер": "browser", "browser": "browser",
    "файлы": "files", "files": "files",
    "чтение": "read", "read": "read",
    "интернет": "search", "поиск": "search", "search": "search",
}
HIDDEN = ("run_code_unsafe",)
# Шагов агента по умолчанию и за раунд; дольше — шаг не получился.
DEFAULT_ROUNDS = 30
MAX_CALLS_PER_ROUND = 4
MAX_NUDGES = 2
DEFAULT_TIMEOUT = 600
# Скрипт обычно долгий (генерация по документации): ждём дольше команды, если в шаге не задан свой таймаут.
SCRIPT_TIMEOUT = 3600
# Доля окна модели под файлы шага и сколько символов одного файла попадает целиком.
FILES_SHARE = 0.45
FILE_CHARS = 12_000
MAX_FILE_BYTES = 512 * 1024
MAX_TREE_LINES = 150
# Сколько от итога прошлого шага видит следующий: целиком итоги передаются файлами («сохранить» → «файлы»).
PREVIOUS_CHARS = 600
OUTPUT_CHARS = 8_000
PREVIEW_CHARS = 400
REPORT_CHARS = 4_000
RESPONSE_RESERVE = 1024
PROGRESS_EVERY = 1.0
# Шаг-инструмент не сообщает, какие файлы записал: их находим по времени изменения — не больше стольких.
MAX_CHANGED_FILES = 10
# Шаг по частям: часть — не больше этой доли окна и не больше PART_TOKENS (маленькая модель на длинном куске
# пропускает детали); ход работы — в PARTS_DIR, ответ «—» значит «в этой части нечего».
PARTS_SHARE = 0.3
PART_TOKENS = 8000
PARTS_DIR = ".meepo/parts"
EMPTY_ANSWERS = {"", "—", "–", "-"}
# Сначала — то, что объясняет проект: описание, настройки, затем код и остальное.
FIRST_FILES = ("readme", "contributing", "pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py",
               "requirements", "package.json", "playwright.config", ".env.example")
TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".toml", ".ini", ".cfg", ".json", ".yaml", ".yml", ".js", ".ts", ".mjs", ".tsx",
    ".jsx", ".java", ".kt", ".go", ".rs", ".cs", ".rb", ".php", ".sh", ".feature", ".html", ".css", ".xml",
    ".csv", ".sql", ".env", ".example", ".lock", ".gradle", ".properties", ".rst",
}

STEP_RULES = (
    "Ты выполняешь один шаг работы, разбитой на шаги. Делай только этот шаг: то, что написано в задании. "
    "Опирайся на данные пользователя, файлы и итоги прошлых шагов из сообщения; не выдумывай того, чего там нет. "
    "Ответ — сам результат шага, без вступлений и пересказа задания."
)
AGENT_RULES = (
    "Действуй через инструменты, а не описывай, что собираешься сделать. Когда шаг выполнен, ответь итогом: "
    "что сделано и что получилось (файлы, найденное). Если сделать нельзя — так и напиши, почему."
)


class StepFailed(Exception):
    """Шаг не получился: текст — причина или вывод команды, его видит пользователь и (при повторе) модель."""


@dataclass
class Toolkit:
    """Чем шаги могут пользоваться: по нему видно ещё до запуска, чего не хватит."""

    # Инструмент по id (в том числе дополнение, которое агенту не включено) и все инструменты — для наборов.
    tool: Callable[[str], Tool | None]
    tools: Callable[[], list[Tool]]
    read_only: bool = False
    # Окружение песочницы: в нём скрипты скиллов видят пакеты, поставленные с согласия пользователя.
    env: PythonEnv | None = None


@dataclass
class RunContext:
    provider: LLMProvider
    model: str
    workspace: Path
    # Окно модели в токенах: в него укладывается каждый запрос шага.
    window: int
    kit: Toolkit
    # Доступ скрипта к модели сессии: MEEPO_LLM_URL, MEEPO_LLM_MODEL, MEEPO_LLM_KEY, MEEPO_LLM_VERIFY_SSL.
    llm_env: dict[str, str] = field(default_factory=dict)


@dataclass
class StepState:
    title: str
    kind: str
    status: str = "pending"
    # Чем шаг занят сейчас или чем закончился — одной строкой.
    detail: str = ""
    output: str = ""
    files: list[str] = field(default_factory=list)
    seconds: float = 0.0
    tries: int = 0

    def view(self, chars: int) -> dict:
        return {
            "title": self.title, "kind": self.kind, "status": self.status, "detail": self.detail,
            "output": _clip(self.output, chars), "files": list(self.files), "seconds": round(self.seconds, 1),
            "tries": self.tries,
        }


def step_tools(step: Step, context: Toolkit) -> tuple[list[Tool], list[str]]:
    """Инструменты шага-агента и какие из названных не нашлись."""
    every = context.tools()
    found: dict[str, Tool] = {}
    missing: list[str] = []
    for name in step.tools:
        group = TOOL_SETS.get(name.casefold())
        if group == "files":
            picked = [tool for tool_id in FILE_TOOLS if (tool := context.tool(tool_id))]
        elif group == "read":
            picked = [tool for tool_id in READ_TOOLS if (tool := context.tool(tool_id))]
        elif group in ("browser", "search"):
            picked = [tool for tool in every if getattr(tool, "server", None) == group and not tool.id.endswith(HIDDEN)]
            if group == "search" and (fetch := context.tool("fetch_url")):
                picked.append(fetch)
        else:
            picked = [tool] if (tool := context.tool(name)) else []
        if not picked:
            missing.append(name)
        for tool in picked:
            found.setdefault(tool.id, tool)
    tools = list(found.values())
    if context.read_only:
        tools = [tool for tool in tools if not tool.requires_approval]
    return tools, missing


def script_path(skill: Skill, name: str) -> Path | None:
    """Скрипт шага в папке scripts/ скилла; None — такого файла нет."""
    if skill.folder is None:
        return None
    path = skill.folder / "scripts" / name
    return path if path.is_file() else None


def problems(skill: Skill, context: Toolkit) -> list[str]:
    """Что помешает шагам ещё до запуска: нет инструмента или скрипта, проект только для чтения."""
    found: list[str] = []
    for index, step in enumerate(skill.steps.steps if skill.steps else (), start=1):
        place = f"Шаг {index} «{step.title}»"
        if step.kind == "agent":
            tools, missing = step_tools(step, replace(context, read_only=False))
            for name in missing:
                group = TOOL_SETS.get(name.casefold())
                hint = (f" (включите сервер {group}: «Возможности → Инструменты → MCP»)"
                        if group in ("browser", "search") else "")
                found.append(f"{place}: нет инструмента «{name}»{hint}")
            if context.read_only and any(tool.requires_approval for tool in tools):
                found.append(f"{place}: проект открыт только для чтения, а агенту шага нужно менять файлы или систему")
        if step.kind == "tool" and context.tool(step.text) is None:
            found.append(f"{place}: нет инструмента {step.text}")
        if step.kind == "script" and script_path(skill, step.text) is None:
            found.append(f"{place}: нет скрипта scripts/{step.text} в папке скилла")
        if context.read_only and (step.kind in ("command", "script") or step.save or (
            step.kind == "tool" and (tool := context.tool(step.text)) is not None and tool.requires_approval
        )):
            found.append(f"{place}: проект открыт только для чтения, а шаг меняет файлы или запускает команду")
    return found


class StepRunner:
    def __init__(
        self, skill: Skill, inputs: dict[str, str], context: RunContext,
        on_progress: Callable[[dict], None] = lambda snapshot: None,
    ) -> None:
        self.skill = skill
        self.plan: Steps = skill.steps
        self.inputs = {item.name: str(inputs.get(item.name) or "").strip() for item in self.plan.inputs}
        self.context = context
        self.root = context.workspace.resolve(strict=True)
        self.on_progress = on_progress
        self.states = [StepState(step.title, step.kind) for step in self.plan.steps]
        self.status = "running"
        self.error = ""
        self.current = 0
        self.started = time.monotonic()
        self._emitted = 0.0
        # Ошибка, с которой работа вернулась к шагу: её получает задание этого шага.
        self._retry_notes: dict[int, str] = {}

    # ---------- ход работы ----------

    async def run(self) -> dict:
        steps = self.plan.steps
        missing = [item.name for item in self.plan.inputs if not item.optional and not self.inputs[item.name]]
        if missing:
            return self._fail(f"В карточке не заполнены поля: {', '.join(missing)} — шаги не начинались")
        if found := problems(self.skill, self.context.kit):
            return self._fail("; ".join(found))
        retries: dict[int, int] = {}
        index = 0
        while index < len(steps):
            step, state = steps[index], self.states[index]
            self.current = index
            # Файлы прошлых попыток остаются в отчёте: повтор обычно дописывает к ним, а не начинает с нуля.
            state.status, state.detail, state.tries = "running", "", state.tries + 1
            self._emit(force=True)
            started = time.monotonic()
            try:
                output = await self._run_step(index, step)
                # Шаг по частям пишет файл сам, по мере работы; его итог — короткая сводка, а не содержимое.
                if step.save and not step.parts and (saved := self._save(step, output)) not in state.files:
                    state.files.append(saved)
            except StepFailed as exc:
                state.seconds += time.monotonic() - started
                state.output, state.status, state.detail = str(exc), "failed", _first_line(str(exc))
                if step.on_error == "continue":
                    index += 1
                elif step.on_error == "retry" and retries.get(index, 0) < step.attempts:
                    retries[index] = retries.get(index, 0) + 1
                    self._retry_notes[step.retry_to] = (
                        f"Прошлая попытка не прошла: шаг «{step.title}» не получился "
                        f"(попытка {retries[index]} из {step.attempts}). Что вышло:\n{_clip(str(exc), 3000, tail=True)}\n"
                        "Исправь причину и сделай шаг заново."
                    )
                    for later in self.states[step.retry_to:index]:
                        later.status = "pending"
                    index = step.retry_to
                else:
                    return self._fail(f"Шаг {index + 1} «{step.title}» не получился: {_first_line(str(exc), 300)}")
                continue
            state.seconds += time.monotonic() - started
            state.output, state.status, state.detail = output, "done", _first_line(output)
            index += 1
        self.status = "done"
        self._emit(force=True)
        return self.snapshot(REPORT_CHARS)

    def _fail(self, error: str) -> dict:
        self.status, self.error = "failed", error
        for state in self.states:
            if state.status in ("pending", "running"):
                state.status = "skipped" if state.status == "pending" else "failed"
        self._emit(force=True)
        return self.snapshot(REPORT_CHARS)

    async def _run_step(self, index: int, step: Step) -> str:
        try:
            if step.kind == "command":
                return await self._command(index, step)
            if step.kind == "tool":
                return await self._tool(index, step)
            if step.kind == "script":
                return await self._script(index, step)
            if step.kind == "agent":
                return await self._agent(index, step)
            if step.parts:
                return await self._parts(index, step)
            return await self._model(index, step)
        except LLMProviderError as exc:
            # Сервер модели недоступен или ответил ошибкой (например, запрос не влез в окно): шаг не получился.
            raise StepFailed(f"Модель не ответила: {exc}") from None
        except OSError as exc:
            raise StepFailed(str(exc)) from None

    # ---------- виды шагов ----------

    async def _model(self, index: int, step: Step) -> str:
        messages = [ChatMessage("system", self._system(step)), ChatMessage("user", self._task(index, step))]
        result = await self._ask(index, self._fit(messages))
        text = (result.content or "").strip()
        if not text:
            raise StepFailed("Модель не написала результат шага")
        return text

    async def _parts(self, index: int, step: Step) -> str:
        """Задание шага для каждой части документа отдельно: ответы подряд — в файл «сохранить», ход работы —
        в .meepo/parts. Тот же документ с тем же заданием продолжается с первой несделанной части."""
        relative = fill(step.parts, self.inputs).strip().strip("`")
        source = self._path(relative)
        text = await asyncio.to_thread(_document, source, relative)
        limit = min(PART_TOKENS, int(self.context.window * PARTS_SHARE))
        parts = await asyncio.to_thread(split_parts, text, limit)
        if not parts:
            raise StepFailed(f"Документ {relative} пустой")
        target = self._path(fill(step.save, self.inputs))
        if target == self.root or target.is_dir():
            raise StepFailed(f"«сохранить: {fill(step.save, self.inputs)}» — это папка, нужен путь к файлу")
        saved = target.relative_to(self.root).as_posix()
        task = fill(step.text, self.inputs)
        place = hashlib.sha256(f"{self.skill.id}\n{step.title}\n{relative}\n{saved}".encode()).hexdigest()[:16]
        work = self.root / PARTS_DIR / f"{place}.json"
        key = hashlib.sha256(f"{task}\n{limit}\n{text}".encode()).hexdigest()
        done = await asyncio.to_thread(_progress, work, key)
        state = self.states[index]
        if saved not in state.files:
            state.files.append(saved)
        for number, part in enumerate(parts, start=1):
            if str(number) in done:
                continue
            prefix = f"часть {number} из {len(parts)}" + (f" · {_first_line(part.title, 60)}" if part.title else "") + " · "
            state.detail = prefix + "думает…"
            self._emit(force=True)
            messages = [
                ChatMessage("system", self._system(step)),
                ChatMessage("user", self._task(index, step, (number, len(parts), part, relative))),
            ]
            result = await self._ask(index, self._fit(messages), prefix=prefix)
            answer = unfence((result.content or "").strip(), target.suffix).strip()
            done[str(number)] = "" if answer.strip(" .*_").casefold() in EMPTY_ANSWERS else answer
            await asyncio.to_thread(_write_parts, work, key, done, target, len(parts))
        await asyncio.to_thread(_write_parts, work, key, done, target, len(parts))
        # Работа закончена: повторный запуск с теми же данными начнёт заново, а не вернёт готовое.
        work.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            work.parent.rmdir()
        self._retry_notes.pop(index, None)
        useful = sum(1 for value in done.values() if value)
        return (f"Документ {relative} пройден по частям: {len(parts)} {_parts_word(len(parts))}, результат есть по "
                f"{useful} из них; всё записано в {saved}.")

    async def _agent(self, index: int, step: Step) -> str:
        tools, _ = step_tools(step, self.context.kit)
        by_id = {tool.id: tool for tool in tools}
        definitions = [
            {"type": "function", "function": {"name": tool.id, "description": tool.description, "parameters": tool.parameters}}
            for tool in tools
        ]
        schemas = estimate_tokens(json.dumps(definitions, ensure_ascii=False))
        budget = max(self.context.window - RESPONSE_RESERVE, 0)
        messages = [
            ChatMessage("system", f"{self._system(step)}\n\n{AGENT_RULES}"),
            ChatMessage("user", self._task(index, step)),
        ]
        limit = step.rounds or DEFAULT_ROUNDS
        nudges = 0
        for round_number in range(limit):
            fitted, _ = fit_turn(drop_stale_snapshots(messages), budget, schemas)
            result = await self._ask(index, fitted, definitions)
            if result.tool_calls:
                messages.append(ChatMessage("assistant", result.content, tool_calls=result.tool_calls))
                for position, call in enumerate(result.tool_calls):
                    output = (ToolResult("Лимит вызовов инструментов за один раз", is_error=True)
                              if position >= MAX_CALLS_PER_ROUND else await self._call(index, by_id, call))
                    messages.append(ChatMessage("tool", output.content, tool_call_id=call.id))
                continue
            text = (result.content or "").strip()
            if text and nudges < MAX_NUDGES and round_number + 1 < limit and promises_action(text):
                nudges += 1
                messages += [ChatMessage("assistant", text), ChatMessage("user", NUDGE)]
                continue
            if not text:
                raise StepFailed("Агент закончил шаг без итога")
            return text
        raise StepFailed(f"Агент не закончил шаг за {limit} действий — можно увеличить «шагов:» в steps.md")

    async def _call(self, index: int, by_id: dict[str, Tool], call) -> ToolResult:
        tool = by_id.get(call.name)
        if tool is None:
            return ToolResult(f"Инструмента «{call.name}» в этом шаге нет. Доступны: {', '.join(by_id)}", is_error=True)
        arguments = parse_arguments(call.arguments)
        if arguments is None:
            return ToolResult("Аргументы должны быть JSON-объектом", is_error=True)
        if problem := prepare_arguments(tool, arguments):
            return ToolResult(f"{problem}. Параметры {tool.id}: {describe_parameters(tool.parameters)}", is_error=True)
        state = self.states[index]
        state.detail = _call_line(tool.id, arguments)
        self._emit()
        try:
            output = await tool.execute(arguments, self.context.workspace)
        except Exception:
            logger.exception("Инструмент шага упал: %s", tool.id)
            output = ToolResult("Внутренняя ошибка инструмента", is_error=True)
        if tool.id in ("write_file", "edit_file") and not output.is_error and isinstance(arguments.get("path"), str):
            path = arguments["path"].strip()
            if path not in state.files:
                state.files.append(path)
        return output

    async def _tool(self, index: int, step: Step) -> str:
        tool = self.context.kit.tool(step.text)
        if tool is None:
            raise StepFailed(f"Инструмента {step.text} нет")
        arguments = _fill_value(step.params, self.inputs)
        if problem := prepare_arguments(tool, arguments):
            raise StepFailed(f"{problem}. Параметры {tool.id}: {describe_parameters(tool.parameters)}")
        self.states[index].detail = _call_line(tool.id, arguments)
        self._emit(force=True)
        started = time.time()
        try:
            output = await tool.execute(arguments, self.context.workspace)
        except (StepFailed, LLMProviderError):
            raise
        except Exception:
            logger.exception("Инструмент шага-инструмента упал: %s", tool.id)
            raise StepFailed(f"Внутренняя ошибка инструмента {tool.id}") from None
        if output.is_error:
            raise StepFailed(output.content)
        self.states[index].files += [path for path in await asyncio.to_thread(self._changed_since, started)
                                     if path not in self.states[index].files]
        return output.content

    async def _script(self, index: int, step: Step) -> str:
        script = script_path(self.skill, step.text)
        if script is None:
            raise StepFailed(f"Нет скрипта scripts/{step.text} в папке скилла")
        state = self.states[index]
        state.detail = f"→ {step.text}"
        self._emit(force=True)

        def line(text: str) -> None:
            state.detail = _first_line(text, 160)
            self._emit()

        env = self.context.kit.env
        python = env.python if env is not None and env.exists else None
        # Запуск утверждён карточкой: скрипт пишет в папку проекта и ходит к модели, а домашняя папка ему не видна.
        policy = SandboxPolicy(step.timeout or SCRIPT_TIMEOUT, network=True, writable_workspace=True)
        started = time.time()
        result = await run_script(
            script, _fill_value(step.params, self.inputs), self.root, policy, python, self.context.llm_env, on_line=line,
        )
        content = result.content
        if key := self.context.llm_env.get("MEEPO_LLM_KEY"):
            # Скрипт мог напечатать ключ API: ни модель, ни чат его не увидят.
            content = content.replace(key, "***")
        if result.is_error:
            raise StepFailed(content)
        state.files += [path for path in await asyncio.to_thread(self._changed_since, started) if path not in state.files]
        return content

    async def _command(self, index: int, step: Step) -> str:
        command = fill(step.text, self.inputs, quote=True)
        timeout = step.timeout or DEFAULT_TIMEOUT
        self.states[index].detail = f"$ {_first_line(command, 120)}"
        self._emit(force=True)
        environment = {**os.environ, "PYTHONUNBUFFERED": "1", "GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1"}
        process = await asyncio.create_subprocess_shell(
            command, cwd=self.root, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True, env=environment,
        )
        try:
            raw, _ = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            _kill(process)
            raise StepFailed(f"Команда не закончилась за {timeout} с: {command}") from None
        except asyncio.CancelledError:
            _kill(process)
            raise
        output = _clip(raw.decode("utf-8", errors="replace").strip(), OUTPUT_CHARS, tail=True)
        if process.returncode != 0:
            raise StepFailed(f"Команда завершилась с кодом {process.returncode}: {command}\n{output}".strip())
        return output or "(команда ничего не вывела)"

    # ---------- запрос к модели ----------

    def _fit(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Запрос без инструментов в окно модели: если не помещается, сокращается середина задания."""
        fitted, _ = fit_turn(messages, max(self.context.window - RESPONSE_RESERVE, 0))
        return fitted

    async def _ask(
        self, index: int, messages: list[ChatMessage], tools: list[dict] | None = None, prefix: str = "",
    ) -> ChatResult:
        """prefix — начало строки хода (у шага по частям — какая часть идёт)."""
        state = self.states[index]
        for attempt in range(3):
            written = 0
            result: ChatResult | None = None
            try:
                async for item in stream_chat(self.context.provider, self.context.model, messages, tools or None):
                    if isinstance(item, ChatResult):
                        result = item
                        continue
                    if isinstance(item, str):
                        written += len(item)
                    # После вызова инструмента строка шага показывает его, пока агент не начнёт писать итог.
                    if written or not state.detail.startswith("→"):
                        state.detail = prefix + (f"пишет… {written} симв." if written else "думает…")
                        self._emit()
                if result is not None:
                    return result
            except (LLMEmptyResponse, LLMRepetition) as exc:
                if attempt == 2:
                    raise StepFailed(f"Модель трижды не справилась с ответом: {exc}") from None
        raise StepFailed("Модель не ответила")

    def _system(self, step: Step) -> str:
        return f"{STEP_RULES}\n{LANGUAGE_RULE}"

    def _task(self, index: int, step: Step, part: tuple[int, int, Part, str] | None = None) -> str:
        """part — (номер, сколько всего, часть, путь документа) у шага по частям."""
        parts = [f"Скилл «{self.skill.name}» — шаг {index + 1} из {len(self.plan.steps)}: {step.title}."]
        # У шага по частям данные, которые уже стоят в задании, пути документа и файла, повторять незачем.
        used = set(PLACEHOLDER.findall(f"{step.text}\n{step.parts}\n{step.save}")) if part else set()
        if values := {name: value for name, value in self.inputs.items() if name not in used}:
            parts.append("Данные от пользователя:\n" + "\n".join(
                f"- {name}: {value or '(не указано)'}" for name, value in values.items()
            ))
        done = [
            f"{number}. {state.title}: {_clip(state.output, PREVIOUS_CHARS)}"
            + (f" (файлы: {', '.join(state.files)})" if state.files else "")
            for number, state in enumerate(self.states[:index], start=1) if state.status == "done"
        ]
        if done:
            parts.append("Итоги прошлых шагов (коротко; полный текст — в их файлах):\n" + "\n".join(done))
        if step.files:
            parts.append(self._files(step))
        if part is not None:
            number, total, piece, relative = part
            where = f"строки {piece.start}–{piece.end}" + (f", раздел «{piece.title}»" if piece.title else "")
            fence = "````" if "```" in piece.text else "```"
            parts.append(f"Документ {relative}, часть {number} из {total} ({where}):\n{fence}\n{piece.text}\n{fence}")
        # Повтор после ошибки: текст ошибки нужен один раз, а у шага по частям — каждой части повтора.
        note = self._retry_notes.get(index, "") if part else self._retry_notes.pop(index, "")
        if note:
            parts.append(note)
        parts.append("Задание:\n" + fill(step.text, self.inputs))
        if part is not None:
            parts.append(
                f"Сделай задание только по этой части документа: остальные {part[1] - 1} обрабатываются отдельно, "
                f"и все ответы подряд лягут в файл {fill(step.save, self.inputs)}, поэтому пиши в одном формате: "
                "как просит задание, а если формат не задан — Markdown-списком «- …». Не пиши вступлений и выводов "
                "про весь документ. Если в этой части нет ничего по заданию, ответь одним символом «—»."
            )
        elif step.save:
            parts.append(f"Твой ответ целиком будет записан в файл {fill(step.save, self.inputs)}.")
        return "\n\n".join(parts)

    # ---------- файлы ----------

    def _files(self, step: Step) -> str:
        budget = int(self.context.window * FILES_SHARE)
        blocks: list[str] = []
        for raw in step.files:
            relative = fill(raw, self.inputs)
            path = self._path(relative)
            if not path.exists():
                raise StepFailed(f"Нет файла или папки {relative} — его должен был создать прошлый шаг")
            if path.is_dir():
                text, used = self._folder(path, budget)
            else:
                text = _file_block(path, self.root)
                used = estimate_tokens(text)
            blocks.append(text)
            budget -= used
        return "Файлы:\n\n" + "\n\n".join(blocks)

    def _folder(self, folder: Path, budget: int) -> tuple[str, int]:
        files: list[Path] = []
        for current, dirs, names in os.walk(folder):
            dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS and not name.startswith("."))
            files += [Path(current) / name for name in sorted(names)]
        relative = folder.relative_to(self.root).as_posix() or "."
        tree = [path.relative_to(self.root).as_posix() for path in files[:MAX_TREE_LINES]]
        if len(files) > MAX_TREE_LINES:
            tree.append(f"… и ещё {len(files) - MAX_TREE_LINES}")
        head = f"Папка {relative} — файлы:\n" + "\n".join(f"- {line}" for line in tree)
        used = estimate_tokens(head)
        blocks = [head]
        skipped = 0
        for path in sorted(files, key=_priority):
            if not _is_text(path):
                continue
            block = _file_block(path, self.root)
            cost = estimate_tokens(block)
            if used + cost > budget:
                skipped += 1
                continue
            blocks.append(block)
            used += cost
        if skipped:
            blocks.append(f"(ещё {skipped} файлов не поместились в окно модели — их можно прочитать отдельным шагом)")
        return "\n\n".join(blocks), used

    def _changed_since(self, moment: float) -> list[str]:
        """Файлы проекта, изменённые после moment: служебные и скрытые папки не смотрим."""
        found: list[tuple[float, str]] = []
        for current, dirs, names in os.walk(self.root):
            dirs[:] = [name for name in dirs if name not in SKIPPED_DIRS and not name.startswith(".")]
            for name in names:
                path = Path(current) / name
                with contextlib.suppress(OSError):
                    if (changed := path.stat().st_mtime) >= moment:
                        found.append((changed, path.relative_to(self.root).as_posix()))
        return [path for _, path in sorted(found)[:MAX_CHANGED_FILES]]

    def _path(self, relative: str) -> Path:
        requested = Path(re.sub(r"\s*/\s*", "/", relative.strip().strip("`")))
        if not relative.strip() or requested.is_absolute():
            raise StepFailed(f"Путь «{relative}» — нужен путь относительно папки проекта")
        target = (self.root / requested).resolve()
        if not target.is_relative_to(self.root):
            raise StepFailed(f"Путь {relative} выходит за пределы папки проекта")
        return target

    def _save(self, step: Step, output: str) -> str:
        relative = fill(step.save, self.inputs)
        path = self._path(relative)
        if path == self.root or path.is_dir():
            raise StepFailed(f"«сохранить: {relative}» — это папка, нужен путь к файлу")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unfence(output, path.suffix).rstrip() + "\n", encoding="utf-8")
        return path.relative_to(self.root).as_posix()

    # ---------- прогресс ----------

    def snapshot(self, chars: int = PREVIEW_CHARS) -> dict:
        return {
            "skill": self.skill.id, "name": self.skill.name, "status": self.status, "error": self.error,
            "current": self.current, "seconds": round(time.monotonic() - self.started, 1),
            "inputs": dict(self.inputs), "steps": [state.view(chars) for state in self.states],
        }

    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._emitted < PROGRESS_EVERY:
            return
        self._emitted = now
        self.on_progress(self.snapshot())


def report(snapshot: dict) -> str:
    """Итог шагов для основного агента: что сделано по шагам и что сказать пользователю."""
    steps = snapshot["steps"]
    done = sum(1 for step in steps if step["status"] == "done")
    lines = [
        f"Шаги скилла «{snapshot['name']}» "
        + ("выполнены" if snapshot["status"] == "done" else f"остановлены: {snapshot['error']}")
        + f" — готово шагов: {done} из {len(steps)}."
    ]
    for number, step in enumerate(steps, start=1):
        mark = {"done": "готово", "failed": "не получился", "skipped": "не выполнялся"}.get(step["status"], step["status"])
        files = f"; файлы: {', '.join(step['files'])}" if step["files"] else ""
        lines.append(f"{number}. {step['title']} — {mark}{files}")
    last = next((step for step in reversed(steps) if step["status"] == "done" and step["output"]), None)
    if last:
        # Вывод шага обычно в конце («можно ли отдавать в работу»): длинный итог — началом и концом.
        lines.append(f"\nИтог шага «{last['title']}»:\n{_ends(last['output'], 2000)}")
    lines.append(
        "\nПользователь уже видит в чате отчёт по шагам. Коротко скажи, что получилось, главный вывод "
        "последнего шага и где лежит результат"
        if snapshot["status"] == "done" else
        "\nПользователь видит в чате, на каком шаге работа остановилась. Коротко объясни причину и что поправить "
        "(данные, steps.md скилла или окружение)"
    )
    lines[-1] += "; работу шагов сам не повторяй и не переделывай."
    return "\n".join(lines)


def fill(text: str, values: dict[str, str], quote: bool = False) -> str:
    """{имя} → значение из «Нужно от вас»; в команде значение берётся в кавычки, чтобы не стать частью команды."""
    def value(match: re.Match) -> str:
        if match[1] not in values:
            return match[0]
        return shlex.quote(values[match[1]]) if quote else values[match[1]]
    return PLACEHOLDER.sub(value, text)


def _fill_value(value: object, values: dict[str, str]) -> object:
    if isinstance(value, str):
        return fill(value, values)
    if isinstance(value, dict):
        return {key: _fill_value(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill_value(item, values) for item in value]
    return value


def unfence(text: str, suffix: str) -> str:
    """Ответ модели для файла: код из блока ``` (для .py и т. п.), Markdown — без обёртки ```markdown целиком."""
    stripped = text.strip()
    whole = re.fullmatch(r"```[\w+-]*\n(.*?)\n?```", stripped, re.S)
    if whole:
        return whole[1]
    if suffix.lower() in ("", ".md", ".txt", ".markdown"):
        return stripped
    blocks = re.findall(r"```[\w+-]*\n(.*?)```", stripped, re.S)
    return max(blocks, key=len) if blocks else stripped


def _document(path: Path, relative: str) -> str:
    """Текст документа для шага по частям: двоичный файл (PDF, DOCX) сначала переводят в Markdown."""
    if not path.is_file():
        raise StepFailed(f"Нет файла {relative}")
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise StepFailed(f"Документ {relative} больше {MAX_DOCUMENT_BYTES // 1024 // 1024} МБ")
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        text = "\x00"
    if "\x00" in text:
        raise StepFailed(f"{relative} — не текстовый файл: сначала переведите его в Markdown (коннектор «Документ → Markdown»)")
    return text


def _progress(work: Path, key: str) -> dict[str, str]:
    """Сделанные части прошлого запуска — если документ, задание и размер части те же."""
    try:
        data = json.loads(work.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    done = data.get("done") if isinstance(data, dict) and data.get("key") == key else None
    return {str(name): value for name, value in done.items() if isinstance(value, str)} if isinstance(done, dict) else {}


def _write_parts(work: Path, key: str, done: dict[str, str], target: Path, total: int) -> None:
    """Ход работы и файл результата после каждой части: результат виден, пока работа идёт."""
    work.parent.mkdir(parents=True, exist_ok=True)
    temporary = work.with_suffix(".tmp")
    temporary.write_text(json.dumps({"key": key, "parts": total, "done": done}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(work)
    answers = [done[str(number)] for number in range(1, total + 1) if done.get(str(number))]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n\n".join(answers).rstrip() + "\n", encoding="utf-8")


def _parts_word(count: int) -> str:
    tens, units = count % 100, count % 10
    if 11 <= tens <= 14:
        return "частей"
    return "часть" if units == 1 else "части" if 2 <= units <= 4 else "частей"


def _file_block(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return f"### {relative}\n(файл больше {MAX_FILE_BYTES // 1024} КБ — не показан)"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"### {relative}\n(не прочитан: {exc})"
    if "\x00" in text[:2048]:
        return f"### {relative}\n(двоичный файл)"
    fence = "````" if "```" in text else "```"
    return f"### {relative}\n{fence}\n{_clip(text, FILE_CHARS)}\n{fence}"


def _is_text(path: Path) -> bool:
    name = path.name.casefold()
    return path.suffix.casefold() in TEXT_SUFFIXES or name.startswith(FIRST_FILES) or name in ("makefile", "dockerfile")


def _priority(path: Path) -> tuple[int, int, str]:
    name = path.name.casefold()
    first = next((position for position, prefix in enumerate(FIRST_FILES) if name.startswith(prefix)), len(FIRST_FILES))
    return first, len(path.parts), path.as_posix()


def _call_line(tool_id: str, arguments: dict) -> str:
    target = next((str(arguments[key]) for key in ("path", "url", "query", "element", "key", "jql", "text", "target")
                   if isinstance(arguments.get(key), str | int) and str(arguments[key]).strip()), "")
    return f"→ {tool_id}" + (f": {_first_line(target, 100)}" if target else "")


def _first_line(text: str, limit: int = 160) -> str:
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    line = re.sub(r"[#*`>|]+", "", line).strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _clip(text: str, limit: int, tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1):] if tail else text[: limit - 1] + "…"


def _ends(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half].rstrip()}\n…\n{text[-half:].lstrip()}"


def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
