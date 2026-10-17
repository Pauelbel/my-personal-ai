"""Сборка приложения: сервисы, реестры, ядро агента и какие инструменты получает каждый агент.

Единственный модуль, который знает обо всех пакетах сразу. Остальные зависят только от слоёв ниже:
agents, skills → core → connectors, mcp, catalog → tools → assembly → api.
"""

from collections.abc import Callable
from contextlib import AsyncExitStack

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from local_agent.agents.files import load_agents, warn_unknown_references
from local_agent.agents.main import MAIN_AGENT_ID, MainInstructions
from local_agent.agents.models import Agent
from local_agent.agents.registry import AgentRegistry
from local_agent.agents.team import reachable
from local_agent.catalog.service import CapabilityPaths, CapabilityService
from local_agent.config.settings import Settings
from local_agent.connectors.pyenv import PythonEnv
from local_agent.connectors.secrets import SecretStore
from local_agent.core.delegation import DelegationManager
from local_agent.core.registry import ToolRegistry
from local_agent.core.runtime import AgentRuntime, RuntimeLimits
from local_agent.core.summary import ConversationSummarizer
from local_agent.core.tool import Tool
from local_agent.core.toolbox import Toolbox
from local_agent.core.turns import TurnHub
from local_agent.llm.base import LLMProvider
from local_agent.llm.openai_compatible import OpenAICompatibleProvider
from local_agent.llm.registry import LLMRegistry
from local_agent.llm.store import ProviderStore, build_provider
from local_agent.mcp.manager import MCPManager
from local_agent.memory.attachments import AttachmentStore
from local_agent.memory.conversation import ConversationService
from local_agent.memory.markdown import MarkdownMemoryStore
from local_agent.memory.service import MemoryService
from local_agent.projects.service import ProjectService
from local_agent.sessions.models import Session
from local_agent.sessions.service import SessionService
from local_agent.skills.files import load_skills
from local_agent.skills.registry import SkillRegistry
from local_agent.storage.json.projects import JsonProjectRepository
from local_agent.storage.json.sessions import JsonSessionRepository
from local_agent.storage.jsonl.conversation import JsonlConversationStore
from local_agent.tools.docs import SearchDocsTool
from local_agent.tools.files import EditFileTool, ListFilesTool, ReadFileTool, SearchFilesTool, WriteFileTool
from local_agent.tools.git import GitTool
from local_agent.tools.growth.learn import LearnTool
from local_agent.tools.growth.propose import ArchiveTool, InstallPackagesTool, ProposeTool
from local_agent.tools.memory import MemorySearchTool, RememberTool
from local_agent.tools.plan import PlanTool
from local_agent.tools.team.ask import AskUserTool, RequestToolsTool
from local_agent.tools.team.delegate import AskAgentTool, DelegateTool
from local_agent.tools.team.route import ChooseRouteTool, HandoffTool
from local_agent.tools.testgen.tool import GenerateTestCasesTool, TestCasesExtra
from local_agent.tools.web import FetchUrlTool
from local_agent.tools.wiki import (
    WikiLintTool,
    WikiMatchTool,
    WikiProposeEditsTool,
    WikiReadTool,
    WikiSaveNoteTool,
    WikiSearchTool,
)
from local_agent.wiki.service import WikiService


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


def extra_factories(providers: LLMRegistry) -> dict[str, tuple[str, Callable[[], Tool]]]:
    """Встроенные дополнения, выключенные по умолчанию: id → (описание, фабрика)."""
    return {
        GenerateTestCasesTool.id: (
            "Генерация ручных тест-кейсов по Markdown-документации раздела постранично",
            lambda: TestCasesExtra(providers),
        ),
    }


class CatalogTool:
    """Обёртка помечает инструмент как редкий: модель видит его в реестре и подключает через activate."""

    catalog = True

    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    def __getattr__(self, name: str):
        return getattr(self._tool, name)


def agent_tools(state) -> Callable[[Session, Agent, Toolbox], list[Tool]]:
    """Какие инструменты агент получает всегда, помимо реестра: у основного, у саб-агента и в чате вики — свои."""

    def tools_for(session: Session, agent: Agent, box: Toolbox) -> list[Tool]:
        if session.kind == "wiki":
            # Чат вики без проекта: инструменты вики и чтение ссылок, все сразу со схемой.
            return [
                WikiReadTool(state.wiki_service), WikiSearchTool(state.wiki_service),
                WikiMatchTool(state.wiki_service), WikiSaveNoteTool(state.wiki_service),
                WikiLintTool(state.wiki_service), WikiProposeEditsTool(state.wiki_service),
                FetchUrlTool(),
            ]
        if agent.id != MAIN_AGENT_ID:
            # Саб-агентам — вики, но не личная память пользователя.
            tools: list[Tool] = [WikiSearchTool(state.wiki_service)]
            if "wiki_read" not in agent.tools:
                tools.append(WikiReadTool(state.wiki_service))
            # Команда: саб-агент может обратиться к любому агенту (или к тем, кем ограничен в файле).
            # В личном чате вне проектов обращаться некуда: разговоры агентов живут в сессиях проекта.
            if session.project_id and (targets := reachable(agent, state.agent_registry)):
                tools.append(AskAgentTool(state.delegation, state.agent_registry, session, targets))
            if session.project_id:
                # Не получается или не хватает данных — спросить пользователя; не хватает инструмента — попросить.
                tools += [AskUserTool(agent.name), RequestToolsTool(state.capabilities, state.tool_registry, agent)]
            # Задачу не по профилю саб-агент не делает сам, а передаёт основному агенту.
            tools.append(HandoffTool(state.delegation, session))
            tools.append(PlanTool(state.session_service, session.id, state.conversation_service))
            # «Запомни, делай так впредь» — агент дописывает правило в свой промпт после подтверждения.
            if state.agent_registry.get(agent.id) is not None:
                tools.append(LearnTool(state.capabilities, agent))
            return tools
        tools = [
            RememberTool(state.memory_service, session.id),
            PlanTool(state.session_service, session.id, state.conversation_service),
            ProposeTool(state.capabilities),
            CatalogTool(MemorySearchTool(state.memory_service)),
            CatalogTool(WikiSearchTool(state.wiki_service)),
            # Шаги добавления заметки и список правок lint всегда со схемой: без неё модель пишет
            # правки текстом вместо карточки. Сам lint без параметров — его хватает строки в реестре.
            WikiMatchTool(state.wiki_service),
            WikiSaveNoteTool(state.wiki_service),
            CatalogTool(WikiLintTool(state.wiki_service)),
            WikiProposeEditsTool(state.wiki_service),
            CatalogTool(ArchiveTool(state.capabilities)),
            CatalogTool(InstallPackagesTool(state.capabilities)),
        ]
        # Поручение идёт в разговор агента внутри сессии проекта: в общем чате вне проектов поручать некуда.
        if state.agent_registry.all() and session.project_id:
            tools += [
                DelegateTool(state.delegation, state.agent_registry, session),
                # Кто сделает — сам или саб-агент — решает пользователь, выбор запоминается в проекте.
                ChooseRouteTool(state.project_service, state.agent_registry, session),
            ]
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
    state.project_service.adopt_sessions()
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
        index_path=index_path / "memory.sqlite",
    )
    state.memory_service.initialize()
    state.wiki_service = WikiService(data("wiki_path", "wiki"), index_path / "wiki.sqlite", attachments=state.attachments.path)
    state.wiki_service.initialize()

    state.secrets = SecretStore(data("secrets_path", "secrets.json"))
    state.tool_registry = ToolRegistry([
        ListFilesTool(), ReadFileTool(), SearchFilesTool(),
        WriteFileTool(settings.memory_path), EditFileTool(settings.memory_path),
        GitTool(), SearchDocsTool(index_path / "docs"), FetchUrlTool(),
        WikiReadTool(state.wiki_service),
    ])
    state.agent_registry = AgentRegistry(load_agents(settings.agents_path))
    state.skill_registry = SkillRegistry(load_skills(settings.skills_path))
    state.capabilities = CapabilityService(
        state.tool_registry, state.skill_registry, state.agent_registry,
        CapabilityPaths(data("tools_path", "tools"), settings.skills_path, settings.agents_path),
        extras=extra_factories(state.llm_registry),
        env=PythonEnv(data("sandbox_env_path", "sandbox-env")), secrets=state.secrets,
    )
    state.capabilities.load()

    checkpoints = data("checkpoints_path", "checkpoints.sqlite")
    checkpoints.parent.mkdir(parents=True, exist_ok=True)
    checkpointer = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(checkpoints)))
    state.agent_runtime = AgentRuntime(
        state.session_service,
        state.conversation_service,
        state.agent_registry,
        state.llm_registry,
        state.memory_service,
        state.tool_registry,
        state.skill_registry,
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
    )
    stack.push_async_callback(state.agent_runtime.close)
    state.turn_hub = TurnHub(state.agent_runtime, state.session_service, state.events.publish)
    stack.push_async_callback(state.turn_hub.close)
    state.delegation = DelegationManager(
        state.agent_runtime, state.session_service, state.conversation_service, state.agent_registry,
        max_parallel=settings.max_parallel_tasks,
        provider=settings.subagent_provider, model=settings.subagent_model,
        turns=state.turn_hub,
    )
    stack.push_async_callback(state.delegation.close)

    state.agent_runtime.core_tools.append(agent_tools(state))
    state.agent_runtime.archive_lines = state.capabilities.archive_lines
    state.agent_runtime.secret_names = state.secrets.names
    state.main_instructions = MainInstructions(settings.data_dir / "main-instructions.md")
    state.agent_runtime.main_instructions = state.main_instructions.read
    state.agent_runtime.attachment_path = state.attachments.path
    warn_unknown_references(
        state.agent_registry.all(),
        {tool.id for tool in state.tool_registry.all()},
        {skill.id for skill in state.skill_registry.all()},
    )
    state.mcp = MCPManager(data("mcp_config_path", "mcp.json"), state.tool_registry, data("logs_path", "logs"))
    if settings.mcp_enabled:
        state.mcp.start()
    stack.push_async_callback(state.mcp.close)
