"""run_command и browser на фейковой модели: подтверждение, вывод, лог, таймаут, снимок страницы с локаторами."""

import shutil
import sys

import pytest

YES = {"action": "approve"}


def results(h):
    return [m for m in h.messages() if m["role"] == "tool"]


def test_run_command_output_and_log(harness):
    command = f'"{sys.executable}" -c "import os; print(\'папка\', os.getcwd()); raise SystemExit(3)"'
    h = harness([{"calls": [("run_command", {"command": command})]}, "Команда упала с кодом 3."])
    h.turn("запусти", [YES])
    card = h.cards()[0]
    assert card["group"] == "command" and card["command"] == command
    result = results(h)[0]
    assert result["is_error"] and "Код возврата: 3" in result["content"]
    assert f"папка {h.project_dir}" in result["content"]
    log = h.project_dir / result["meta"]["log"]
    assert log.is_file() and "папка" in log.read_text(encoding="utf-8")


def test_run_command_timeout(harness):
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    h = harness([{"calls": [("run_command", {"command": command, "timeout": 1})]}, "Не уложилась."])
    h.turn("запусти", [YES])
    assert "не уложилась в 1 с" in results(h)[0]["content"]


def test_run_command_long_output_trimmed(harness):
    command = f'"{sys.executable}" -c "print(\'начало\'); print(\'x\' * 20000); print(\'конец\')"'
    h = harness([{"calls": [("run_command", {"command": command})]}, "Готово."])
    h.turn("запусти", [YES])
    content = results(h)[0]["content"]
    assert "начало" in content and "конец" in content and "вывод длинный" in content and len(content) < 7000


@pytest.fixture
def login_page(tmp_path):
    page = tmp_path / "project" / "login.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text("""<!doctype html><html><head><meta charset="utf-8"><title>Вход</title></head><body>
<h1>Вход в систему</h1>
<label for="login">Логин</label><input id="login" name="login">
<input type="password" placeholder="Пароль" name="password">
<button onclick="document.querySelector('#result').textContent = 'Привет, ' + document.querySelector('#login').value">Войти</button>
<p id="result"></p><a href="#help">Помощь</a></body></html>""", encoding="utf-8")
    return page


def test_browser_snapshot_locators_and_actions(harness, login_page):
    if not (shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("chromium")):
        pytest.skip("нет браузера для Playwright")
    url = login_page.as_uri()
    h = harness([
        {"calls": [("browser", {"action": "open", "url": url})]},
        {"calls": [("browser", {"action": "snapshot"})]},
        {"calls": [("browser", {"action": "highlight", "refs": ["e1", "e3"]})]},
        {"calls": [("browser", {"action": "type", "ref": "e1", "text": "admin"}),
                   ("browser", {"action": "click", "ref": "e3"})]},
        {"calls": [("browser", {"action": "snapshot"}), ("browser", {"action": "close"})]},
        "Готово.",
    ])
    h.services.settings_path.parent.mkdir(parents=True, exist_ok=True)
    h.services.settings_path.write_text('{"browser_headed": false}', encoding="utf-8")
    h.turn("проверь вход", [{"action": "approve", "trust": True}, YES, YES])
    assert len(h.cards()) == 3 and all(card["group"] == "browser" for card in h.cards())
    assert h.cards()[0]["allow_trust"] is True
    assert all(card["allow_trust"] is False for card in h.cards()[1:])
    out = [m["content"] for m in results(h)]
    assert all(not m.get("is_error") for m in results(h)), out
    snapshot = out[1]
    assert "Страница: Вход" in snapshot
    assert 'e1 textbox «Логин» — page.get_by_role("textbox", name="Логин")' in snapshot
    assert 'page.get_by_placeholder("Пароль")' in snapshot or 'get_by_role("textbox", name="Пароль")' in snapshot
    assert 'e3 button «Войти» — page.get_by_role("button", name="Войти")' in snapshot
    assert 'e4 link «Помощь»' in snapshot
    assert out[2] == "На странице выделены элементы: e1, e3"
    assert "Привет, admin" in out[5]
    assert out[6] == "Браузер закрыт"


def test_browser_bad_ref_is_error_for_model(harness, login_page):
    if not (shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("chromium")):
        pytest.skip("нет браузера для Playwright")
    h = harness([
        {"calls": [("browser", {"action": "open", "url": login_page.as_uri()}),
                   ("browser", {"action": "click", "ref": "e99"}), ("browser", {"action": "close"})]},
        "Не нашёл.",
    ])
    h.services.settings_path.parent.mkdir(parents=True, exist_ok=True)
    h.services.settings_path.write_text('{"browser_headed": false}', encoding="utf-8")
    h.turn("кликни", [{"action": "approve", "trust": True}, YES])
    failed = results(h)[1]
    assert failed["is_error"] and "Нет элемента e99" in failed["content"]


def test_trusted_browser_still_confirms_each_interaction(harness, login_page):
    if not (shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("chromium")):
        pytest.skip("нет браузера для Playwright")
    h = harness([
        {"calls": [("browser", {"action": "open", "url": login_page.as_uri()})]},
        {"calls": [("browser", {"action": "snapshot"})]},
        {"calls": [("browser", {"action": "click", "ref": "e3"})]},
    ])
    h.services.settings_path.parent.mkdir(parents=True, exist_ok=True)
    h.services.settings_path.write_text('{"browser_headed": false}', encoding="utf-8")
    h.turn("открой страницу", [{"action": "approve", "trust": True}, {"action": "reject"}])
    cards = h.cards()
    assert len(cards) == 2 and cards[1]["title"].startswith("Браузер: click")
    assert cards[1]["allow_trust"] is False
    assert results(h)[2]["content"] == "Действие отменено через карточку"
    assert h.messages()[-1]["content"] == "Действие отменено."
    assert len(h.llm.requests) == 3
