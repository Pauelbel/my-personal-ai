"""Вики: поиск без SQLite, чат вики своим агентом, правки после проверки с выбором пунктов, картинки, API."""

import asyncio
import base64
import json

from fastapi.testclient import TestClient

from meepo.app import Services, create_app
from meepo.llm.client import _message_payload
from meepo.llm.fake import FakeLLM
from meepo.settings import Env
from meepo.wiki.search import MarkdownSearch

PNG = "data:image/png;base64," + base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                  "1f15c4890000000d49444154789c6360000000000200015e3d4ff40000000049454e44ae426082")).decode()

NOTE = {"title": "RAG: основы", "description": "Как модель ищет куски документов перед ответом",
        "ideas": ["Документы режутся на куски", "Куски ищутся по смыслу"], "keywords": "rag, поиск, эмбеддинги",
        "tags": ["llm"], "source": "текст из чата"}


def wiki_turn(services, text, decisions=(), attachments=()):
    session = services.sessions.list()
    session = next((item for item in session if item.get("kind") == "wiki"), None) or services.sessions.create(None, "Чат вики", kind="wiki")
    events, decisions = [], list(decisions)

    async def run():
        def emit(event):
            events.append(event)
            if event["type"] == "approval":
                asyncio.get_running_loop().call_soon(services.approvals.decide, session["id"], decisions.pop(0))
        await services.wiki_agent.run(session["id"], text, "r", emit, attachments)

    asyncio.run(run())
    return session, events


def make(tmp_path, script):
    llm = FakeLLM(script)
    services = Services(Env(data_path=tmp_path / "data", default_model="fake"), llm)
    services.wiki.initialize()
    services.memory.initialize()
    services.memory.add("profile.md", "Обращение", "Зовут Павел")
    return services, llm


def test_search_ranks_title_over_text(tmp_path):
    (tmp_path / "a.md").write_text("# Заметка\n\nПро авторизацию пользователей и пароли.\n", encoding="utf-8")
    (tmp_path / "авторизация.md").write_text("# Авторизация\n\nВход по паролю.\n", encoding="utf-8")
    (tmp_path / "history").mkdir()
    (tmp_path / "history" / "old.md").write_text("# Авторизация\n\nстарое\n", encoding="utf-8")
    rows = MarkdownSearch(tmp_path, {"history"}).search("авторизации")
    assert [row[0] for row in rows] == ["авторизация.md", "a.md"]
    assert MarkdownSearch(tmp_path).search("   ") == []


def test_wiki_chat_saves_note_with_own_tools_and_no_memory(tmp_path):
    services, llm = make(tmp_path, [
        {"calls": [("wiki_match", {"keywords": "rag, поиск, эмбеддинги"})]},
        {"calls": [("wiki_save_note", NOTE)]},
        "Создана заметка rag_основы.md.",
    ])
    wiki_turn(services, "добавь заметку: RAG — это поиск кусков документов перед ответом")
    assert "rag" in services.wiki.read("index.md").lower()
    assert services.wiki.read("rag_основы.md").startswith("---")
    first = llm.requests[0]
    names = {tool["function"]["name"] for tool in first["tools"]}
    assert {"wiki_match", "wiki_save_note", "fetch_url"} <= names and "write_file" not in names
    assert "Ты ведёшь личную вики" in first["messages"][0]["content"] and "Павел" not in first["messages"][0]["content"]
    # Чат вики не попадает в список сессий проектов.
    app = create_app(services.env, FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/sessions").json() == []
        assert client.get("/api/wiki/chat").json()["session"]["kind"] == "wiki"


def test_wiki_chat_remembers_last_three_turns(tmp_path):
    services, llm = make(tmp_path, ["1", "2", "3", "4"])
    for text in ("первая", "вторая", "третья", "четвёртая"):
        wiki_turn(services, text)
    users = [message["content"] for message in llm.requests[-1]["messages"] if message["role"] == "user"]
    assert users == ["вторая", "третья", "четвёртая"]


def test_lint_edits_card_applies_only_selected(tmp_path):
    services, _ = make(tmp_path, [
        {"calls": [("wiki_save_note", NOTE)]}, "Создана.",
        {"calls": [("wiki_propose_edits", {"edits": [
            {"kind": "fix", "notes": ["rag_основы.md"], "problem": "нет примера", "change": "добавить пример"},
            {"kind": "outdated", "notes": ["rag_основы.md"], "problem": "старые сведения", "change": "обновить"},
        ]})]},
        "Готово: правка 2 применена.",
    ])
    wiki_turn(services, "добавь заметку")
    session, events = wiki_turn(services, "проверь вики", [{"action": "approve", "selected": [2]}])
    card = next(event["card"] for event in events if event["type"] == "approval")
    assert card["kind"] == "wiki_lint" and [edit["kind"] for edit in card["edits"]] == ["fix", "outdated"]
    result = [m for m in services.sessions.messages(session["id"]) if m["role"] == "tool"][-1]["content"]
    assert "2. устарело: обновить" in result and "Правки 1 не подтверждены" in result


def test_unknown_note_in_edits_is_error_for_model(tmp_path):
    services, _ = make(tmp_path, [
        {"calls": [("wiki_propose_edits", {"edits": [{"kind": "fix", "notes": "нет.md", "problem": "p", "change": "c"}]})]},
        "Понял.",
    ])
    session, events = wiki_turn(services, "проверь вики")
    assert not any(event["type"] == "approval" for event in events)
    assert "таких заметок нет: нет.md" in [m for m in services.sessions.messages(session["id"]) if m["role"] == "tool"][0]["content"]


def test_images_reach_model_and_note(tmp_path):
    services, llm = make(tmp_path, [{"calls": [("wiki_save_note", {**NOTE, "images": []})]}, "Готово."])
    name = services.attachments.save(PNG)
    llm.script[0]["calls"][0][1]["images"] = [name]
    wiki_turn(services, "добавь заметку с картинкой", attachments=(name,))
    sent = llm.requests[0]["messages"][-1]
    assert f"[Картинки: {name}]" in sent["content"] and sent["images"]
    payload = _message_payload(sent)
    assert payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert (services.wiki.root / "images" / name).is_file()
    assert f"images/{name}" in services.wiki.read("rag_основы.md")


def test_wiki_api_pages_search_graph_actions(tmp_path):
    services, _ = make(tmp_path, [{"calls": [("wiki_save_note", NOTE)]}, "Создана."])
    wiki_turn(services, "добавь")
    app = create_app(services.env, FakeLLM([]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert [page["path"] for page in client.get("/api/wiki").json()] == ["rag_основы.md"]
        page = client.get("/api/wiki/page", params={"path": "rag_основы.md"}).json()
        assert page["meta"]["title"] == "RAG: основы" and not page["body"].startswith("---")
        assert client.get("/api/wiki/search", params={"q": "эмбеддинги"}).json()[0]["path"] == "rag_основы.md"
        assert client.get("/api/wiki/graph").json()["nodes"][0]["path"] == "rag_основы.md"
        assert client.get("/api/wiki/actions").json()["actions"][0]["title"] == "Проверить вики"
        uploaded = client.post("/api/attachments", json={"data": PNG}).json()
        assert client.get(uploaded["url"]).status_code == 200
        assert client.post("/api/attachments", json={"data": "data:text/plain;base64,aGk="}).status_code == 400
        assert json.loads(client.put("/api/wiki/actions", json={"actions": [{"title": "Дубли", "text": "найди дубли"}]}).text)["actions"][0]["title"] == "Дубли"
