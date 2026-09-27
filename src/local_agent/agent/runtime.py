"""Runtime выполняет один ход агента: собирает контекст, вызывает LLM и сохраняет ответ."""

import logging
import json
import time
from pathlib import Path

from local_agent.agent.context import build_context
from local_agent.agent.registry import AgentRegistry
from local_agent.llm.registry import LLMRegistry
from local_agent.llm.models import ChatMessage
from local_agent.memory.conversation import ConversationService
from local_agent.memory.models import Message
from local_agent.sessions.service import SessionService
from local_agent.tools.registry import ToolRegistry
from local_agent.tools.settings import ToolSettings

logger = logging.getLogger(__name__)


class AgentRuntimeError(Exception):
    """Сессию нельзя обработать из-за неподдерживаемой конфигурации."""


class SessionNotFoundError(AgentRuntimeError):
    """Запрошенная сессия не существует."""


class AgentRuntime:
    def __init__(
        self,
        sessions: SessionService,
        conversation: ConversationService,
        agents: AgentRegistry,
        providers: LLMRegistry,
        max_context_messages: int,
        tools: ToolRegistry,
        tool_settings: ToolSettings,
    ) -> None:
        self._sessions = sessions
        self._conversation = conversation
        self._agents = agents
        self._providers = providers
        self._max_context_messages = max_context_messages
        self._tools = tools
        self._tool_settings = tool_settings

    async def run_turn(self, session_id: str, content: str, request_id: str) -> Message:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError("Session not found")
        agent = self._agents.get(session.agent_id)
        if agent is None:
            raise AgentRuntimeError("Agent not found")
        provider_id = session.provider or agent.llm_provider
        provider = self._providers.get(provider_id)
        if provider is None:
            raise AgentRuntimeError("LLM provider not found")
        model = session.model or agent.model
        if not model:
            raise AgentRuntimeError("Select a model for this session")

        self._conversation.add_user_message(session_id, content)
        self._sessions.name_from_first_message(
            session_id, content, self._conversation.count(session_id)
        )
        context = build_context(
            agent.system_prompt,
            self._conversation.recent(session_id, self._max_context_messages),
            self._max_context_messages,
        )
        enabled_ids = self._tool_settings.enabled_ids()
        allowed = [
            tool for tool in self._tools.all()
            if tool.id in enabled_ids and tool.id in agent.tools
        ] if session.workspace else []
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": tool.id,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in allowed
        ]
        started = time.perf_counter()
        try:
            result = await provider.chat(model, context, definitions) if definitions else await provider.chat(model, context)
            if result.tool_calls:
                # Один ограниченный раунд: модель не может запускать бесконечную цепочку.
                context.append(ChatMessage(role="assistant", content=result.content, tool_calls=result.tool_calls))
                for call in result.tool_calls[:4]:
                    tool = next((item for item in allowed if item.id == call.name), None)
                    if tool is None:
                        output = "Инструмент не разрешён для этой сессии"
                    else:
                        try:
                            arguments = json.loads(call.arguments)
                            if not isinstance(arguments, dict):
                                raise ValueError("Аргументы должны быть объектом")
                            tool_result = await tool.execute(arguments, Path(session.workspace))
                            output = tool_result.content
                        except (ValueError, TypeError) as exc:
                            output = str(exc)
                    context.append(ChatMessage(role="tool", content=output, tool_call_id=call.id))
                if len(result.tool_calls) > 4:
                    for call in result.tool_calls[4:]:
                        context.append(ChatMessage(role="tool", content="Лимит вызовов инструментов за один ход", tool_call_id=call.id))
                result = await provider.chat(model, context)
                if result.tool_calls:
                    raise AgentRuntimeError("Too many tool call rounds")
        except Exception:
            logger.exception(
                "LLM request failed request_id=%s session_id=%s agent_id=%s provider=%s model=%s latency_ms=%.1f",
                request_id, session_id, agent.id, provider_id, model,
                (time.perf_counter() - started) * 1000,
            )
            raise

        if not result.content:
            raise AgentRuntimeError("Model returned no answer")
        answer = self._conversation.add_assistant_message(session_id, result.content)
        count = result.input_tokens
        self._sessions.set_context_tokens(
            session_id, count if isinstance(count, int) and count >= 0 else None
        )
        logger.info(
            "LLM request completed request_id=%s session_id=%s agent_id=%s provider=%s model=%s input_tokens=%s output_tokens=%s latency_ms=%.1f",
            request_id, session_id, agent.id, provider_id, model,
            result.input_tokens, result.output_tokens,
            (time.perf_counter() - started) * 1000,
        )
        return answer
