"""Вики: отдельная папка и индекс, поиск, запись только с подтверждением, API для UI."""

import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of
from tests.test_graph_flow import settings

from local_agent.api.app import create_app
from local_agent.wiki.service import WikiService

PAGE = "# RAG\n\nИсточник: https://example.org/rag\n\n## Идея\n\n- Поиск кусков документов перед ответом модели\n"


def test_pages_are_searched_and_paths_are_checked(tmp_path):
    wiki = WikiService(tmp_path / "wiki", tmp_path / "index" / "wiki.sqlite")
    wiki.write("ai/rag.md", PAGE)

    [(path, title, _line, text)] = wiki.search("поиск документов")
    assert path == "ai/rag.md" and title == "RAG › Идея" and "кусков" in text
    assert [page.title for page in wiki.pages()] == ["RAG"]
    for bad in ("../secret.md", "/etc/x.md", "Заметка.md", "a/b.txt"):
        with pytest.raises(ValueError):
            wiki.write(bad, PAGE)


def test_agent_writes_wiki_only_after_approval_and_ui_reads_it(tmp_path):
    provider = ScriptedProvider(call("wiki_write", path="ai/rag.md", content=PAGE), "Сохранил заметку")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = client.post("/api/sessions", json={"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Сделай заметку"}))[-1]
        assert card["type"] == "interrupt" and card["pending"]["preview"] == PAGE
        assert client.get("/api/wiki").json() == []
        client.post(url + "/resume", json={"action": "approve"})
        pages = client.get("/api/wiki").json()
        found = client.get("/api/wiki/search", params={"q": "RAG"}).json()
        page = client.get("/api/wiki/page", params={"path": "ai/rag.md"}).json()

    assert [item["path"] for item in pages] == ["ai/rag.md"] and found and page["content"].startswith("# RAG")
    # Вики и память лежат отдельно.
    assert (tmp_path / "wiki" / "ai" / "rag.md").exists() and not list((tmp_path / "memory").glob("*rag*"))


def test_search_results_for_ui_are_plain_text_around_the_match():
    from local_agent.api.routes.wiki import plain_text, snippet

    text = plain_text("## Идея\n\n- **Поиск** кусков [документов](https://x.org) перед `ответом`\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert text == "Идея Поиск кусков документов перед ответом a · b 1 · 2"
    long = "начало " * 60 + "искомое слово " + "хвост " * 60
    piece = snippet(long, "искомое")
    assert piece.startswith("…") and piece.endswith("…") and "искомое" in piece
