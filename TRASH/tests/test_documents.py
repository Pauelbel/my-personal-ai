"""Большие документы: оглавление и чтение кусками, огромная вставка в чат — файлом, шаги и инструмент «по частям»
с продолжением после остановки, коннектор «Документ → Markdown»."""

import asyncio
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of
from tests.test_graph_flow import project_session, settings

from api.app import create_app
from connectors.scripts import LIBRARY, _list, library_tools
from llm.base import LLMProviderError
from shared.document import outline, split_parts, tokens
from steps.engine import PARTS_DIR, RunContext, StepRunner, Toolkit
from steps.files import parse_steps
from tools.documents import PROCESS_WORKFLOW
from tools.files import ReadFileTool, SearchFilesTool

SECTION = "Пользователь вводит логин и пароль, система проверяет их и открывает личный кабинет. "


def document(sections: int = 3, paragraphs: int = 4) -> str:
    """Документ с разделами «## Раздел N» по нескольку абзацев."""
    parts = ["# Спецификация"]
    for number in range(1, sections + 1):
        parts.append(f"## Раздел {number}")
        parts += [f"Абзац {number}.{index}. " + SECTION * 3 for index in range(1, paragraphs + 1)]
    return "\n\n".join(parts) + "\n"


# ---------- оглавление и части ----------

def test_outline_lists_headings_with_line_numbers_outside_code():
    text = "# Глава\n\n```\n# не заголовок\n```\n## Вход\nтекст\n### Пароль\n"
    assert outline(text) == "строка 1: Глава\n  строка 6: Вход\n    строка 8: Пароль"
    many = "\n".join(f"## Раздел {index}\n### Пункт {index}" for index in range(100))
    short = outline(many, limit=150)
    assert "Пункт" not in short and "показаны верхние уровни: 100 из 200" in short


def test_parts_fit_the_limit_cover_every_line_and_start_at_headings():
    text = document(sections=6)
    parts = split_parts(text, 600)
    assert len(parts) > 3 and all(tokens(part.text) <= 600 for part in parts)
    # Ни одна строка не потерялась и не повторилась: части идут подряд.
    assert parts[0].start == 1 and parts[-1].end == len(text.splitlines())
    assert all(later.start > earlier.end for earlier, later in zip(parts, parts[1:], strict=False))
    joined = "\n".join(part.text for part in parts)
    assert all(f"Абзац {number}.4." in joined for number in range(1, 7))
    # Граница — перед заголовком раздела, если он есть во второй половине части; раздел виден в названии части.
    assert any(part.text.startswith("## Раздел") for part in parts[1:])
    assert parts[-1].title.startswith("Спецификация › Раздел")


def test_one_huge_line_is_cut_into_pieces():
    parts = split_parts("слово " * 20_000, 500)
    assert len(parts) > 10 and all(tokens(part.text) <= 500 for part in parts)
    assert all(part.start == 1 for part in parts)


# ---------- read_file и search_files ----------

def test_big_file_is_read_as_outline_then_by_lines(tmp_path):
    lines = ["# Требования"] + [f"## Раздел {index}\n" + "строка требования " * 40 for index in range(200)]
    (tmp_path / "spec.md").write_text("\n".join(lines), encoding="utf-8")
    read = ReadFileTool()

    overview = asyncio.run(read.execute({"path": "spec.md"}, tmp_path))
    assert not overview.is_error and "целиком в окно модели не помещается" in overview.content
    assert "строка 1: Требования" in overview.content and "  строка 2: Раздел 0" in overview.content
    assert "… и ещё 51 заголовков, следующий — на строке 300" in overview.content
    assert "offset" in overview.content and "process_document" in overview.content

    piece = asyncio.run(read.execute({"path": "spec.md", "offset": 4, "limit": 2}, tmp_path))
    assert piece.content.startswith("spec.md, строки 4–5 из 401:\n## Раздел 1")
    assert piece.content.endswith("(дальше — read_file с offset 6)")
    tail = asyncio.run(read.execute({"path": "spec.md", "offset": 400}, tmp_path))
    assert "строки 400–401 из 401" in tail.content and "дальше" not in tail.content
    beyond = asyncio.run(read.execute({"path": "spec.md", "offset": 1000}, tmp_path))
    assert beyond.is_error and "401 строк" in beyond.content
    # Маленький файл по-прежнему читается целиком, без заголовка.
    (tmp_path / "small.md").write_text("коротко", encoding="utf-8")
    assert asyncio.run(read.execute({"path": "small.md"}, tmp_path)).content == "коротко"


def test_search_finds_text_in_big_file_and_names_skipped_ones(tmp_path):
    big = "строка без нужного\n" * 120_000 + "Пароль хранится в хэше\n"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "all.md").write_text(big, encoding="utf-8")
    (tmp_path / "docs" / "page.md").write_text("пароль — не короче 8 символов\n", encoding="utf-8")
    search = SearchFilesTool()

    folder = asyncio.run(search.execute({"query": "пароль", "path": "docs"}, tmp_path))
    assert "docs/page.md:1:" in folder.content and "пропущены: docs/all.md" in folder.content
    single = asyncio.run(search.execute({"query": "пароль", "path": "docs/all.md"}, tmp_path))
    assert single.content == "docs/all.md:120001: Пароль хранится в хэше"


# ---------- огромная вставка в чат ----------

def test_huge_paste_goes_to_a_project_file_and_the_model_gets_its_ends(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    pasted = "Напиши тест-кейсы по тексту.\n" + document(sections=40) + "Конец требований."
    provider = ScriptedProvider("Прочитал")
    # Окно с запасом: реплика доходит до модели такой, как сохранена, без сокращения середины.
    roomy = settings(tmp_path).model_copy(update={"default_context_tokens": 32768})
    with TestClient(create_app(roomy, llm_provider=provider)) as client:
        session = project_session(client, folder)
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": pasted}))
        history = client.get(url + "/messages").json()

    [saved] = list((folder / ".meepo" / "inbox").iterdir())
    assert saved.read_text(encoding="utf-8") == pasted
    stored = history[0]["content"]
    assert len(stored) < 3000 and stored.startswith("Напиши тест-кейсы") and "Конец требований." in stored
    assert f".meepo/inbox/{saved.name}" in stored and "process_document" in stored
    asked = provider.requests[0]["messages"][-1].content
    assert asked == stored
    assert any("Большой текст сохранён в файл" in event.get("text", "") for event in events if event["type"] == "log")


# ---------- шаги по частям ----------

def test_parts_step_is_parsed_and_checked():
    steps = parse_steps("## Нужно от вас\n- документ — файл\n\n## Требования\nмодель: Выпиши требования.\n"
                        "по частям: {документ}\nсохранить: out/req.md\n")
    assert steps.steps[0].parts == "{документ}" and steps.view()["steps"][0]["parts"] == "{документ}"
    assert any(effect["code"] == "{документ}" for effect in steps.effects())
    with pytest.raises(ValueError, match="куда его записать"):
        parse_steps("## Нужно от вас\n- документ — файл\n\n## Т\nмодель: x\nпо частям: {документ}\n")
    with pytest.raises(ValueError, match="только у шага-модели"):
        parse_steps("## Нужно от вас\n- документ — файл\n\n## Т\nкоманда: ls\nпо частям: {документ}\nсохранить: a.md\n")


class FlakyProvider(ScriptedProvider):
    """Как ScriptedProvider, но исключение в сценарии — сбой сервера модели."""

    async def chat(self, model, messages, tools=None, response_format=None, options=None):
        if self.steps and isinstance(self.steps[0], Exception):
            self.requests.append({"model": model, "messages": list(messages), "tools": tools})
            raise self.steps.pop(0)
        return await super().chat(model, messages, tools, response_format, options)


def run_parts(provider, folder, window=3000):
    runner = StepRunner(
        PROCESS_WORKFLOW, {"документ": "docs/spec.md", "задание": "Выпиши требования", "сохранить": "out/req.md"},
        RunContext(provider, "test", folder, window, Toolkit(tool=lambda name: None, tools=list)),
    )
    return asyncio.run(runner.run())


def test_document_is_processed_part_by_part_and_resumes_after_a_failure(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text(document(sections=3), encoding="utf-8")
    first = FlakyProvider("Требование A", LLMProviderError("сервер модели упал"))
    snapshot = run_parts(first, tmp_path)

    assert snapshot["status"] == "failed" and "Модель не ответила" in snapshot["error"]
    assert (tmp_path / "out" / "req.md").read_text(encoding="utf-8") == "Требование A\n"
    [work] = list((tmp_path / PARTS_DIR).iterdir())
    task = first.requests[0]["messages"][1].content
    assert "часть 1 из 3" in task and "Абзац 1.1." in task and "Абзац 2.1." not in task
    assert task.index("Абзац 1.1.") < task.index("Задание:\nВыпиши требования") < task.index("ответь одним символом «—»")
    # Поля, которые уже стоят в задании и путях, отдельным списком не повторяются.
    assert "Данные от пользователя" not in task

    second = ScriptedProvider("—", "Требование C")
    snapshot = run_parts(second, tmp_path)
    assert snapshot["status"] == "done" and len(second.requests) == 2
    assert "часть 2 из 3" in second.requests[0]["messages"][1].content
    assert (tmp_path / "out" / "req.md").read_text(encoding="utf-8") == "Требование A\n\nТребование C\n"
    step = snapshot["steps"][0]
    assert step["files"] == ["out/req.md"] and "3 части, результат есть по 2 из них" in step["output"]
    assert not work.exists()


def test_process_document_runs_from_card_with_edited_fields(tmp_path):
    folder = tmp_path / "project"
    (folder / "docs").mkdir(parents=True)
    (folder / "docs" / "spec.md").write_text(document(sections=3), encoding="utf-8")
    provider = ScriptedProvider(
        call("process_document", path="docs/spec.md", task="Выпиши требования"),
        "Р1", "Р2", "Р3", "Готово: требования в docs/req.md",
    )
    # Окно 3000: каждая часть — один раздел документа.
    small = settings(tmp_path).model_copy(update={"default_context_tokens": 3000})
    with TestClient(create_app(small, llm_provider=provider)) as client:
        session = project_session(client, folder)
        url = f"/api/sessions/{session['id']}"
        card = events_of(client.post(url + "/turns/stream", json={"content": "Выпиши требования из docs/spec.md"}))[-1]
        events = events_of(client.post(url + "/resume", json={"action": "approve", "inputs": {"сохранить": "docs/req.md"}}))

    pending = card["pending"]
    assert pending["kind"] == "steps" and pending["title"] == "Обработать по частям: docs/spec.md"
    run = pending["run"]
    assert run["values"] == {"документ": "docs/spec.md", "задание": "Выпиши требования", "сохранить": "docs/spec-result.md"}
    assert "около 3 частей" in run["description"] and run["problems"] == []
    assert (folder / "docs" / "req.md").read_text(encoding="utf-8") == "Р1\n\nР2\n\nР3\n"
    assert not (folder / "docs" / "spec-result.md").exists()
    [result] = [event["message"] for event in events if event["type"] == "tool_result"]
    assert not result["is_error"] and result["meta"]["steps"]["workflow"] == "process_document"
    assert "записано в docs/req.md" in result["content"] and "заново не обрабатывай" in result["content"]
    progress = [event["run"]["steps"][0]["detail"] for event in events if event["type"] == "steps"]
    assert any(detail.startswith("часть 2 из 3") for detail in progress)
    assert events[-1]["type"] == "done"


# ---------- план: пункт текстом ----------

def test_plan_done_accepts_the_step_text_instead_of_its_number(tmp_path):
    provider = ScriptedProvider(
        call("plan", action="set", steps=["Тест-кейсы", "Автотесты"]),
        call("plan", action="done", step="Тест-кейсы", note="в cases.md"),
        call("plan", action="done", step="2"),
        "Готово",
    )
    folder = tmp_path / "project"
    folder.mkdir()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = project_session(client, folder)
        url = f"/api/sessions/{session['id']}"
        events = events_of(client.post(url + "/turns/stream", json={"content": "Тест-кейсы и автотесты"}))

    results = [event["message"] for event in events if event["type"] == "tool_result"]
    assert [result["is_error"] for result in results] == [False, False, False]
    assert results[1]["meta"]["stage"] == 1 and results[2]["meta"]["stage"] == 2


# ---------- коннектор «Документ → Markdown» ----------

def test_to_markdown_connector_declares_markitdown_with_extras():
    tool = {tool.id: tool for tool in library_tools()}["to_markdown"]
    assert tool.requires == ("markitdown[pdf,docx,xlsx,pptx]",) and tool.requires_approval and not tool.network
    assert _list("markitdown[pdf,docx], lxml>=5") == ["markitdown[pdf,docx]", "lxml>=5"]


def load_converter():
    spec = importlib.util.spec_from_file_location("to_markdown_script", LIBRARY / "to_markdown" / "script.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def convert(module, monkeypatch, root, arguments) -> tuple[int, str]:
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(arguments)))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    return module.main(), output.getvalue()


def test_converter_writes_markdown_next_to_documents_and_keeps_foreign_files(tmp_path, monkeypatch):
    class FakeMarkItDown:
        def __init__(self, enable_plugins):
            pass

        def convert_local(self, path):
            if path.endswith(".pptx"):
                raise ValueError("битый файл")
            return types.SimpleNamespace(markdown=f"# {path.rsplit('/', 1)[-1]}\n\nтекст")

    monkeypatch.setitem(sys.modules, "markitdown", types.SimpleNamespace(MarkItDown=FakeMarkItDown))
    module = load_converter()
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    for name in ("spec.pdf", "spec.docx", "sub/table.xlsx", "deck.pptx"):
        (docs / name).write_bytes(b"x")
    (docs / "spec.md").write_text("# Мой конспект\n", encoding="utf-8")

    code, report = convert(module, monkeypatch, tmp_path, {"path": "docs"})
    assert code == 0
    # Чужой spec.md не тронут: перевод — рядом под полным именем; spec.docx не перетирает перевод spec.pdf.
    assert (docs / "spec.md").read_text(encoding="utf-8") == "# Мой конспект\n"
    assert (docs / "spec.pdf.md").read_text(encoding="utf-8").startswith("<!-- Markdown из spec.pdf")
    assert (docs / "spec.docx.md").exists() and (docs / "sub" / "table.md").exists()
    assert "docs/deck.pptx: не переведён — ValueError: битый файл" in report
    again = convert(module, monkeypatch, tmp_path, {"path": "docs"})[1]
    assert "docs/sub/table.xlsx: перевод docs/sub/table.md уже свежий — пропущен" in again

    code, single = convert(module, monkeypatch, tmp_path, {"path": "docs/sub/table.xlsx", "save": "out/t.md"})
    assert code == 0 and "docs/sub/table.xlsx → out/t.md" in single
    with pytest.raises(module.Problem, match="за пределы"):
        convert(module, monkeypatch, tmp_path, {"path": "../x.pdf"})
    with pytest.raises(module.Problem, match="не переводятся"):
        convert(module, monkeypatch, tmp_path, {"path": "docs/spec.md"})


def test_connector_missing_packages_are_shown_and_install_checks_names(tmp_path):
    with TestClient(create_app(settings(tmp_path), llm_provider=ScriptedProvider())) as client:
        assert client.post("/api/capabilities/library/tool/to_markdown").status_code == 200
        tools = {tool["id"]: tool for tool in client.get("/api/capabilities").json()["tools"]}
        bad = client.post("/api/capabilities/packages", json={"requirements": ["https://evil.example/x.whl"]})

    assert tools["to_markdown"]["missing"] == ["markitdown[pdf,docx,xlsx,pptx]"]
    assert bad.status_code == 400 and "не имена пакетов" in bad.json()["detail"]



def test_packages_needed_by_a_connector_include_dependencies_of_its_extras(tmp_path):
    from connectors.pyenv import PythonEnv

    site = tmp_path / "env" / "lib" / "python3.14" / "site-packages"
    for name, requires in {
        "Main-Lib": ["helper", 'pdf-reader>=1; extra == "pdf"', "xls-reader; extra == 'xls'"],
        "helper": ["deep_dep"], "pdf_reader": [], "xls-reader": [], "deep.dep": [], "unrelated": [],
    }.items():
        info = site / f"{name}-1.0.dist-info"
        info.mkdir(parents=True)
        lines = [f"Name: {name}", "Version: 1.0", *(f"Requires-Dist: {spec}" for spec in requires)]
        (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = PythonEnv(tmp_path / "env")

    assert env.needed(["main-lib[pdf]"]) == {"main-lib", "helper", "deep-dep", "pdf-reader"}
    assert env.needed(["Main_Lib"]) == {"main-lib", "helper", "deep-dep"}


def test_sandbox_env_given_by_relative_path_works_from_project_folder(tmp_path, monkeypatch):
    import os

    from connectors.pyenv import PythonEnv
    from connectors.sandbox import SandboxPolicy, run_script

    (tmp_path / "data" / "sandbox-env" / "bin").mkdir(parents=True)
    # Как у настоящего окружения: bin/python — ссылка на системный интерпретатор (домашняя папка в песочнице скрыта).
    os.symlink(os.path.realpath(sys.executable), tmp_path / "data" / "sandbox-env" / "bin" / "python")
    (tmp_path / "project").mkdir()
    script = tmp_path / "show.py"
    script.write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Как в .env: SANDBOX_ENV_PATH не задан, data/ — относительный путь; скрипт запускается в папке проекта.
    env = PythonEnv(Path("data/sandbox-env"))
    result = asyncio.run(run_script(script, {}, tmp_path / "project", SandboxPolicy(30), env.python))

    assert not result.is_error, result.content
    assert result.content == str(tmp_path / "data" / "sandbox-env" / "bin" / "python")
