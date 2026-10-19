"""Генератор тест-кейсов агента test-cases: разбор страниц, куски, генерация с продолжением, ревью по кускам, запуск."""

import importlib.util
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("testcases_script", Path(__file__).parents[1] / "src/qa/testcases.py")
script = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = script
SPEC.loader.exec_module(script)

BODY = "## Настройка\n\n" + "Откройте меню «Сервер» и задайте адрес. " * 20

CASE = {
    "title": "Настройка адреса сервера",
    "priority": "High",
    "preconditions": ["EMS запущен."],
    "steps": [{"action": "Откройте меню «Сервер».", "test_data": "IP = 10.0.0.1 | 24", "expected": "Открывается вкладка."}],
    "questions": "",
}


class FakeModel:
    """Отвечает заготовленными ответами по очереди; ответ-исключение выбрасывается."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, user, response_format=None):
        self.calls.append({"system": system, "user": user, "response_format": response_format})
        reply = self.replies.pop(0) if self.replies else {"test_cases": [CASE]}
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def page(root, relative, text, title="Страница", source="https://docs/pages/viewpage.action?pageId=42"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ntitle: "{title}"\npath: "Документация > {title}"\nsource: "{source}"\n---\n{text}', encoding="utf-8")
    return path


def test_parse_page_drops_images_links_and_child_pages(tmp_path):
    text = (
        "# Заголовок\n\nТекст ![схема](img.png) раздела.\n\n- [Ссылка](other.md)\n\nЕщё текст.\n"
        "\n---\n\n### Дочерние страницы\n\n- [Дочь](child/index.md)\n"
    )
    parsed = script.parse_page(page(tmp_path, "docs/a/index.md", text, title="Инициализация"), tmp_path)

    assert parsed.title == "Инициализация" and parsed.location == "Документация > Инициализация"
    assert parsed.source.endswith("pageId=42") and parsed.relative == "docs/a/index.md"
    assert parsed.text == "Текст  раздела.\n\nЕщё текст."


def test_parse_page_without_front_matter_uses_heading(tmp_path):
    (tmp_path / "guide.md").write_text("# Руководство\n\nТекст.\n", encoding="utf-8")

    parsed = script.parse_page(tmp_path / "guide.md", tmp_path)

    assert parsed.title == "Руководство" and parsed.location == "guide.md"
    assert parsed.source == "" and parsed.text == "Текст."


def test_chunks_split_by_headings_and_keep_all_text():
    text = "".join(f"## Раздел {n}\n\n" + "слово " * 300 + "\n\n" for n in range(6)) + "## Огромный\n\n" + ("абзац " * 200 + "\n\n") * 10

    parts = script.chunks(text, 4000)

    assert len(parts) > 3 and all(len(part) <= 4000 for part in parts)
    assert "".join(parts) == text


def test_lint_checks_verb_length_and_banned_words():
    case = {"steps": [
        {"action": "Нажатие кнопки «Сохранить».", "expected": "Настройки успешно применены."},
        {"action": "Если поле пустое, введите тестовые данные.", "expected": " ".join(["слово"] * 21) + "."},
        {"action": "Убедитесь, что данные сохранены.", "expected": "Открывается окно."},
    ]}

    issues = script.lint([case], first=5)

    assert any("TC-005, шаг 1: шаг не начинается с глагола" in issue for issue in issues)
    assert any("TC-005, шаг 1: в expected запрещённое слово «успешн…»" in issue for issue in issues)
    assert any("TC-005, шаг 2: в expected предложение длиннее 20 слов" in issue for issue in issues)
    assert not any("шаг 3" in issue for issue in issues)


def test_generator_writes_pages_in_batches_and_resumes(tmp_path):
    page(tmp_path, "docs/a/index.md", BODY, title="Первая")
    page(tmp_path, "docs/a/short/index.md", "Мало текста.", title="Короткая")
    page(tmp_path, "docs/b/index.md", BODY, title="Вторая", source="")
    model = FakeModel()

    first, first_ok = script.generate(model, tmp_path, tmp_path / "docs", 1, False)
    second, _ = script.generate(model, tmp_path, tmp_path / "docs", 5, False)
    third, _ = script.generate(model, tmp_path, tmp_path / "docs", 5, False)

    out = (tmp_path / "testgen/test_cases/docs/a/index.md").read_text(encoding="utf-8")
    assert "| TC-001 | Настройка адреса сервера | High | EMS запущен. | 1. IP = 10.0.0.1 \\| 24 |" in out
    assert first_ok and "осталось без тест-кейсов: 1" in first and "testgen/test_cases/docs/a/index.md: кейсов 1" in first
    # Короткая страница пропускается и в лимит не входит.
    assert "осталось без тест-кейсов: 0" in second and "мало текста" in second
    assert "Новых страниц для генерации нет." in third and len(model.calls) == 2
    assert model.calls[0]["response_format"]["json_schema"]["strict"] is True


def test_cases_are_one_table_with_numbered_steps_and_questions(tmp_path):
    page(tmp_path, "docs/login.md", BODY, title="Вход")
    login = {
        "title": "Авторизация пользователя", "priority": "High", "preconditions": ["Сервис запущен.", "Есть admin."],
        "steps": [
            {"action": "Откройте страницу входа.", "test_data": "—", "expected": "Открывается форма."},
            {"action": "Введите неверный пароль.", "test_data": "Пароль = wrong", "expected": "—"},
            {"action": "Нажмите «Войти».", "test_data": "", "expected": "Вход не выполняется."},
        ],
        "questions": "Сколько попыток до блокировки?",
    }
    script.generate(FakeModel({"test_cases": [login, {**CASE, "questions": ""}]}), tmp_path, tmp_path / "docs", 5, False)

    lines = (tmp_path / "testgen/test_cases/docs/login.md").read_text(encoding="utf-8").splitlines()
    table = lines[lines.index("| ID | Название | Приоритет | Предусловия | Тестовые данные | Шаги | Ожидаемый результат | Вопросы |"):]
    assert table[2] == (
        "| TC-001 | Авторизация пользователя | High | Сервис запущен.<br>Есть admin. | 2. Пароль = wrong "
        "| 1. Откройте страницу входа.<br>2. Введите неверный пароль.<br>3. Нажмите «Войти». "
        "| 1. Открывается форма.<br>3. Вход не выполняется. | Сколько попыток до блокировки? |"
    )
    # Вопросов нет — столбец пустой.
    assert table[3].startswith("| TC-002 | Настройка адреса сервера |") and table[3].endswith("| 1. Открывается вкладка. |  |")


def test_system_prompt_takes_project_rules_and_example(tmp_path):
    (tmp_path / "testgen").mkdir()
    (tmp_path / "testgen/rules.md").write_text("Продукт: Wi-Fi контроллер SoftWLC.", encoding="utf-8")
    (tmp_path / "testgen/example.json").write_text(json.dumps({"title": "Создание домена"}), encoding="utf-8")

    prompt = script.system_prompt(tmp_path)

    assert "Правила проекта:\nПродукт: Wi-Fi контроллер SoftWLC." in prompt
    # Свой пример заменяет встроенный целиком.
    assert "Создание домена" in prompt and "Пароль = wrong" not in prompt


def test_empty_or_broken_answer_is_retried(tmp_path):
    page(tmp_path, "docs/a.md", BODY)
    model = FakeModel("", script.ModelError("сервер модели вернул HTTP 500"), {"test_cases": [CASE]})

    text, ok = script.generate(model, tmp_path, tmp_path / "docs", 5, False)

    assert ok and "testgen/test_cases/docs/a.md: кейсов 1" in text and len(model.calls) == 3


def test_page_with_failed_chunk_is_not_written_and_stays_pending(tmp_path):
    page(tmp_path, "docs/a.md", BODY)
    model = FakeModel("не json", "снова не json", "и опять")

    text, ok = script.generate(model, tmp_path, tmp_path / "docs", 5, False)

    assert not ok and "кусок 1/1: ответ модели — не JSON" in text
    assert "осталось без тест-кейсов: 1" in text and not (tmp_path / "testgen/test_cases/docs/a.md").exists()


def test_batch_with_one_failed_page_returns_failure_and_keeps_published_page(tmp_path):
    page(tmp_path, "docs/a.md", BODY)
    page(tmp_path, "docs/b.md", BODY)
    model = FakeModel({"test_cases": [CASE]}, *[script.ModelError("ошибка модели")] * script.ATTEMPTS)

    summary, ok = script.generate(model, tmp_path, tmp_path / "docs", 5, False)

    published = tmp_path / "testgen/test_cases/docs/a.md"
    original = published.read_bytes()
    assert not ok and "testgen/test_cases/docs/a.md: кейсов 1" in summary
    assert "docs/b.md: кусок 1/1: ошибка модели" in summary and "осталось без тест-кейсов: 1" in summary
    assert not (tmp_path / "testgen/test_cases/docs/b.md").exists()
    retry = FakeModel()
    _, ok = script.generate(retry, tmp_path, tmp_path / "docs", 5, False)
    assert ok and len(retry.calls) == 1 and published.read_bytes() == original


def test_big_file_is_skipped_in_folder_but_processed_by_path_and_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "MAX_PAGE_BYTES", 3000)
    monkeypatch.setattr(script, "CHUNK_CHARS", 1500)
    sections = "".join(f"## Раздел {n}\n\n" + "Задайте адрес сервера и сохраните. " * 30 + "\n\n" for n in range(4))
    big = page(tmp_path, "docs/all.md", sections, title="Вся документация")
    page(tmp_path, "docs/a.md", BODY)

    in_folder, _ = script.generate(FakeModel(), tmp_path, tmp_path / "docs", 5, False)
    skipped = not (tmp_path / "testgen/test_cases/docs/all.md").exists()
    broken = FakeModel({"test_cases": [CASE]}, {"test_cases": [CASE]}, script.Unavailable("Сервер модели недоступен"))
    stopped, stopped_ok = script.generate(broken, tmp_path, big, 5, False)
    resumed = FakeModel()
    done, done_ok = script.generate(resumed, tmp_path, big, 5, False)

    assert "похоже на сводный" in in_folder and skipped
    # Сервер пропал на третьем куске: два готовых сохранены, следующий запуск начинает с третьего.
    assert not stopped_ok and "продолжит с места остановки" in stopped
    parts = len(script.chunks(script.parse_page(big, tmp_path).text, 1500))
    assert parts == 4 and len(resumed.calls) == parts - 2
    assert done_ok and "testgen/test_cases/docs/all.md: кейсов 4, кусков 4" in done


@pytest.mark.parametrize("changed", ["instructions", "rules", "example", "schema", "generator", "source", "agent"])
def test_changed_generation_inputs_restart_all_chunks(tmp_path, monkeypatch, changed):
    monkeypatch.setattr(script, "CHUNK_CHARS", 1500)
    document = "".join(f"## Раздел {n}\n\n" + "Задайте адрес сервера и сохраните. " * 30 + "\n\n" for n in range(2))
    source = page(tmp_path, "docs/a.md", document)
    old = {**CASE, "title": "Старые инструкции"}
    new = {**CASE, "title": "Новые инструкции"}
    _, first_ok = script.generate(
        FakeModel({"test_cases": [old]}, script.Unavailable("нет соединения")), tmp_path, source, 5, False,
    )
    assert not first_ok
    config = tmp_path / "testgen"
    if changed == "instructions":
        monkeypatch.setattr(script, "SYSTEM", script.SYSTEM + "\nНовое правило.")
    elif changed == "rules":
        (config / "rules.md").write_text("Проверяйте адрес IPv6.", encoding="utf-8")
    elif changed == "example":
        (config / "example.json").write_text(json.dumps(new), encoding="utf-8")
    elif changed == "schema":
        monkeypatch.setattr(script, "RESPONSE_FORMAT", {**script.RESPONSE_FORMAT, "extra": True})
    elif changed == "generator":
        monkeypatch.setattr(script, "GENERATOR_VERSION", script.GENERATOR_VERSION + 1)
    elif changed == "source":
        monkeypatch.setattr(script, "GENERATOR_SOURCE_HASH", "changed-source")
    else:
        monkeypatch.setenv("MEEPO_AGENT_REVISION", "repaired-agent")

    resumed = FakeModel({"test_cases": [new]}, {"test_cases": [new]})
    _, ok = script.generate(resumed, tmp_path, source, 5, False)

    output = (tmp_path / "testgen/test_cases/docs/a.md").read_text(encoding="utf-8")
    assert ok and len(resumed.calls) == 2
    assert "Новые инструкции" in output and "Старые инструкции" not in output


def test_changed_revision_keeps_old_output_until_complete_and_then_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "CHUNK_CHARS", 1500)
    text = "".join(f"## Раздел {n}\n\n" + "Задайте адрес сервера и сохраните. " * 30 + "\n\n" for n in range(2))
    source = page(tmp_path, "docs/a.md", text)
    script.generate(FakeModel(), tmp_path, source, 5, False)
    output = tmp_path / "testgen/test_cases/docs/a.md"
    original = output.read_bytes()
    monkeypatch.setenv("MEEPO_AGENT_REVISION", "revision-2")
    replacement = {**CASE, "title": "Исправленный агент"}
    partial = FakeModel({"test_cases": [replacement]}, script.Unavailable("нет соединения"))

    summary, ok = script.generate(partial, tmp_path, source, 5, False)
    reviewer = FakeModel()
    review_summary, reviewed = script.review(reviewer, tmp_path, source, 5)

    assert not ok and "осталось без тест-кейсов: 1" in summary
    assert output.read_bytes() == original
    assert not reviewed and not reviewer.calls and "сгенерируйте кейсы заново" in review_summary
    resumed = FakeModel({"test_cases": [replacement]})
    _, ok = script.generate(resumed, tmp_path, source, 5, False)
    assert ok and len(resumed.calls) == 1
    assert "Исправленный агент" in output.read_text(encoding="utf-8")
    assert "Настройка адреса сервера" not in output.read_text(encoding="utf-8")


def test_existing_output_is_invalidated_when_rules_change(tmp_path):
    source = page(tmp_path, "docs/a.md", BODY)
    script.generate(FakeModel(), tmp_path, source, 5, False)
    (tmp_path / "testgen/rules.md").write_text("Только адреса IPv6.", encoding="utf-8")
    replacement = {**CASE, "title": "Новые правила"}
    model = FakeModel({"test_cases": [replacement]})

    _, ok = script.generate(model, tmp_path, source, 5, False)

    assert ok and len(model.calls) == 1
    assert "Новые правила" in (tmp_path / "testgen/test_cases/docs/a.md").read_text(encoding="utf-8")


def test_legacy_output_without_fingerprint_is_regenerated(tmp_path):
    source = page(tmp_path, "docs/a.md", BODY)
    parsed = script.parse_page(source, tmp_path)
    output = tmp_path / "testgen/test_cases/docs/a.md"
    output.parent.mkdir(parents=True)
    output.write_text("Старый результат", encoding="utf-8")
    script.save_work(tmp_path, parsed, {"hash": "legacy", "chunks": [[CASE]], "review": [None]})
    model = FakeModel()

    _, ok = script.generate(model, tmp_path, source, 5, False)

    assert ok and len(model.calls) == 1
    assert "Старый результат" not in output.read_text(encoding="utf-8")


def test_output_publication_uses_atomic_replace(tmp_path, monkeypatch):
    source = page(tmp_path, "docs/a.md", BODY)
    script.generate(FakeModel(), tmp_path, source, 5, False)
    output = tmp_path / "testgen/test_cases/docs/a.md"
    original = output.read_bytes()
    replace = Path.replace

    def fail_output_replace(path, target):
        if target == output:
            assert output.read_bytes() == original
            assert "Новый результат" in path.read_text(encoding="utf-8")
            raise OSError("ошибка публикации")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_output_replace)
    monkeypatch.setenv("MEEPO_AGENT_REVISION", "revision-2")
    with pytest.raises(OSError, match="ошибка публикации"):
        script.generate(FakeModel({"test_cases": [{**CASE, "title": "Новый результат"}]}), tmp_path, source, 5, False)
    assert output.read_bytes() == original
    assert list(output.parent.iterdir()) == [output]


def test_review_checks_each_chunk_with_its_own_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "CHUNK_CHARS", 1500)
    text = "".join(f"## Раздел {n}\n\n" + f"Пункт {n}: задайте адрес сервера. " * 40 + "\n\n" for n in range(2))
    page(tmp_path, "docs/a.md", text, title="Адреса")
    second = {**CASE, "title": "Второй блок", "steps": [{"action": "Успешно нажмите.", "test_data": "—", "expected": "Ок."}]}
    script.generate(FakeModel({"test_cases": [CASE]}, {"test_cases": [CASE, second]}), tmp_path, tmp_path / "docs", 5, False)
    model = FakeModel("1. Не покрыт пункт 0.\n**Вывод:** поправить TC-001.", "Замечаний нет.\nВывод: можно отдавать.")

    summary, ok = script.review(model, tmp_path, tmp_path / "docs", 5)
    again, _ = script.review(model, tmp_path, tmp_path / "docs", 5)

    first_call, second_call = model.calls
    # Ревьюер видит кусок документации и кейсы, написанные по нему, с номерами из общей таблицы страницы.
    assert "Пункт 0" in first_call["user"] and "Пункт 1" not in first_call["user"]
    assert "| TC-001 |" in first_call["user"] and "| TC-003 | Второй блок" in second_call["user"]
    assert "TC-003, шаг 1: в action запрещённое слово «Успешн…»" in second_call["user"]
    assert first_call["system"].startswith("Ты — строгий ревьюер тест-кейсов")
    report = (tmp_path / "testgen/review/docs/a.md").read_text(encoding="utf-8")
    assert "## Фрагмент 1 из 2 (TC-001–TC-001)" in report and "## Фрагмент 2 из 2 (TC-002–TC-003)" in report
    assert ok and "testgen/review/docs/a.md — Вывод: поправить TC-001.; Вывод: можно отдавать." in summary
    # Повторное ревью ничего не спрашивает: куски уже проверены.
    assert len(model.calls) == 2 and "testgen/review/docs/a.md" in again


def test_review_agent_revision_is_separate_from_author_and_invalidates_only_review(tmp_path, monkeypatch):
    source = page(tmp_path, "docs/a.md", BODY)
    monkeypatch.setenv("MEEPO_AGENT_REVISION", "author-1")
    script.generate(FakeModel(), tmp_path, source, 5, False)
    output = tmp_path / "testgen/test_cases/docs/a.md"
    original = output.read_bytes()
    monkeypatch.setenv("MEEPO_AGENT_REVISION", "reviewer-1")
    first = FakeModel("Вывод: проверено первым ревьюером.")
    _, ok = script.review(first, tmp_path, source, 5)
    assert ok and len(first.calls) == 1
    unchanged = FakeModel()
    script.review(unchanged, tmp_path, source, 5)
    assert not unchanged.calls

    monkeypatch.setenv("MEEPO_AGENT_REVISION", "reviewer-2")
    second = FakeModel("Вывод: проверено после исправления ревьюера.")
    summary, ok = script.review(second, tmp_path, source, 5)
    assert ok and len(second.calls) == 1 and "после исправления" in summary
    assert output.read_bytes() == original


def test_main_checks_arguments_and_needs_the_model(tmp_path, monkeypatch, capsys):
    page(tmp_path, "docs/a.md", BODY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEEPO_LLM_URL", raising=False)

    def run(arguments):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(arguments)))
        code = script.main()
        return code, capsys.readouterr().out

    assert run({"mode": "delete", "path": "docs"})[0] == 2
    assert run({"path": "docs", "limit": 0})[0] == 2
    assert run({"path": "../"})[0] == 2
    code, out = run({"path": "docs"})
    assert code == 2 and "MEEPO_LLM_URL" in out


def test_model_talks_to_openai_compatible_server_and_drops_rejected_option(monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — имя метода задаёт http.server
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            status = 400 if len(requests) == 1 else 200
            reply = {"error": "unknown field"} if status == 400 else {"choices": [{"message": {"content": "<think>x</think>ответ"}}]}
            data = json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("MEEPO_LLM_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("MEEPO_LLM_MODEL", "gemma")
    monkeypatch.setenv("MEEPO_LLM_KEY", "sk-1")
    try:
        model = script.Model()
        with pytest.raises(script.ModelError, match="HTTP 400"):
            model.chat("система", "вопрос")
        answer = model.chat("система", "вопрос")
    finally:
        server.shutdown()

    first, second = requests
    assert first["path"] == "/v1/chat/completions" and first["auth"] == "Bearer sk-1"
    assert first["body"]["reasoning_effort"] == "none" and first["body"]["model"] == "gemma"
    # Сервер не принял reasoning_effort — дальше запросы без него; рассуждения в начале ответа отрезаны.
    assert "reasoning_effort" not in second["body"] and answer == "ответ"
