"""Восстановление вызовов инструментов, которые модель напечатала обычным текстом."""

import json

from meepo.llm.markup import recover


BROWSER = [{"function": {"name": "browser", "parameters": {"type": "object"}}}]


def test_recovers_plain_browser_open() -> None:
    calls, text = recover(BROWSER, "browser open https://example.com", "")
    assert text == "" and len(calls) == 1
    assert calls[0]["name"] == "browser"
    assert json.loads(calls[0]["arguments"]) == {"action": "open", "url": "https://example.com"}


def test_plain_browser_command_must_be_the_whole_answer() -> None:
    content = "Например: browser open https://example.com"
    assert recover(BROWSER, content, "") == ([], content)


def test_plain_browser_command_requires_known_tool() -> None:
    content = "browser open https://example.com"
    assert recover([], content, "") == ([], content)
