"""Проверка UI подтверждает, что backend отдаёт страницу и её локальные ресурсы."""

import re

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def test_ui_files_are_served(tmp_path) -> None:
    settings = Settings(
        agents_path=tmp_path / "agents",
        sessions_path=tmp_path / "sessions",
        conversations_path=tmp_path / "conversations",
        memory_path=tmp_path / "memory",
        _env_file=None,
    )

    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        styles = client.get("/ui/css/app.css")
        theme_styles = client.get("/ui/css/theme.css")
        script = client.get("/ui/js/app.js")
        chat_script = client.get("/ui/js/chat.js")
        markdown_script = client.get("/ui/js/markdown.js")
        mascot = client.get("/ui/assets/meepo-mascot.png")
        logo = client.get("/ui/assets/meepo-logo.png")
        sidebar_script = client.get("/ui/js/sidebar.js")
        memory_script = client.get("/ui/js/memory.js")
        tools_script = client.get("/ui/js/tools-dialog.js")
        app_script = client.get("/ui/js/app.js")
        canvas_script = client.get("/ui/js/session-canvas.js")
        composer_script = client.get("/ui/js/composer.js")
        projects_script = client.get("/ui/js/projects.js")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert script.headers["cache-control"] == "no-cache"
    assert "Новая сессия" in page.text
    assert 'class="brand"' not in page.text
    assert "Локальный агент</small>" not in page.text
    assert "Локальный режим" not in page.text
    assert "РАБОЧАЯ ОБЛАСТЬ" not in page.text
    assert "v0.1 · LM Studio" not in page.text
    assert 'class="topbar"' not in page.text
    assert 'class="app-header"' in page.text
    assert 'class="header-mascot"' in page.text
    assert 'id="header-tokens"' not in page.text
    assert 'id="usage-text"' in page.text
    assert 'id="usage-speed"' in page.text
    assert "Контекст: —" not in page.text
    assert 'id="edit-title"' in page.text
    assert 'id="title-input"' in page.text
    assert 'id="title-form"' not in page.text
    assert 'class="header-edit"' not in page.text
    assert 'class="session-context"' not in page.text
    assert 'id="customization-panel"' in page.text
    assert 'id="tools-panel"' in page.text
    assert 'id="tools-dialog"' not in page.text
    assert 'id="show-memory"' in page.text
    assert 'id="memory-panel"' in page.text
    assert '<h2 id="memory-title">Память</h2>' not in page.text
    assert 'id="memory-document-description"' in page.text
    assert 'id="update-memory"' not in page.text
    assert "Отправить" in page.text
    assert "Переименовать сессию" in page.text
    assert 'class="composer-config"' in page.text
    assert 'id="save-config"' not in page.text
    assert 'form="message-form"' in page.text
    assert 'id="update-memory-chat"' in page.text
    assert page.text.index('id="update-memory-chat"') < page.text.index('id="save-message"')
    assert 'id="message-form"' in page.text
    assert 'id="turn-recovery"' in page.text
    assert 'id="retry-turn"' in page.text
    assert "Ctrl+Enter — новая строка" in page.text
    assert 'name="theme" value="light"' in page.text
    assert 'name="theme" value="dark"' in page.text
    assert 'name="theme" value="sunset"' not in page.text
    # Сессия удаляется только кнопкой в сайдбаре; «Удалить проект» живёт в диалоге проекта.
    assert "Удалить сессию" not in page.text
    assert styles.status_code == 200
    assert theme_styles.status_code == 200
    assert "composer-shell" in styles.text
    assert 'html[data-theme="dark"]' in theme_styles.text
    assert 'html[data-theme="sunset"]' not in theme_styles.text
    # Темы задают только токены цвета, а форма компонентов общая: терминальный стиль без скруглений.
    for token in ("--bg", "--fg", "--line", "--line-strong", "--accent", "--accent-fg"):
        assert theme_styles.text.count(f"{token}:") == 2
    assert "monospace" in styles.text
    assert 'content: "❯"' in styles.text
    assert "border-radius: 8px" not in styles.text and "box-shadow: 0" not in styles.text
    assert script.status_code == 200
    assert "toolsApi.configure" in tools_script.text
    assert "session?.context_tokens" in script.text
    assert "tokens_per_second" in script.text
    assert 'field.addEventListener("change"' in script.text
    assert 'addEventListener("blur", renameSession)' in script.text
    assert 'addEventListener("keydown", handleMessageKeydown)' in script.text
    assert chat_script.status_code == 200
    assert markdown_script.status_code == 200
    assert mascot.status_code == 200 and mascot.headers["content-type"] == "image/png"
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    assert 'import { appendMarkdown } from "./markdown.js"' in chat_script.text
    assert "markdown-table-container" in styles.text
    assert "Удалить сессию" in sidebar_script.text
    assert "session-delete-button" in sidebar_script.text
    assert "openMenuId" not in sidebar_script.text
    assert "⋯" not in sidebar_script.text
    assert memory_script.status_code == 200
    assert "renderMemoryFiles" in memory_script.text
    assert "хранить историю диалогов в JSONL" in memory_script.text
    assert "не удалять данные без подтверждения" in memory_script.text
    assert "имя — Алексей" in memory_script.text
    assert "отвечать по-русски" in memory_script.text
    assert "разрабатывает Meepo Agent" in memory_script.text

    # Боковой панели нет: вся навигация в шапке, сессии выезжают отдельной панелью.
    for removed in ('class="sidebar"', 'id="sidebar-resizer"', 'id="session-workspace"', 'id="workspace-label"'):
        assert removed not in page.text
    header = page.text[page.text.index('<header class="app-header">'):page.text.index("</header>")]
    # Рядом с названием — рабочая папка сессии (папка проекта); щелчок открывает настройки проекта.
    order = ["show-sessions", "edit-title", "session-folder", "new-session", "tab-canvas", "tab-files", "show-customization", "show-settings"]
    positions = [header.index(f'id="{item}"') for item in order]
    assert positions == sorted(positions)
    assert 'aria-controls="sessions-drawer"' in header and 'aria-expanded="false"' in header
    drawer = page.text[page.text.index('id="sessions-drawer"'):page.text.index("</aside>")]
    assert 'id="new-project"' in drawer and 'id="session-list"' in drawer
    for removed in (".sidebar", ".workspace-button", ".workspace-field", "canvas-collapsed"):
        assert removed not in styles.text
    assert ".sessions-drawer" in styles.text and "--header-height: 36px" in styles.text
    assert "@media (max-width: 900px)" in styles.text
    # Цвета задаются только токенами темы.
    assert not re.findall(r"(?<![\w-])#[0-9a-fA-F]{3,8}(?=[;\s,)])", styles.text)
    assert 'storageKey: "sidebar-width"' not in app_script.text
    assert "Без проекта" not in sidebar_script.text and "DRAFTS_PROJECT_ID" in sidebar_script.text
    assert "canvas-collapsed" not in canvas_script.text and "expanded-cards" not in canvas_script.text
    # Карточка агента — одна строка, настройки открываются окном по ⚙.
    assert 'id="agent-node-dialog"' in page.text and 'id="agent-prompt-dialog"' not in page.text
    assert "openNodeSettings" in canvas_script.text and "agent-card-resize" not in styles.text
    assert "chooseWorkspace" not in composer_script.text and "pickFolder" not in composer_script.text
    assert "перейдут в «Черновики»" in projects_script.text and "Без проекта" not in projects_script.text
