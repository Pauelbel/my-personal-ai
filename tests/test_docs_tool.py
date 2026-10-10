"""Поиск по документации: нарезка Markdown, русские словоформы, обновление индекса и границы рабочей папки."""

import asyncio
import os

import pytest

from local_agent.tools.docs import CHUNK_CHARS, SearchDocsTool, _chunks


def search(tool, workspace, **arguments):
    return asyncio.run(tool.execute(arguments, workspace))


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    (root / "docs" / "api").mkdir(parents=True)
    (root / "docs" / "auth.md").write_text(
        "# Вход\n\nОбщее описание.\n\n## Ошибки авторизации\n\nПосле пяти неверных паролей учётная запись блокируется.\n",
        encoding="utf-8",
    )
    (root / "docs" / "api" / "orders.md").write_text(
        "# Заказы\n\nПоле `amount` принимает значения от 1 до 100000.\n", encoding="utf-8",
    )
    return root


@pytest.fixture
def tool(tmp_path):
    return SearchDocsTool(tmp_path / "index")


def test_finds_section_by_other_word_form_with_heading_path(tool, workspace):
    result = search(tool, workspace, query="авторизация")

    assert not result.is_error
    assert "docs/auth.md:7 — Вход › Ошибки авторизации" in result.content
    assert "блокируется" in result.content
    assert "Общее описание" not in result.content


def test_path_limits_search_to_folder(tool, workspace):
    everywhere = search(tool, workspace, query="заказы вход")
    in_api = search(tool, workspace, query="заказы вход", path="docs/api")

    assert "auth.md" in everywhere.content and "orders.md" in everywhere.content
    assert "orders.md" in in_api.content and "auth.md" not in in_api.content


def test_index_follows_changed_and_deleted_files(tool, workspace):
    search(tool, workspace, query="заказы")
    orders = workspace / "docs" / "api" / "orders.md"
    orders.write_text("# Возвраты\n\nВозврат доступен 14 дней.\n", encoding="utf-8")
    # Та же секунда и тот же размер не должны скрыть правку: сравниваются наносекунды.
    os.utime(orders, ns=(orders.stat().st_atime_ns, orders.stat().st_mtime_ns + 1))
    (workspace / "docs" / "auth.md").unlink()

    assert "Возврат доступен" in search(tool, workspace, query="возврат").content
    assert "Ничего не найдено" in search(tool, workspace, query="заказы").content
    assert "Ничего не найдено" in search(tool, workspace, query="пароль").content


def test_two_letter_words_are_found_and_rank_by_matched_words(tool, workspace):
    (workspace / "docs" / "Шпаргалка по uv.md").write_text(
        "# Шпаргалка по uv\n\n- `uv sync` — восстановить окружение Python.\n", encoding="utf-8",
    )
    (workspace / "docs" / "python.md").write_text("# Python\n\nВерсии Python и окружение.\n", encoding="utf-8")

    only_short = search(tool, workspace, query="uv")
    mixed = search(tool, workspace, query="окружение uv")

    assert "Шпаргалка по uv.md" in only_short.content and "python.md" not in only_short.content
    # Раздел, где нашлись оба слова, выше раздела только с одним.
    assert mixed.content.index("Шпаргалка по uv.md") < mixed.content.index("python.md")


@pytest.mark.parametrize("query", ['"amount', "amount* OR -NEAR(", "поле: amount AND"])
def test_fts_syntax_in_query_is_treated_as_text(tool, workspace, query):
    result = search(tool, workspace, query=query)

    assert not result.is_error
    assert "orders.md" in result.content


@pytest.mark.parametrize(("arguments", "message"), [
    ({"query": "и"}, "от двух букв"),
    ({"query": "заказы", "path": "../"}, "за пределы"),
    ({"query": "заказы", "limit": 0}, "limit"),
])
def test_invalid_arguments_are_reported(tool, workspace, arguments, message):
    result = search(tool, workspace, **arguments)

    assert result.is_error and message in result.content


def test_index_is_stored_outside_workspace(tool, workspace, tmp_path):
    search(tool, workspace, query="заказы")

    assert list((tmp_path / "index").glob("*.sqlite"))
    assert not list(workspace.rglob("*.sqlite"))


def test_chunks_ignore_headings_in_code_and_split_long_sections():
    paragraph = "слово " * 100
    text = "# Раздел\n\n```\n# не заголовок\n```\n\n" + "\n\n".join([paragraph] * 8)

    chunks = _chunks(text, "файл")

    assert all(title == "Раздел" for title, _, _ in chunks)
    assert "# не заголовок" in chunks[0][2]
    assert len(chunks) > 1 and all(len(body) <= 2 * CHUNK_CHARS for _, _, body in chunks)
