"""Генерация тест-кейсов по документации: разбор страниц, нарезка, запросы к модели, запись, продолжение и инструмент."""

import asyncio
import json
from datetime import UTC, datetime

from local_agent.agent.models import Agent
from local_agent.agent.tool_executor import ToolExecutor
from local_agent.llm.base import LLMProviderError, LLMProviderUnavailable
from local_agent.llm.models import ChatResult
from local_agent.sessions.models import Session
from local_agent.testgen.pipeline import CaseGenerator, chunks, parse_page, system_prompt
from local_agent.testgen.style import lint
from local_agent.tools.registry import ToolRegistry
from local_agent.tools.testgen import GenerateTestCasesTool

BODY = "## Настройка\n\n" + "Откройте меню «Сервер» и задайте адрес. " * 20

CASE = {
    "title": "Настройка адреса сервера",
    "component": "EMS",
    "priority": "High",
    "type": "Позитивный",
    "preconditions": ["EMS запущен."],
    "steps": [{"action": "Откройте меню «Сервер».", "test_data": "IP = 10.0.0.1 | 24", "expected": "Открывается вкладка."}],
    "source_section": "Настройка",
}


class FakeProvider:
    """Отвечает заготовленными ответами по очереди; ответ-исключение выбрасывается."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, model, messages, tools=None, response_format=None, options=None):
        self.calls.append({"model": model, "messages": messages, "response_format": response_format, "options": options})
        reply = self.replies.pop(0) if self.replies else {"test_cases": [CASE]}
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(content=reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False))


def page(root, relative, text, title="Страница", source="https://docs/pages/viewpage.action?pageId=42"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ntitle: "{title}"\npath: "Документация > {title}"\nsource: "{source}"\n---\n{text}', encoding="utf-8")
    return path


def run(generator, start, **kwargs):
    return asyncio.run(generator.run(start, **kwargs))


def test_parse_page_drops_images_links_and_child_pages(tmp_path):
    text = (
        "# Заголовок\n\nТекст ![схема](img.png) раздела.\n\n- [Ссылка](other.md)\n\nЕщё текст.\n"
        "\n---\n\n### Дочерние страницы\n\n- [Дочь](child/index.md)\n"
    )
    parsed = parse_page(page(tmp_path, "docs/a/index.md", text, title="Инициализация"), tmp_path)

    assert parsed.title == "Инициализация" and parsed.location == "Документация > Инициализация"
    assert parsed.page_id == "42" and parsed.relative == "docs/a/index.md"
    assert parsed.text == "Текст  раздела.\n\nЕщё текст."


def test_parse_page_without_front_matter_uses_heading_and_hash_id(tmp_path):
    (tmp_path / "guide.md").write_text("# Руководство\n\nТекст.\n", encoding="utf-8")

    parsed = parse_page(tmp_path / "guide.md", tmp_path)

    assert parsed.title == "Руководство" and parsed.location == "guide.md"
    assert len(parsed.page_id) == 8 and parsed.text == "Текст."


def test_chunks_split_by_headings_and_keep_all_text():
    text = "".join(f"## Раздел {n}\n\n" + "слово " * 300 + "\n\n" for n in range(6)) + "## Огромный\n\n" + ("абзац " * 200 + "\n\n") * 10

    parts = chunks(text, 4000)

    assert len(parts) > 3 and all(len(part) <= 4000 for part in parts)
    assert "".join(parts) == text


def test_lint_checks_verb_length_and_banned_words():
    case = {"steps": [
        {"action": "Нажатие кнопки «Сохранить».", "expected": "Настройки успешно применены."},
        {"action": "Если поле пустое, введите тестовые данные.", "expected": " ".join(["слово"] * 21) + "."},
        {"action": "Убедитесь, что данные сохранены.", "expected": "Открывается окно."},
    ]}

    issues = lint([case])

    assert any("шаг 1: шаг не начинается с глагола" in issue for issue in issues)
    assert any("шаг 1: в expected запрещённое слово «успешн…»" in issue for issue in issues)
    assert any("шаг 2: в expected предложение длиннее 20 слов" in issue for issue in issues)
    assert not any("шаг 3" in issue for issue in issues)


def test_generator_writes_pages_in_batches_and_resumes(tmp_path):
    page(tmp_path, "docs/a/index.md", BODY, title="Первая")
    page(tmp_path, "docs/a/short/index.md", "Мало текста.", title="Короткая")
    page(tmp_path, "docs/b/index.md", BODY, title="Вторая", source="")
    provider = FakeProvider()
    generator = CaseGenerator(provider, "gemma", tmp_path)

    first = run(generator, tmp_path / "docs", limit=1)
    second = run(generator, tmp_path / "docs", limit=5)
    third = run(generator, tmp_path / "docs")

    out = (tmp_path / "testgen/test_cases/docs/a/index.md").read_text(encoding="utf-8")
    assert "## TC-42-01. Настройка адреса сервера" in out and "IP = 10.0.0.1 \\| 24" in out
    assert [p.output for p in first.pages] == ["testgen/test_cases/docs/a/index.md"] and first.remaining == 1
    # Короткая страница пропускается и в лимит не входит.
    assert [p.skipped for p in second.pages] == ["мало текста", ""] and second.remaining == 0
    assert [p.skipped for p in third.pages] == ["мало текста"] and third.total == 3
    assert len(provider.calls) == 2
    call = provider.calls[0]
    assert call["model"] == "gemma" and call["response_format"]["json_schema"]["strict"] is True
    assert call["options"] == {"temperature": 0.2, "reasoning_effort": "none"}


def test_system_prompt_takes_project_rules_and_example(tmp_path):
    (tmp_path / "testgen").mkdir()
    (tmp_path / "testgen/rules.md").write_text("Продукт: Wi-Fi контроллер SoftWLC.", encoding="utf-8")
    (tmp_path / "testgen/example.json").write_text(json.dumps({"title": "Создание домена"}), encoding="utf-8")

    prompt = system_prompt(tmp_path)

    assert "Правила проекта:\nПродукт: Wi-Fi контроллер SoftWLC." in prompt
    assert "Создание домена" in prompt and "Вход в веб-интерфейс с неверным паролем" not in prompt


def test_empty_answer_is_retried_and_rejected_option_is_dropped(tmp_path):
    page(tmp_path, "docs/a.md", BODY)
    provider = FakeProvider("", LLMProviderError("Сервер вернул ошибку HTTP 400: unknown field"), {"test_cases": [CASE]})

    result = run(CaseGenerator(provider, "gemma", tmp_path), tmp_path / "docs")

    assert result.pages[0].output and not result.pages[0].errors
    # Пустой ответ не отключает reasoning_effort, отказ сервера с HTTP 400 — отключает.
    assert [call["options"].get("reasoning_effort") for call in provider.calls] == ["none", "none", None]


def test_page_with_failed_chunk_is_not_written_and_stays_pending(tmp_path):
    page(tmp_path, "docs/a.md", BODY)
    provider = FakeProvider("не json", "снова не json", "и опять")

    result = run(CaseGenerator(provider, "gemma", tmp_path), tmp_path / "docs")

    assert result.pages[0].errors and not result.pages[0].output
    assert result.remaining == 1 and not (tmp_path / "testgen/test_cases/docs/a.md").exists()


def test_tool_validates_arguments_and_reports_batch(tmp_path):
    page(tmp_path, "docs/a.md", BODY)
    unbound = asyncio.run(GenerateTestCasesTool().execute({"path": "docs"}, tmp_path))
    tool = GenerateTestCasesTool().bind(FakeProvider(), "gemma")

    outside = asyncio.run(tool.execute({"path": "../"}, tmp_path))
    bad_limit = asyncio.run(tool.execute({"path": "docs", "limit": 0}, tmp_path))
    done = asyncio.run(tool.execute({"path": "docs"}, tmp_path))

    assert unbound.is_error and "Модель" in unbound.content
    assert outside.is_error and bad_limit.is_error
    assert not done.is_error and "testgen/test_cases/docs/a.md: кейсов 1" in done.content
    assert GenerateTestCasesTool.requires_approval is True


def test_tool_keeps_finished_pages_when_server_goes_down(tmp_path):
    page(tmp_path, "docs/a.md", BODY)
    page(tmp_path, "docs/b.md", BODY)
    tool = GenerateTestCasesTool().bind(FakeProvider({"test_cases": [CASE]}, LLMProviderUnavailable("Сервер недоступен")), "m")

    result = asyncio.run(tool.execute({"path": "docs", "limit": 2}, tmp_path))

    assert result.is_error and "продолжит с места остановки" in result.content
    assert (tmp_path / "testgen/test_cases/docs/a.md").exists()


def test_executor_binds_session_model_to_tools(tmp_path):
    now = datetime.now(UTC)
    session = Session(
        id="s", title="t", created_at=now, updated_at=now, agent_id="qa", model="gemma", provider="p",
        workspace=str(tmp_path), enabled_tools=["generate_test_cases"],
    )
    agent = Agent(id="qa", name="QA", system_prompt="-", llm_provider="p", model="", tools=("generate_test_cases",))
    provider = FakeProvider()

    executor = ToolExecutor.for_session(
        ToolRegistry([GenerateTestCasesTool()]), agent, session, "r", llm=(provider, "gemma"),
    )

    tool = executor.get("generate_test_cases")
    assert tool._provider is provider and tool._model == "gemma"
