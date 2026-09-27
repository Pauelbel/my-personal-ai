"""Проверка UI подтверждает, что backend отдаёт страницу и её локальные ресурсы."""

from fastapi.testclient import TestClient

from local_agent.api.app import create_app
from local_agent.config.settings import Settings


def test_ui_files_are_served(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "agent.sqlite3", _env_file=None)

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

    assert page.status_code == 200
    assert "New Session" in page.text
    assert 'class="brand"' not in page.text
    assert "Локальный агент</small>" not in page.text
    assert "Локальный режим" not in page.text
    assert "РАБОЧАЯ ОБЛАСТЬ" not in page.text
    assert "v0.1 · LM Studio" not in page.text
    assert 'class="topbar"' not in page.text
    assert 'class="app-header"' in page.text
    assert 'class="header-mascot"' in page.text
    assert 'class="header-folder"' not in page.text
    assert 'id="header-tokens"' in page.text
    assert 'id="last-tokens"' in page.text
    assert 'id="total-tokens"' in page.text
    assert "Контекст: —" not in page.text
    assert 'id="edit-title"' in page.text
    assert 'id="title-input"' in page.text
    assert 'id="title-form"' not in page.text
    assert 'class="header-edit"' not in page.text
    assert 'class="session-context"' not in page.text
    assert page.text.index('id="show-tools"') < page.text.index('id="new-session"')
    assert 'id="tools-dialog"' in page.text
    assert "Отправить" in page.text
    assert "Переименовать сессию" in page.text
    assert 'class="composer-config"' in page.text
    assert 'id="save-config"' not in page.text
    assert 'form="message-form"' in page.text
    assert 'id="message-form"' in page.text
    assert "Ctrl+Enter — новая строка" in page.text
    assert 'name="theme" value="light"' in page.text
    assert 'name="theme" value="dark"' in page.text
    assert "Удалить" not in page.text
    assert styles.status_code == 200
    assert theme_styles.status_code == 200
    assert "composer-shell" in theme_styles.text
    assert 'html[data-theme="dark"]' in theme_styles.text
    assert "#ffd300" in theme_styles.text
    assert 'content: "◻"' not in styles.text
    assert "border-radius: 50%" in styles.text
    assert "box-shadow: inset 3px" not in theme_styles.text
    assert script.status_code == 200
    assert "toolsApi.configure" in script.text
    assert "selected?.context_tokens" in script.text
    assert "estimateTranscriptTokens(messages)" in script.text
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
