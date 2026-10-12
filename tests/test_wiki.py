"""Вики: заметки по шаблону OKF, index.md, дополнение по ключевым словам, граф, lint с выбором правок."""

import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session
from tests.test_graph_flow import settings

from local_agent.api.app import create_app
from local_agent.wiki import notes
from local_agent.wiki.service import NoteFields, WikiService


def wiki(tmp_path) -> WikiService:
    return WikiService(tmp_path / "wiki", tmp_path / "index" / "wiki.sqlite")


def fields(title="RAG: основы", keywords=("rag", "поиск", "эмбеддинги"), **extra) -> NoteFields:
    return NoteFields(
        title=title, description="Как модель ищет куски документов перед ответом",
        ideas=("Перед ответом модель ищет куски документов",), keywords=keywords, **extra,
    )


def test_note_names_keywords_and_format_round_trip():
    assert notes.slug("RAG: основы") == "rag_основы.md"
    assert notes.slug("Векторные базы — обзор") == "векторные_базы_обзор.md"
    assert notes.same_keyword("эмбеддинги", "эмбеддинг") and notes.same_keyword("векторная база", "векторные базы")
    assert not notes.same_keyword("модель", "мода") and not notes.same_keyword("rag", "rage")

    text = notes.render(
        type="note", title="RAG: основы", description="Поиск — потом ответ", tags=("rag",), source="https://x.org",
        ideas=["Идея"], details="", related=[("Векторные базы", "векторные_базы.md")], keywords=("rag", "поиск"),
    )
    note = notes.parse("rag_основы.md", text)
    assert text.startswith('---\ntype: "note"\ntitle: "RAG: основы"')
    assert (note.type, note.title, note.tags, note.keywords) == ("note", "RAG: основы", ("rag",), ("rag", "поиск"))
    assert note.links == ("векторные_базы.md",)
    # Описание с « — » не ломает строку индекса.
    [entry] = notes.parse_index(notes.render_index([notes.entry_of(note)]))
    assert entry == notes.IndexEntry("rag_основы.md", "Поиск – потом ответ", ("rag", "поиск"))

    # Модель положила в подробности всю заметку: повторы шаблона выброшены, ключевые слова берутся из своего раздела.
    copied = "# RAG\n\n**Источник:** x\n\n## Ключевые мысли\n\n### Основы\n- Чанки\n\n## Ключевые слова\n\nлишнее, слово"
    assert notes.clean_details(copied) == "### Основы\n- Чанки"
    text = notes.render(type="note", title="T", description="D", tags=(), source="s", ideas=["И"], details=copied,
                        related=[], keywords=("rag", "чанки"))
    assert text.count("Ключевые слова") == 1 and notes.parse("t.md", text).keywords == ("rag", "чанки")

    meta, _ = notes.split_frontmatter("---\ntype: howto\ntags:\n  - a\n  - b\ntitle: Ручная\n---\n# X")
    assert meta == {"type": "howto", "tags": ["a", "b"], "title": "Ручная"}


def test_new_text_extends_note_with_two_shared_keywords(tmp_path):
    service = wiki(tmp_path)
    assert "Создана заметка rag_основы.md" in service.save_note(fields())
    index = (tmp_path / "wiki" / "index.md").read_text(encoding="utf-8")
    assert "- [rag_основы.md](rag_основы.md) — Как модель ищет куски документов перед ответом — rag, поиск, эмбеддинги" in index

    # Одно общее слово — это другая тема; два — тот же текст надо дописать в найденную заметку.
    assert service.match(["rag", "трансформер"]) == []
    [found] = service.match(["Эмбеддинг", "поиск", "чанки"])
    assert found.entry.path == "rag_основы.md" and found.shared == ("поиск", "эмбеддинги")
    with pytest.raises(ValueError, match="path=rag_основы.md"):
        service.save_note(fields(title="Чанки", keywords=("чанки", "поиск", "эмбеддинги")))

    service.save_note(fields(keywords=("rag", "поиск", "эмбеддинги", "чанки"), source="https://x.org"), path="rag_основы.md")
    assert [entry.keywords for entry in service.index()] == [("rag", "поиск", "эмбеддинги", "чанки")]
    assert "**Источник:** https://x.org" in service.read("rag_основы.md")
    # Прежняя версия пересобранной заметки сохранена.
    assert len(list((tmp_path / "wiki-history").glob("rag_основы.*.md"))) == 1
    # Абзац вместо описания сокращается до первой фразы: строка индекса остаётся короткой.
    long = "Как модель ищет куски документов. " + "Подробности " * 30
    service.save_note(NoteFields(**{**fields().__dict__, "description": long}), path="rag_основы.md")
    assert service.index()[0].description == "Как модель ищет куски документов."


def test_rename_and_merge_rewrite_links_and_index(tmp_path):
    service = wiki(tmp_path)
    service.save_note(fields(title="Векторные базы", keywords=("векторная база", "индекс")))
    service.save_note(fields(title="Векторные БД", keywords=("векторные базы", "faiss")))
    service.save_note(fields(title="RAG", keywords=("rag", "ответ")), )
    service.save_note(fields(title="RAG", keywords=("rag", "ответ"), related=("векторные_бд.md",)), path="rag.md")
    assert service.note("rag.md").links == ("векторные_бд.md",)

    message = service.save_note(
        fields(title="Векторные базы данных", keywords=("векторная база", "индекс", "faiss")),
        path="векторные_базы.md", replaces=("векторные_бд.md",),
    )
    assert "переименован из векторные_базы.md" in message and "слиты и удалены: векторные_бд.md" in message
    assert sorted(entry.path for entry in service.index()) == ["rag.md", "векторные_базы_данных.md"]
    assert service.note("rag.md").links == ("векторные_базы_данных.md",)
    assert not (tmp_path / "wiki" / "векторные_бд.md").exists()

    graph = service.graph()
    assert graph["edges"] == [{"from": "rag.md", "to": "векторные_базы_данных.md", "kind": "link"}]
    assert {node["path"]: (node["degree"], node["cluster"]) for node in graph["nodes"]} == {
        "rag.md": (1, 0), "векторные_базы_данных.md": (1, 0),
    }


def test_graph_links_notes_close_by_text_without_model(tmp_path):
    service = wiki(tmp_path)
    root = tmp_path / "wiki"
    root.mkdir()
    texts = {
        "тест_дизайн.md": "# Тест-дизайн\n\nГраничные значения и классы эквивалентности для тестирования форм.",
        "тест_кейсы.md": "# Тест-кейсы\n\nАгент пишет тест-кейсы: граничные значения, классы эквивалентности, шаги.",
        "ollama.md": "# Ollama\n\nЗапуск локальной модели, context length и выгрузка слоёв на видеокарту.",
        "lm_studio.md": "# LM Studio\n\nНастройки локальной модели: context length, выгрузка на видеокарту.",
        "рецепт.md": "# Яблочный пирог\n\nМука, сахар, яблоки, корица. Выпекать сорок минут.",
    }
    for name, text in texts.items():
        (root / name).write_text(text, encoding="utf-8")

    edges = {(edge["from"], edge["to"]): edge for edge in service.graph()["edges"]}
    assert set(edges) == {("тест_дизайн.md", "тест_кейсы.md"), ("lm_studio.md", "ollama.md")}
    assert all(edge["kind"] == "similar" and 0 < edge["score"] <= 1 for edge in edges.values())

    # Если заметки уже связаны ссылкой, пунктир её не дублирует.
    (root / "ollama.md").write_text(texts["ollama.md"] + "\n\nСм. [LM Studio](lm_studio.md).", encoding="utf-8")
    edges = {(edge["from"], edge["to"]): edge["kind"] for edge in service.graph()["edges"]}
    assert edges[("lm_studio.md", "ollama.md")] == "link"
    assert len(edges) == 2


def test_index_follows_manual_files_and_paths_are_checked(tmp_path):
    service = wiki(tmp_path)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "openrgb.md").write_text("# OpenRGB\n\nНастройка подсветки без sudo.\n", encoding="utf-8")
    [entry] = service.index()
    assert entry.path == "openrgb.md" and entry.description == "Настройка подсветки без sudo."
    [(path, title, _line, _text)] = service.search("подсветки")
    assert (path, title) == ("openrgb.md", "OpenRGB")

    (tmp_path / "wiki" / "openrgb.md").unlink()
    assert service.index() == [] and "openrgb" not in service.read("index.md")
    for bad in ("../secret.md", "/etc/x.md", "a/b.txt", "index.md", "_x.md"):
        with pytest.raises(ValueError):
            service.write(bad, "# X")
    assert notes.clusters(["a", "b", "c", "d", "e"], [("a", "b"), ("b", "c"), ("d", "e")]) == {
        "a": 0, "b": 0, "c": 0, "d": 1, "e": 1,
    }


def test_agent_adds_note_without_card_and_ui_reads_it(tmp_path):
    provider = ScriptedProvider(
        call("wiki_match", keywords="rag, поиск, эмбеддинги"),
        call("wiki_save_note", title="RAG: основы", description="Поиск кусков документов перед ответом",
             ideas="Модель сначала ищет, потом отвечает\nКуски, ищут по эмбеддингам", keywords=["rag", "поиск", "эмбеддинги"]),
        "Создал заметку",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        events = events_of(client.post(f"/api/sessions/{session['id']}/turns/stream", json={"content": "Добавь заметку: …"}))
        pages = client.get("/api/wiki").json()
        page = client.get("/api/wiki/page", params={"path": "rag_основы.md"}).json()
        found = client.get("/api/wiki/search", params={"q": "эмбеддингам"}).json()

    match, saved = [event["message"]["content"] for event in events if event["type"] == "tool_result"]
    assert "создай новую" in match.casefold() and "Создана заметка rag_основы.md" in saved
    assert events[-1]["type"] == "done"
    assert [item["path"] for item in pages] == ["rag_основы.md"]
    # Описание и теги — для поиска в интерфейсе без запроса к серверу.
    assert pages[0]["description"] == "Поиск кусков документов перед ответом" and pages[0]["tags"] == []
    assert page["meta"]["type"] == "note" and page["body"].lstrip().startswith("# RAG: основы")
    # Мысль с запятой осталась одной мыслью.
    assert "- Куски, ищут по эмбеддингам" in page["content"]
    assert {item["path"] for item in found} == {"rag_основы.md"}
    assert (tmp_path / "wiki" / "index.md").exists() and not list((tmp_path / "memory").glob("*rag*"))


def test_lint_applies_only_checked_edits(tmp_path):
    edits = [
        {"kind": "merge", "notes": ["a.md", "b.md"], "problem": "Дубли", "change": "Слить b в a"},
        {"kind": "outdated", "notes": ["b.md"], "problem": "Старая версия", "change": "Обновить версию"},
    ]
    provider = ScriptedProvider(call("wiki_lint"), call("wiki_propose_edits", edits=edits), "Применил")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        service = client.app.state.wiki_service
        service.save_note(fields(title="A", keywords=("rag", "поиск")))
        (tmp_path / "wiki" / "b.md").write_text("# B\n\nRAG v1\n\n## Ключевые слова\n\nrag, поиск\n", encoding="utf-8")
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "/lint"}))
        card = events[-1]
        resumed = events_of(client.post(url + "/resume", json={"action": "approve", "selected": [2]}))

    [material] = [event["message"]["content"] for event in events if event["type"] == "tool_result"]
    assert "a.md + b.md: rag, поиск" in material and "=== b.md ===" in material
    assert card["type"] == "interrupt" and card["pending"]["kind"] == "wiki_lint"
    assert [edit["kind"] for edit in card["pending"]["edits"]] == ["merge", "outdated"]
    [result] = [event["message"]["content"] for event in resumed if event["type"] == "tool_result"]
    assert '2. устарело: Обновить версию\n   Вызови wiki_save_note с path="b.md"' in result and "Слить" not in result
    assert "Правки 1 не подтверждены" in result and "=== b.md ===\n# B" in result


def test_wiki_chat_lives_outside_projects_and_forgets_old_turns(tmp_path):
    provider = ScriptedProvider("Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        chat = post_session(client, {"model": "test"}).json()
        empty = client.get("/api/wiki/chat").json()
        wiki_id = client.post("/api/wiki/chat", json={"provider": "lm_studio", "model": "test"}).json()["session"]["id"]
        for number in range(1, 5):
            events = events_of(client.post(f"/api/sessions/{wiki_id}/turns/stream", json={"content": f"Вопрос {number}"}))
            assert events[-1]["type"] == "done"
        state = client.get("/api/wiki/chat").json()
        sessions = client.get("/api/sessions", params={"include_hidden": True}).json()
        # Модель чата вики следует за выбором в интерфейсе.
        same = client.post("/api/wiki/chat", json={"model": "other"}).json()["session"]
        cleared = client.delete("/api/wiki/chat")
        after = client.get("/api/wiki/chat").json()
        gone = client.get(f"/api/sessions/{wiki_id}")

    assert empty == {"session": None, "running": False, "memory": 3}
    # Помнит только три последние реплики пользователя с ответами; «Вопрос 1» уже забыт.
    request = provider.requests[-1]
    assert [message.content for message in request["messages"] if message.role == "user"] == ["Вопрос 2", "Вопрос 3", "Вопрос 4"]
    assert "личную вики" in request["messages"][0].content
    tools = {tool["function"]["name"] for tool in request["tools"]}
    assert {"wiki_match", "wiki_save_note", "wiki_lint", "fetch_url"} <= tools
    assert not tools & {"remember", "activate", "delegate", "propose"}
    assert state["session"]["id"] == wiki_id and state["session"]["project_id"] is None
    assert state["session"]["title"] == "Чат вики"
    assert [item["id"] for item in sessions] == [chat["id"]]
    assert same["id"] == wiki_id and same["model"] == "other"
    assert cleared.status_code == 204 and after["session"] is None and gone.status_code == 404


def test_lint_in_wiki_chat_card_stays_out_of_the_attention_panel(tmp_path):
    edits = [{"kind": "fix", "notes": ["a.md"], "problem": "Опечатка", "change": "Исправить"}]
    provider = ScriptedProvider(call("wiki_lint"), call("wiki_propose_edits", edits=edits), "Готово")
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        client.app.state.wiki_service.save_note(fields(title="A", keywords=("rag", "поиск")))
        wiki_id = client.post("/api/wiki/chat", json={"model": "test"}).json()["session"]["id"]
        events = events_of(client.post(f"/api/sessions/{wiki_id}/turns/stream", json={"content": "/lint — проверь вики"}))
        attention = client.get("/api/attention").json()
        pending = client.get("/api/wiki/chat").json()["session"]["pending"]
        resumed = events_of(client.post(f"/api/sessions/{wiki_id}/resume", json={"action": "approve", "selected": [1]}))

    assert events[-1]["type"] == "interrupt" and pending["kind"] == "wiki_lint"
    assert attention["cards"] == [] and attention["running"] == []
    assert resumed[-1]["type"] == "done"


def test_summary_put_into_description_is_kept_in_details(tmp_path):
    """Модель положила весь конспект в описание, а мысли — одной строкой с буквальными «\\n»."""
    service = wiki(tmp_path)
    summary = (
        "### Введение в плейбук\nЭтот плейбук — справочник по тестированию. Он охватывает всё.\n\n"
        "### Классификация\n- Модульное, интеграционное, системное\n- Пирамида: Unit 70%, E2E 10%"
    )
    service.save_note(NoteFields(
        title="Плейбук тестировщика", description=summary,
        ideas=("Классификация тестирования\\nТехники тест-дизайна (EP, BVA)\\n- Баг-репорты",),
        keywords=("тестирование", "тест-дизайн"),
    ))
    text = service.read("плейбук_тестировщика.md")
    note = service.note("плейбук_тестировщика.md")

    assert note.description == "Этот плейбук — справочник по тестированию."
    assert "## Подробности" in text and "Пирамида: Unit 70%, E2E 10%" in text and "\n### Классификация" in text
    assert "- Классификация тестирования\n- Техники тест-дизайна (EP, BVA)\n- Баг-репорты\n" in text
    assert "\\n" not in text


def test_tags_find_notes_and_variants_of_one_word_merge(tmp_path):
    import asyncio

    from local_agent.wiki.tools import WikiLintTool, WikiMatchTool

    service = wiki(tmp_path)
    service.save_note(fields(tags=("тестирование", "методологии")))
    # Файл, поправленный руками: тот же тег в другой форме.
    (tmp_path / "wiki" / "bdd.md").write_text(
        '---\ntitle: "BDD"\ndescription: "Сценарии на Gherkin"\ntags: ["Методология", "qa"]\n---\n\n# BDD\n',
        encoding="utf-8",
    )
    pages = {page.path: page for page in service.pages()}
    # Список для интерфейса: описание и теги; «Методология» и «методологии» — один тег.
    assert pages["bdd.md"].description == "Сценарии на Gherkin"
    assert pages["bdd.md"].tags == ("методологии", "qa")
    assert service.tags() == [("методологии", 2), ("qa", 1), ("тестирование", 1)]
    # Новая заметка с вариантом существующего тега получает его, а не заводит ещё один.
    service.save_note(fields(title="Чек-лист", keywords=("чек-лист", "релиз"), tags=("Методология", "чек-листы")))
    assert notes.parse("x.md", service.read("чек-лист.md")).tags == ("методологии", "чек-листы")

    # Агент видит теги вики, когда подбирает заметку, и при проверке — чтобы слить синонимы.
    matched = asyncio.run(WikiMatchTool(service).execute({"keywords": ["gherkin", "сценарии"]}, None)).content
    assert "Теги вики (сколько заметок): методологии 3, qa 1" in matched
    lint = asyncio.run(WikiLintTool(service).execute({}, None)).content
    assert "Теги (сколько заметок): методологии 3" in lint and "синонимы" in lint
