"""Сборка приложения: сервисы, реестры, ядро агента и какие инструменты получают основной агент и агент вики.

Единственный модуль, который знает обо всех пакетах сразу. Остальные зависят только от слоёв ниже:
agents, steps → core → connectors, mcp → steps/engine → catalog → tools → assembly → api.
"""

from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agents.files import initialize_agents
from agents.main import MainInstructions
from agents.models import Agent
from agents.registry import AgentRegistry
from catalog.service import CapabilityPaths, CapabilityService
from config.settings import Settings
from connectors.pyenv import PythonEnv
from connectors.secrets import SecretStore
from core.delegation import DelegationStore
from core.registry import ToolRegistry
from core.runtime import AgentRuntime, RuntimeLimits
from core.summary import ConversationSummarizer
from core.tool import Tool, summary_line
from core.toolbox import Toolbox
from core.turns import TurnHub
from llm.base import LLMProvider
from llm.openai_compatible import OpenAICompatibleProvider
from llm.registry import LLMRegistry
from llm.store import ProviderStore, build_provider
from mcp_runtime.manager import MCPManager
from memory.attachments import AttachmentStore
from memory.conversation import ConversationService
from memory.markdown import MarkdownMemoryStore
from memory.service import MemoryService
from projects.service import ProjectService
from sessions.models import Session
from sessions.service import SessionService
from storage.json.projects import JsonProjectRepository
from storage.json.sessions import JsonSessionRepository
from storage.jsonl.conversation import JsonlConversationStore
from tools.agent_patch import ProposeAgentPatchTool
from tools.agents import DelegateAgentTool, StopDelegationTool
from tools.docs import SearchDocsTool
from tools.documents import ProcessDocumentTool
from tools.files import EditFileTool, ListFilesTool, ReadFileTool, SearchFilesTool, WriteFileTool
from tools.git import GitTool
from tools.growth.propose import ArchiveTool, InstallPackagesTool, ProposeTool, ReadCapabilityTool
from tools.memory import MemorySearchTool, QAMemorySearchTool, RememberQAExperienceTool, RememberTool
from tools.plan import PlanTool
from tools.web import FetchUrlTool
from tools.wiki import (
    WikiLintTool,
    WikiMatchTool,
    WikiProposeEditsTool,
    WikiReadTool,
    WikiSaveNoteTool,
    WikiSearchTool,
)
from wiki.actions import WikiActions
from wiki.service import WikiService


def build_providers(settings: Settings, lm_studio: LLMProvider | None) -> LLMRegistry:
    providers: dict[str, LLMProvider] = {
        "lm_studio": lm_studio or OpenAICompatibleProvider(
            settings.llm_base_url, settings.llm_timeout_seconds, name=settings.llm_name,
            verify_ssl=settings.llm_verify_ssl,
        ),
    }
    for config in settings.llm_providers:
        providers[config.id] = OpenAICompatibleProvider(
            config.base_url,
            settings.llm_timeout_seconds,
            name=config.name,
            api_key=config.api_key,
            verify_ssl=settings.llm_verify_ssl,
        )
    return LLMRegistry(providers)


def apply_default(state, provider: str, model: str) -> None:
    """Выбор из настроек заменяет DEFAULT_MODEL из .env на время работы сервера."""
    if provider and model:
        state.default_provider = provider
        state.settings.default_model = model


class CatalogTool:
    """Обёртка помечает инструмент как редкий: модель видит его в реестре и подключает через activate."""

    catalog = True

    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    def __getattr__(self, name: str):
        return getattr(self._tool, name)


def agent_tools(state) -> Callable[[Session, Agent, Toolbox], list[Tool]]:
    """Какие инструменты агент получает всегда, помимо реестра: у основного агента и в чате вики — свои."""

    def tools_for(session: Session, _agent: Agent, _box: Toolbox) -> list[Tool]:
        if session.kind == "wiki":
            # Чат вики без проекта: инструменты вики и чтение ссылок, все сразу со схемой.
            return [
                WikiReadTool(state.wiki_service), WikiSearchTool(state.wiki_service),
                WikiMatchTool(state.wiki_service), WikiSaveNoteTool(state.wiki_service),
                WikiLintTool(state.wiki_service, state.conversation_service), WikiProposeEditsTool(state.wiki_service),
                FetchUrlTool(),
            ]
        tools: list[Tool] = []
        project = state.session_service.project_of(session)
        if session.workspace:
            # Большой документ целиком — по частям; редкая работа, поэтому в реестре одной строкой.
            tools.append(CatalogTool(ProcessDocumentTool(
                state.llm_registry,
                state.capabilities.toolkit(read_only=project is not None and project.mode == "read"),
                session.context_window or state.settings.default_context_tokens, Path(session.workspace),
            )))
            if state.agent_registry.all():
                tools.append(CatalogTool(DelegateAgentTool(
                    state.agent_registry, state.llm_registry,
                    state.capabilities.toolkit(read_only=project is not None and project.mode == "read"),
                    state.settings.default_context_tokens,
                    state.memory_service, session.project_id or session.workspace or "",
                    runs=state.delegations, session_id=session.id,
                )))
        tools += [
            RememberTool(state.memory_service, session.id),
            CatalogTool(RememberQAExperienceTool(state.memory_service, session)),
            PlanTool(state.session_service, session.id, state.conversation_service),
            ProposeTool(state.capabilities),
            CatalogTool(StopDelegationTool(state.delegations, session.id)),
            ReadCapabilityTool(state.capabilities),
            CatalogTool(MemorySearchTool(state.memory_service)),
            CatalogTool(QAMemorySearchTool(state.memory_service, session)),
            CatalogTool(WikiSearchTool(state.wiki_service)),
            # Шаги добавления заметки и список правок lint всегда со схемой: без неё модель пишет
            # правки текстом вместо карточки. Сам lint без параметров — его хватает строки в реестре.
            WikiMatchTool(state.wiki_service),
            WikiSaveNoteTool(state.wiki_service),
            CatalogTool(WikiLintTool(state.wiki_service, state.conversation_service)),
            WikiProposeEditsTool(state.wiki_service),
            CatalogTool(ArchiveTool(state.capabilities)),
            CatalogTool(InstallPackagesTool(state.capabilities)),
        ]
        if project is None or project.mode != "read":
            tools.append(CatalogTool(ProposeAgentPatchTool(state.capabilities)))
        return tools

    return tools_for


async def assemble(state, settings: Settings, stack: AsyncExitStack, llm_provider: LLMProvider | None = None) -> None:
    """Собирает всё приложение в state; закрытие — через stack. state.events уже создан HTTP-слоем."""
    data = settings.data_path
    index_path = data("index_path", "index")
    conversation_store = JsonlConversationStore(settings.conversations_path)
    conversation_store.initialize()
    session_repository = JsonSessionRepository(settings.sessions_path)
    session_repository.initialize()
    project_repository = JsonProjectRepository(settings.projects_path)
    project_repository.initialize()
    session_repository.on_change = lambda session_id: state.events.publish({"type": "changed", "session_id": session_id})
    project_repository.on_change = lambda project_id: state.events.publish({"type": "projects", "project_id": project_id})
    memory_store = MarkdownMemoryStore(settings.memory_path)
    state.attachments = AttachmentStore(data("attachments_path", "attachments"))

    def forget_attachments(session_id: str) -> None:
        # Картинки сессии уходят вместе с её историей; в заметки вики попали их копии.
        state.attachments.delete(name for message in conversation_store.list(session_id) for name in message.attachments)

    state.session_service = SessionService(
        session_repository,
        projects=project_repository,
        on_deleted=(forget_attachments, conversation_store.delete, memory_store.forget),
    )
    state.project_service = ProjectService(project_repository, state.session_service)
    state.conversation_service = ConversationService(
        conversation_store,
        on_saved=lambda message: state.session_service.touch(message.session_id, message.created_at),
    )
    state.llm_registry = build_providers(settings, llm_provider)
    stack.push_async_callback(state.llm_registry.close)
    # Провайдеры, добавленные в настройках интерфейса, — поверх заданных в .env.
    state.provider_store = ProviderStore(settings.data_dir / "providers.json")
    state.default_provider = "lm_studio"
    stored = state.provider_store.load()
    for entry in stored.providers:
        state.llm_registry.set(
            entry.id, build_provider(entry, settings.llm_timeout_seconds, verify_ssl=settings.llm_verify_ssl),
        )
    apply_default(state, stored.default_provider, stored.default_model)
    state.memory_service = MemoryService(
        memory_store,
        state.conversation_service,
        state.llm_registry,
        memory_model=settings.memory_model,
        max_context_chars=settings.max_memory_context_chars,
        index_path=settings.memory_path / "index.sqlite",
    )
    state.memory_service.initialize()
    wiki_path = data("wiki_path", "wiki")
    state.wiki_service = WikiService(
        wiki_path, wiki_path / "index.sqlite", history_path=wiki_path / "history", attachments=state.attachments.path,
    )
    state.wiki_service.initialize()
    state.wiki_actions = WikiActions(settings.data_dir / "wiki-actions.md")

    state.secrets = SecretStore(data("secrets_path", "secrets.json"))
    state.tool_registry = ToolRegistry([
        ListFilesTool(), ReadFileTool(), SearchFilesTool(),
        WriteFileTool(settings.memory_path), EditFileTool(settings.memory_path),
        GitTool(), SearchDocsTool(index_path / "docs"), FetchUrlTool(),
        WikiReadTool(state.wiki_service),
    ])
    agents_path = data("agents_path", "agents")
    state.delegations = DelegationStore(settings.data_dir / "delegations")
    state.agent_registry = AgentRegistry(initialize_agents(agents_path))
    state.capabilities = CapabilityService(
        state.tool_registry,
        CapabilityPaths(data("tools_path", "tools"), agents_path),
        env=PythonEnv(data("sandbox_env_path", "sandbox-env")), secrets=state.secrets,
        agents=state.agent_registry,
    )
    state.capabilities.load()

    checkpoints = data("checkpoints_path", "checkpoints.sqlite")
    checkpoints.parent.mkdir(parents=True, exist_ok=True)
    checkpointer = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(checkpoints)))
    state.agent_runtime = AgentRuntime(
        state.session_service,
        state.conversation_service,
        state.llm_registry,
        state.memory_service,
        state.tool_registry,
        ConversationSummarizer(state.session_service, state.conversation_service, state.llm_registry),
        RuntimeLimits(
            max_context_messages=settings.max_context_messages,
            default_context_tokens=settings.default_context_tokens,
            response_reserve_tokens=settings.response_reserve_tokens,
            memory_auto_update_messages=settings.memory_auto_update_messages,
            wiki_context_messages=settings.wiki_context_messages,
            max_tool_rounds=settings.max_tool_rounds,
        ),
        checkpointer=checkpointer,
        delegations=state.delegations,
    )
    stack.push_async_callback(state.agent_runtime.close)
    state.turn_hub = TurnHub(state.agent_runtime, state.session_service, state.events.publish)
    stack.push_async_callback(state.turn_hub.close)

    state.agent_runtime.core_tools.append(agent_tools(state))
    state.agent_runtime.agent_lines = lambda: [
        f"- {agent.id} — {summary_line(agent.description, 70)}" for agent in state.agent_registry.all()
    ]
    state.agent_runtime.archive_lines = state.capabilities.archive_lines
    state.agent_runtime.secret_names = state.secrets.names
    state.main_instructions = MainInstructions(settings.data_dir / "main-instructions.md")
    state.agent_runtime.main_instructions = state.main_instructions.read
    state.agent_runtime.attachment_path = state.attachments.path
    state.mcp = MCPManager(data("mcp_config_path", "mcp.json"), state.tool_registry, data("logs_path", "logs"))
    if settings.mcp_enabled:
        state.mcp.start()
    stack.push_async_callback(state.mcp.close)
