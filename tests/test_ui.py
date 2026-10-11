"""UI: страница и модули отдаются локально, без сборки и внешних ресурсов; старого холста больше нет."""

import re

from fastapi.testclient import TestClient
from tests.test_graph_flow import settings

from local_agent.api.app import create_app

MODULES = ("app.js", "api.js", "chat.js", "panel.js", "pages.js", "team.js", "files.js", "markdown.js", "highlight.js", "tree.js", "diff.js", "graph.js", "wikichat.js", "resize.js")


def test_ui_is_served_without_build_and_external_resources(tmp_path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        page = client.get("/")
        styles = client.get("/ui/css/app.css")
        scripts = {name: client.get(f"/ui/js/{name}") for name in MODULES}
        removed = [client.get(f"/ui/js/{name}").status_code for name in ("session-canvas.js", "composer.js", "state.js")]

    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert all(response.status_code == 200 for response in scripts.values())
    assert removed == [404, 404, 404]
    # Чат, панель внимания и разделы на месте.
    for element in ('id="agent-bar"', 'id="team"', 'id="files-tree"', 'id="project-mode"', 'id="retry"', 'id="messages"', 'id="cards"', 'id="tasks"', 'data-view="wiki"', 'data-view="capabilities"', 'id="composer"'):
        assert element in page.text
    assert "canvas-pane" not in page.text
    # Всё локально: ни CDN, ни сборки.
    assert not re.search(r"""(src|href)=["']https?://""", page.text)
    for response in scripts.values():
        assert not re.search(r"""from\s+["']https?://""", response.text)
        for imported in re.findall(r"""from\s+["']\./([\w-]+\.js)["']""", response.text):
            assert imported in MODULES
    # Цвета — только токены темы.
    assert not re.findall(r"(?<![\w-])#[0-9a-fA-F]{3,8}(?=[;\s,)])", styles.text)
    assert "@media (max-width: 900px)" in styles.text
