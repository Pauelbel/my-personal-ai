"""Граф хода агента на LangGraph: модель → подтверждения → инструменты → модель … → ответ.

Один и тот же граф ведёт основного агента и агента вики: различаются только набор инструментов
и контекст, которые собирает runtime. Состояние — простые словари, чтобы чекпоинт в SQLite
переживал перезапуск сервера. Подтверждения — interrupt в отдельном узле approve, в котором
нет побочных эффектов: при resume LangGraph выполняет узел заново, и инструменты не вызываются дважды.
Карточка лимита шагов («продолжить или остановить?») — interrupt в самом начале call_model, до запроса к модели:
повторный запуск узла после решения тоже ничего не делает дважды.

Узлы пишут в поток через get_stream_writer события того же вида, что раньше отдавал runtime:
log, delta, reasoning, tool_calls, tool_result, user_message, done.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from local_agent.core.context import drop_stale_snapshots, estimate_tokens, fit_turn
from local_agent.core.errors import AgentRuntimeError
from local_agent.core.plan import open_steps, plan_prompt
from local_agent.core.tool import CURRENT_TURN, Tool, ToolResult
from local_agent.core.toolbox import Toolbox
from local_agent.llm.base import LLMEmptyResponse, LLMRepetition, stream_chat
from local_agent.llm.models import ChatMessage, ChatResult, ReasoningDelta, ToolCall
from local_agent.llm.registry import LLMRegistry
from local_agent.memory.conversation import ConversationService
from local_agent.memory.models import MessageStats
from local_agent.sessions.models import Session
from local_agent.sessions.service import SessionService

logger = logging.getLogger(__name__)

MAX_CALLS_PER_ROUND = 4
LOG_ARGUMENTS_CHARS = 160
INTERRUPTED_SUFFIX = "\n\n_(ответ прерван)_"
# Итог хода, остановленного кнопкой «Стоп» до того, как модель что-то написала.
STOPPED_TEXT = "_(ход остановлен)_"
# Запас на ответ для хода из старого чекпоинта, в состоянии которого бюджета ещё нет.
DEFAULT_RESPONSE_RESERVE = 1024
DECISIONS = {"approve", "reject", "reply"}
# Сколько раз за ход модель просят сделать обещанное вызовом инструмента: дальше её ответ — итог хода.
MAX_NUDGES = 3
# Сколько раз за ход модель возвращают к несделанным пунктам плана: дальше её ответ — итог хода.
MAX_PLAN_NUDGES = 4
NUDGE = (
    "Ты написал, что делаешь следующий шаг, но не вызвал инструмент — так ход закончится и работа встанет. "
    "Сделай этот шаг сейчас вызовом инструмента. Если задача уже выполнена целиком — напиши итог; "
    "если без ответа пользователя дальше нельзя — задай вопрос."
)
RUSSIAN_NUDGE = (
    "Ты ответил не по-русски. Все агенты пишут только по-русски. Перепиши свой последний ответ по-русски целиком, "
    "ничего не меняя по смыслу и не вызывая инструментов; код, команды, названия и цитаты оставь как есть."
)
NEXT_STEP_WORDS = {"сейчас", "теперь", "далее", "дальше", "следующий", "следующим", "затем", "потом", "приступаю"}
# Вводные слова перед обещанием: «Для начала я перейду на сайт», «Итак, проверю корзину».
LEAD_WORDS = {"для", "начала", "сначала", "итак", "хорошо", "отлично", "ок", "я", "и", "а", "так", "что", "ж", "теперь", "сейчас"}
# Первое лицо, но не действие: ждёт пользователя, предлагает или подводит итог.
NOT_ACTIONS = {
    "жду", "могу", "предлагаю", "рекомендую", "надеюсь", "прошу", "благодарю", "хочу", "думаю", "считаю",
    "вижу", "советую", "напомню", "готову", "остаюсь", "уточню", "подытожу", "резюмирую",
    # Ждёт ответа: «Сообщу, когда будет готово», «Дождусь вашего ответа».
    "сообщу", "доложу", "отвечу", "вернусь", "дождусь", "подожду", "оповещу", "уведомлю",
}
# Место реестра возможностей в системном промпте, который хранится в состоянии хода.
CATALOG_MARKER = "\u2063CAPABILITIES\u2063"
STOP_NOTE = (
    "Пользователь остановил работу на лимите шагов. Инструменты больше не вызывай. Коротко напиши итог: "
    "что уже сделано (и где лежит результат) и что осталось сделать."
)


class TurnState(TypedDict, total=False):
    session_id: str
    request_id: str
    provider_id: str
    model: str
    window: int
    # Окно за вычетом запаса на ответ: в него укладывается каждый запрос хода.
    budget: int
    rounds: int
    max_rounds: int
    # Контекст запроса к модели в формате ChatMessage, но словарями: так его можно сохранить в чекпоинт.
    messages: list[dict]
    # Решения пользователя по вызовам текущего раунда: id вызова → {"action", "text"}.
    decisions: dict
    # Финальный ответ хода, когда он готов.
    answer: dict | None
    # Сколько сообщений истории попало в контекст и сколько их было в начале хода: для резюме после хода.
    included: int
    history_count: int
    # Сводка для подвала ответа: время модели и инструментов без ожидания решений и токены всех раундов.
    spent: float
    output_tokens: int
    # Сколько раз за ход модель подтолкнули сделать обещанное действие вызовом инструмента.
    nudges: int
    # Сколько раз модель вернули к несделанным пунктам плана.
    plan_nudges: int
    # На сколько шагов «Продолжить» в карточке лимита продлевает ход (обычно — лимит агента).
    round_step: int
    # Пользователь остановил ход на лимите: последний запрос — без инструментов, только итог.
    stopped: bool


@dataclass
class GraphDeps:
    sessions: SessionService
    conversation: ConversationService
    providers: LLMRegistry
    # Инструменты хода собираются заново на каждом узле: подключённые через activate сохраняются в сессии.
    toolbox: Callable[[Session], Toolbox]
    # Реестр возможностей для системного промпта: пересобирается перед каждым запросом,
    # чтобы утверждённое посреди хода модель видела сразу.
    catalog: Callable[[Session, Toolbox], str] | None = None


def build_agent_graph(deps: GraphDeps, checkpointer=None):
    graph = StateGraph(TurnState)
    graph.add_node("call_model", _call_model(deps))
    graph.add_node("approve", _approve(deps))
    graph.add_node("run_tools", _run_tools(deps))
    graph.add_conditional_edges(START, _entry, ["call_model", "approve"])
    graph.add_conditional_edges("call_model", _route, ["approve", "call_model", END])
    graph.add_edge("approve", "run_tools")
    graph.add_edge("run_tools", "call_model")
    return graph.compile(checkpointer=checkpointer)


def _entry(state: TurnState) -> str:
    """Ход обычно начинается запросом к модели. Шаги скилла, запущенные кнопкой, приходят уже готовым вызовом
    инструмента — тогда сразу к подтверждению: модель не решает, запускать ли то, что пользователь выбрал сам."""
    last = state["messages"][-1] if state.get("messages") else {}
    return "approve" if last.get("role") == "assistant" and last.get("tool_calls") else "call_model"


def _route(state: TurnState) -> str:
    if state.get("answer"):
        return END
    # Последней стоит подсказка «сделай обещанное»: снова к модели, вызывать нечего.
    return "call_model" if state["messages"][-1]["role"] == "user" else "approve"


def _session(deps: GraphDeps, state: TurnState) -> Session:
    session = deps.sessions.get(state["session_id"])
    if session is None:
        raise AgentRuntimeError("Сессия удалена во время хода")
    return session


def _call_model(deps: GraphDeps):
    async def call_model(state: TurnState) -> dict:
        write = get_stream_writer()
        session = _session(deps, state)
        box = deps.toolbox(session)
        provider = deps.providers.get(state["provider_id"])
        if provider is None:
            raise AgentRuntimeError("Провайдер LLM не найден")
        rounds, max_rounds = state.get("rounds", 0), state["max_rounds"]
        stopped = state.get("stopped", False)
        limit: dict = {}
        if rounds >= max_rounds and not stopped:
            # Лимит шагов за ход: продолжать или нет, решает пользователь карточкой. Узел ещё ничего не сделал,
            # поэтому после решения LangGraph спокойно выполнит его заново.
            step = state.get("round_step") or max_rounds
            decision = normalize_decision(interrupt({
                "kind": "limit",
                "title": f"Сделано {rounds} {_steps(rounds)} — продолжить работу?",
                "text": (f"За этот ход агент сделал {rounds} {_steps(rounds)} с инструментами — это лимит хода. "
                         f"«Продолжить» даст ещё {step} {_steps(step)}; «Остановить» — агент напишет итог: "
                         "что сделано и что осталось."),
                "session_id": state["session_id"],
            }))
            if decision["action"] == "approve":
                max_rounds = limit["max_rounds"] = rounds + step
            else:
                stopped = limit["stopped"] = True
        definitions = box.definitions()
        offered = definitions if rounds < max_rounds and not stopped else None
        write(log(
            f"Запрос к {state['provider_id']} · {state['model']}" + (f" · раунд {rounds + 1}" if rounds else "")
            + ("" if offered or not definitions else " · без инструментов: работа остановлена на лимите")
        ))
        if offered:
            write(log("Подключены: " + ", ".join(item["function"]["name"] for item in offered)))
        messages = drop_stale_snapshots([to_chat(item) for item in state["messages"]])
        if deps.catalog is not None and messages and messages[0].role == "system":
            messages[0] = ChatMessage("system", (messages[0].content or "").replace(
                CATALOG_MARKER, deps.catalog(session, box),
            ))
        # План задачи — в конце системного промпта перед каждым запросом: так модель не теряет его части.
        if (steps := plan_prompt(session.plan)) and messages and messages[0].role == "system":
            messages[0] = ChatMessage("system", f"{messages[0].content or ''}\n\n{steps}")
        if stopped:
            messages.append(ChatMessage("user", STOP_NOTE))
        # Внутри хода результаты инструментов копятся: если запрос уже не влезает в окно, давние сокращаются.
        budget = state.get("budget") or max(state["window"] - DEFAULT_RESPONSE_RESERVE, 0)
        schemas = estimate_tokens(json.dumps(offered, ensure_ascii=False)) if offered else 0
        messages, compacted = fit_turn(messages, budget, schemas)
        if compacted:
            write(log(f"Контекст хода не помещался в окно: сокращено сообщений — {compacted}"))
        partial: list[str] = []
        thoughts: list[str] = []
        result: ChatResult | None = None
        requested = time.perf_counter()
        first_token_at = None
        # Маленькая модель иногда отвечает пустотой: повторяем тот же запрос, затем — без инструментов.
        attempts = [offered, offered, None] if offered else [None, None]
        for attempt, tools in enumerate(attempts):
            try:
                async for item in stream_chat(provider, state["model"], messages, tools):
                    if first_token_at is None and not isinstance(item, ChatResult):
                        first_token_at = time.perf_counter()
                        write(log(f"Первый токен через {first_token_at - requested:.1f} с"))
                    if isinstance(item, str):
                        partial.append(item)
                        write({"type": "delta", "text": item})
                    elif isinstance(item, ReasoningDelta):
                        thoughts.append(item.text)
                        write({"type": "reasoning", "text": item.text})
                    else:
                        result = item
                break
            except (LLMEmptyResponse, LLMRepetition) as exc:
                looped = isinstance(exc, LLMRepetition)
                if attempt == len(attempts) - 1:
                    raise AgentRuntimeError(
                        "Модель несколько раз зациклилась и повторяла одно и то же. Попробуйте переформулировать "
                        "или начать новую сессию" if looped else
                        "Модель несколько раз вернула пустой ответ. Попробуйте переформулировать или начать новую сессию"
                    ) from None
                # Напечатанное в оборванной попытке не сохраняем: повтор начнёт ответ заново.
                partial.clear()
                thoughts.clear()
                problem = "зациклилась" if looped else "вернула пустой ответ"
                write(log(f"Модель {problem} — повторяю" + (" без инструментов" if attempts[attempt + 1] is None else "")))
                write({"type": "retry"})
            except (asyncio.CancelledError, GeneratorExit):
                # «Стоп» или закрытая вкладка: сохраняем то, что модель успела написать.
                if text := "".join(partial).strip():
                    deps.conversation.add_assistant_message(
                        state["session_id"], text + INTERRUPTED_SUFFIX, reasoning="".join(thoughts),
                    )
                raise
        if result is None:
            raise AgentRuntimeError("Модель не вернула ответ")
        elapsed = time.perf_counter() - requested
        write(log(_round_summary(result, elapsed)))
        written = result.output_tokens if isinstance(result.output_tokens, int) and result.output_tokens > 0 else 0
        totals = {"spent": state.get("spent", 0.0) + elapsed, "output_tokens": state.get("output_tokens", 0) + written, **limit}
        # Не «partial»: в нём могут быть рассуждения, пришедшие текстом, а result.content уже очищен от них.
        text = result.content or ""
        reasoning = "".join(thoughts)
        if result.tool_calls and offered is None:
            # Работу остановили на лимите: инструменты не предлагались, а модель всё равно их зовёт.
            # Ход не обрывается ошибкой — заканчивается ответом, и работу можно продолжить следующим сообщением.
            note = f"Работа остановлена после {rounds} {_steps(rounds)}. Напишите «продолжай» — продолжу с этого места."
            result = replace(result, content=f"{text.strip()}\n\n{note}" if text.strip() else note, tool_calls=())
        if result.tool_calls:
            saved = await asyncio.to_thread(
                deps.conversation.add_assistant_message, state["session_id"], text, result.tool_calls,
                reasoning=reasoning,
            )
            write({"type": "tool_calls", "message": saved})
            assistant = {
                "role": "assistant", "content": text or None,
                "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in result.tool_calls],
            }
            return {"messages": [*state["messages"], assistant], "decisions": {}, "answer": None, **totals}
        if not result.content:
            raise AgentRuntimeError("Модель не вернула ответ")
        nudges = state.get("nudges", 0)
        if nudges < MAX_NUDGES and is_foreign(result.content) and not foreign_allowed(state["messages"]):
            # Ответ ушёл в английский (обычно после английских страниц): в чат его не сохраняем,
            # модель переписывает его по-русски — напечатанное сотрёт «retry».
            write(log("Ответ не на русском — прошу переписать"))
            write({"type": "retry"})
            return {
                "messages": [
                    *state["messages"], {"role": "assistant", "content": result.content},
                    {"role": "user", "content": RUSSIAN_NUDGE},
                ],
                "decisions": {}, "answer": None, "nudges": nudges + 1, **totals,
            }
        plan_nudges = state.get("plan_nudges", 0)
        remaining = open_steps(session.plan) if session.plan_request == state["request_id"] else []
        if remaining and offered and plan_nudges < MAX_PLAN_NUDGES and not result.content.rstrip().endswith("?"):
            # План этого хода не закончен: ответ остаётся в чате промежуточным, модель просим сделать следующий пункт.
            # Вопрос пользователю ход заканчивает — без ответа дальше нельзя.
            saved = await asyncio.to_thread(
                deps.conversation.add_assistant_message, state["session_id"], result.content, reasoning=reasoning,
            )
            write({"type": "tool_calls", "message": saved})
            write(log(f"В плане не сделано пунктов: {len(remaining)} — прошу продолжить"))
            return {
                "messages": [
                    *state["messages"], {"role": "assistant", "content": result.content},
                    {"role": "user", "content": plan_nudge(remaining)},
                ],
                "decisions": {}, "answer": None, "plan_nudges": plan_nudges + 1, **totals,
            }
        if rounds and offered and nudges < MAX_NUDGES and promises_action(result.content):
            # Маленькая модель пишет «Перехожу в корзину.» и молчит: текст без вызова закончил бы ход,
            # и пользователю пришлось бы писать «продолжай». Текст остаётся в чате, модель просим продолжить.
            saved = await asyncio.to_thread(
                deps.conversation.add_assistant_message, state["session_id"], result.content, reasoning=reasoning,
            )
            write({"type": "tool_calls", "message": saved})
            write(log("Модель пообещала действие без вызова инструмента — прошу продолжить"))
            return {
                "messages": [
                    *state["messages"], {"role": "assistant", "content": result.content},
                    {"role": "user", "content": NUDGE},
                ],
                "decisions": {}, "answer": None, "nudges": nudges + 1, **totals,
            }
        generated_for = time.perf_counter() - (first_token_at or requested)
        speed = written / generated_for if written and generated_for > 0.05 else None
        count = result.input_tokens if isinstance(result.input_tokens, int) and result.input_tokens >= 0 else None
        stats = MessageStats(
            seconds=round(totals["spent"], 1), rounds=rounds + 1, input_tokens=count,
            output_tokens=totals["output_tokens"] or None, tokens_per_second=round(speed, 1) if speed else None,
        )
        answer = await asyncio.to_thread(
            deps.conversation.add_assistant_message, state["session_id"], result.content, (), stats, reasoning,
        )
        deps.sessions.set_usage(state["session_id"], {
            "context_tokens": count,
            "context_window": state["window"],
            "tokens_per_second": speed,
        })
        write({"type": "done", "message": answer})
        return {
            "messages": [*state["messages"], {"role": "assistant", "content": result.content}],
            "answer": answer.model_dump(mode="json"),
        }

    return call_model


def _approve(deps: GraphDeps):
    def approve(state: TurnState) -> dict:
        session = _session(deps, state)
        box = deps.toolbox(session)
        decisions: dict[str, dict] = {}
        trusted = set(session.trusted)
        for call in state["messages"][-1]["tool_calls"][:MAX_CALLS_PER_ROUND]:
            tool = box.get(call["name"])
            arguments = parse_arguments(call["arguments"])
            if tool is None or arguments is None or not tool.requires_approval:
                continue
            group = getattr(tool, "trust_group", None)
            if group and group in trusted:
                # Пользователь уже разрешил этой сессии все вызовы группы — карточка не нужна.
                continue
            if problem := prepare_arguments(tool, arguments):
                # Неполный вызов пользователю не показываем: модель сначала исправит его сама.
                decisions[call["id"]] = {"action": "invalid", "text": f"{problem}. Параметры {tool.id}: {describe_parameters(tool.parameters)}"}
                continue
            describe = getattr(tool, "approval_payload", None)
            payload = describe(arguments) if describe is not None else {"kind": "tool_call"}
            decision = normalize_decision(interrupt({
                **payload,
                "call": call,
                "tool_name": tool.name,
                "session_id": state["session_id"],
                # Карточка предложит «утвердить все такие действия в сессии».
                **({"trust_group": group} if group else {}),
            }))
            if decision.pop("trust", False) and group:
                deps.sessions.trust(state["session_id"], group)
                trusted.add(group)
            decisions[call["id"]] = decision
        return {"decisions": decisions}

    return approve


def _run_tools(deps: GraphDeps):
    async def run_tools(state: TurnState) -> dict:
        write = get_stream_writer()
        session = _session(deps, state)
        box = deps.toolbox(session)
        workspace = Path(session.workspace) if session.workspace else None
        messages = list(state["messages"])
        replies: list[str] = []
        round_started = time.perf_counter()
        for index, call in enumerate(messages[-1]["tool_calls"]):
            decision = state.get("decisions", {}).get(call["id"], {"action": "approve"})
            if index >= MAX_CALLS_PER_ROUND:
                output = ToolResult("Лимит вызовов инструментов за один раунд", is_error=True)
            else:
                write(log(f"Вызов {call['name']} {_shorten(call['arguments'], LOG_ARGUMENTS_CHARS)}"))
                started = time.perf_counter()
                output = await _execute(box, call, decision, workspace, {
                    key: state[key] for key in ("session_id", "request_id", "provider_id", "model")
                })
                if decision["action"] == "reply":
                    replies.append(decision["text"])
                write(log(
                    f"{call['name']}: "
                    + (f"ошибка — {_shorten(output.content, LOG_ARGUMENTS_CHARS)}" if output.is_error
                       else f"готово, {len(output.content)} символов")
                    + f" за {time.perf_counter() - started:.1f} с"
                ))
            saved = await asyncio.to_thread(
                deps.conversation.add_tool_message, state["session_id"],
                call_id=call["id"], name=call["name"], content=output.content, is_error=output.is_error, meta=output.meta,
            )
            write({"type": "tool_result", "message": saved})
            messages.append({"role": "tool", "content": output.content, "tool_call_id": call["id"]})
        for text in replies:
            # Ответ на карточку — обычная реплика пользователя: она идёт после результатов вызовов.
            saved = await asyncio.to_thread(deps.conversation.add_user_message, state["session_id"], text)
            write({"type": "user_message", "message": saved})
            messages.append({"role": "user", "content": text})
        return {
            "messages": messages, "rounds": state.get("rounds", 0) + 1, "decisions": {},
            "spent": state.get("spent", 0.0) + time.perf_counter() - round_started,
        }

    return run_tools


async def _execute(box: Toolbox, call: dict, decision: dict, workspace: Path | None, turn: dict) -> ToolResult:
    tool = box.get(call["name"])
    if tool is None:
        return ToolResult(
            f"Инструмента «{call['name']}» нет. Если без него задачу не решить, скажи пользователю, "
            "чего не хватает, и предложи создать его через propose.",
            is_error=True,
        )
    action = decision["action"]
    if action == "invalid":
        return ToolResult(f"Вызов не показан пользователю: {decision['text']}. Исправь и вызови снова.", is_error=True)
    if action == "reject":
        return ToolResult("Пользователь отклонил этот вызов", is_error=True)
    if action == "reply":
        return ToolResult(
            f"Пользователь не разрешил вызов и ответил: «{decision['text']}». "
            "Учти ответ: доработай предложение или спроси, как поступить.",
            is_error=True,
        )
    arguments = parse_arguments(call["arguments"])
    if arguments is None:
        return ToolResult("Аргументы должны быть JSON-объектом", is_error=True)
    if problem := prepare_arguments(tool, arguments):
        # Модель могла позвать инструмент, не подключив его, и угадать имена параметров: подсказываем схему.
        if not box.is_active(tool.id):
            box.activate([tool.id])
        return ToolResult(f"{problem}. Параметры {tool.id}: {describe_parameters(tool.parameters)}. Вызови снова.", is_error=True)
    if not box.is_active(tool.id):
        # Модель позвала инструмент из реестра, не подключив его: подключаем на ходу.
        box.activate([tool.id])
    if "selected" in decision:
        # Схема инструмента это поле запрещает, так что модель подставить его не может — только пользователь.
        arguments = {**arguments, "selected": decision["selected"]}
    if "inputs" in decision:
        # Карточка шагов скилла: поля, которые пользователь заполнил или поправил, важнее того, что подставила модель.
        arguments = {**arguments, "inputs": {**(arguments.get("inputs") or {}), **decision["inputs"]}}
    token = CURRENT_TURN.set(turn)
    try:
        return await tool.execute(arguments, workspace)
    except Exception:
        # Сбой одного инструмента не должен ронять весь ход: модель получает ошибку как результат.
        logger.exception("Инструмент завершился ошибкой request_id=%s tool=%s", turn.get("request_id"), call["name"])
        return ToolResult("Внутренняя ошибка инструмента", is_error=True)
    finally:
        CURRENT_TURN.reset(token)


def prepare_arguments(tool: Tool, arguments: dict) -> str | None:
    """Своя проверка инструмента (она же чинит мелкие промахи модели), затем общая проверка по схеме."""
    validate = getattr(tool, "validate", None)
    if validate is not None and (problem := validate(arguments)):
        return problem
    return check_arguments(tool.parameters, arguments)


def check_arguments(schema: dict, arguments: dict) -> str | None:
    """Грубая проверка по схеме: обязательные параметры есть, лишних нет (если схема их запрещает)."""
    properties = schema.get("properties") or {}
    missing = [name for name in schema.get("required") or [] if isinstance(name, str) and name not in arguments]
    if missing:
        return f"Не хватает параметров: {', '.join(missing)}"
    if properties and schema.get("additionalProperties") is False:
        unknown = [name for name in arguments if name not in properties]
        if unknown:
            return f"Неизвестные параметры: {', '.join(unknown)}"
    return None


def describe_parameters(schema: dict) -> str:
    required = set(schema.get("required") or [])
    parts = []
    for name, spec in (schema.get("properties") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        note = f" — {spec['description']}" if spec.get("description") else ""
        parts.append(f"{name} ({spec.get('type', 'any')}{', обязательный' if name in required else ''}){note}")
    return "; ".join(parts) or "нет"


def _steps(count: int) -> str:
    tens, units = count % 100, count % 10
    if 11 <= tens <= 14:
        return "шагов"
    return "шаг" if units == 1 else "шага" if 2 <= units <= 4 else "шагов"


def plan_nudge(remaining: list[tuple[int, str]]) -> str:
    steps = "; ".join(f"{index}. {text}" for index, text in remaining)
    return (
        f"В плане ещё не сделано: {steps}. Сделай пункт {remaining[0][0]} сейчас — вызовом инструмента, "
        "и отметь его через plan done. Если пункт сделать нельзя — отметь его done с причиной в note "
        "или задай пользователю вопрос."
    )


def promises_action(text: str) -> bool:
    """Ответ кончается обещанием действия («Заполняю данные.», «Теперь проверю корзину»), а не итогом или вопросом."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1].lstrip("#*->•0123456789.) ").rstrip("*_ ")
    if not last or last.endswith("?"):
        return False
    if last.endswith(":"):
        return True
    words = re.findall(r"[а-яё]+", last.casefold())
    if not words:
        return False
    if words[0] in NEXT_STEP_WORDS - LEAD_WORDS:
        return True
    lead = next((index for index, word in enumerate(words) if word not in LEAD_WORDS), len(words))
    if lead == len(words) or len(words) > 16:
        return False
    if lead and words[0] in NEXT_STEP_WORDS:
        return True
    verb = words[lead]
    # «Проверяю», «Перехожу», «Перейду», «Выполню»: глагол первого лица — сейчас или сразу следом, но не «жду» и «могу».
    return verb.endswith(("ю", "у")) and len(verb) >= 4 and verb not in NOT_ACTIONS


def is_foreign(text: str, minimum: int = 60) -> bool:
    """Ответ в основном на латинице: код, ссылки и `идентификаторы` не считаются — это не язык ответа.
    minimum — сколько латинских букв нужно, чтобы судить: в короткой реплике это «OK» или название."""
    prose = re.sub(r"```.*?(```|$)|`[^`]*`|https?://\S+|\S+\.[a-z]{1,5}\b", " ", text, flags=re.S)
    latin = len(re.findall(r"[A-Za-z]", prose))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", prose))
    return latin >= minimum and cyrillic < latin * 0.25


def foreign_allowed(messages: list[dict]) -> bool:
    """Пользователь сам пишет не по-русски или просил другой язык — тогда ответ на нём законен."""
    asked = [item.get("content") or "" for item in messages if item["role"] == "user"]
    asked = [text for text in asked if text not in (NUDGE, RUSSIAN_NUDGE)]
    if any(re.search(r"английск|по-англ|in english|на другом языке|по-немец|по-француз|translate", text, re.I) for text in asked):
        return True
    return bool(asked) and is_foreign(asked[-1], minimum=20)


def normalize_decision(value: object) -> dict:
    """Решение из UI: approve, reject или reply с текстом; всё непонятное считается отказом."""
    if isinstance(value, bool):
        return {"action": "approve" if value else "reject", "text": ""}
    if isinstance(value, dict) and value.get("action") in DECISIONS:
        text = str(value.get("text") or "").strip()
        if value["action"] == "reply" and not text:
            return {"action": "reject", "text": ""}
        decision = {"action": value["action"], "text": text}
        # Карточка со списком (правки вики после lint): номера пунктов, которые пользователь отметил.
        if value["action"] == "approve" and isinstance(value.get("selected"), list):
            decision["selected"] = [item for item in value["selected"] if isinstance(item, int) and not isinstance(item, bool)]
        # Карточка шагов скилла: значения полей «Нужно от вас».
        if value["action"] == "approve" and isinstance(value.get("inputs"), dict):
            decision["inputs"] = {
                str(key): str(item) for key, item in value["inputs"].items() if isinstance(item, str | int | float)
            }
        # «Утвердить все такие действия в сессии»: это вызов и дальше вся его группа без карточек.
        if value["action"] == "approve" and value.get("trust") is True:
            decision["trust"] = True
        return decision
    return {"action": "reject", "text": ""}


def parse_arguments(raw: str) -> dict | None:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def to_chat(item: dict) -> ChatMessage:
    return ChatMessage(
        role=item["role"],
        content=item.get("content"),
        tool_calls=tuple(ToolCall(**call) for call in item.get("tool_calls") or ()),
        tool_call_id=item.get("tool_call_id"),
        images=tuple(item.get("images") or ()),
    )


def from_chat(message: ChatMessage) -> dict:
    item: dict = {"role": message.role, "content": message.content}
    if message.tool_calls:
        item["tool_calls"] = [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in message.tool_calls]
    if message.tool_call_id:
        item["tool_call_id"] = message.tool_call_id
    if message.images:
        item["images"] = list(message.images)
    return item


def log(text: str) -> dict:
    """Строка лога хода: UI показывает её под статусом ответа."""
    return {"type": "log", "text": text}


def _round_summary(result: ChatResult, seconds: float) -> str:
    tokens = ""
    if result.input_tokens is not None or result.output_tokens is not None:
        tokens = f", токенов: вход {result.input_tokens or '?'}, выход {result.output_tokens or '?'}"
    calls = f"; просит инструменты: {', '.join(call.name for call in result.tool_calls)}" if result.tool_calls else ""
    return f"Модель ответила за {seconds:.1f} с{tokens}{calls}"


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = ["GraphDeps", "Tool", "TurnState", "build_agent_graph", "from_chat", "normalize_decision"]
