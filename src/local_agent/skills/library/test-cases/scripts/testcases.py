"""Тест-кейсы по Markdown-документации: генерация и ревью по кускам страниц, с продолжением с места остановки.

Скрипт шагов скилла test-cases. Аргументы — JSON в stdin:
    {"mode": "generate", "path": "docs/раздел", "limit": 20, "force": false}
    {"mode": "review", "path": "docs/раздел", "limit": 20}
path — папка раздела или файл .md относительно папки проекта (она же текущая папка скрипта).
Итог — в stdout, ход работы («docs/a.md: кусок 3 из 12») — в stderr: его видно в строке шага.

Модель — та же, что в сессии: шаг передаёт адрес, модель и ключ переменными MEEPO_LLM_URL, MEEPO_LLM_MODEL,
MEEPO_LLM_KEY и MEEPO_LLM_VERIFY_SSL.

Что где лежит в папке проекта:
- testgen/rules.md — что за продукт и правила стиля, дописываются в промпт генерации;
- testgen/example.json — пример тест-кейса для формата вместо встроенного;
- testgen/test_cases/<путь страницы> — тест-кейсы страницы одной таблицей, рядом .style.txt — замечания по стилю;
- testgen/review/<путь страницы> — замечания ревьюера по кускам страницы;
- testgen/.work/<путь страницы>.json — кейсы и ревью по кускам: по ним работа продолжается после остановки.
"""

import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = "testgen"
OUTPUT_DIR = f"{CONFIG_DIR}/test_cases"
REVIEW_DIR = f"{CONFIG_DIR}/review"
WORK_DIR = f"{CONFIG_DIR}/.work"
RULES_FILE = "rules.md"
EXAMPLE_FILE = "example.json"
# Кусок около 6–7 тыс. токенов: вместе с промптом и ответом помещается даже в окно 16K.
CHUNK_CHARS = 20000
MAX_CASES = 8
MIN_CHARS = 400
# Файл больше этого в папке — обычно сводный (вся документация одним файлом, её страницы лежат рядом): его
# пропускаем. Файл, указанный путём напрямую, обрабатывается при любом размере — по кускам.
MAX_PAGE_BYTES = 1024 * 1024
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
TEMPERATURE = 0.2
# Пустой или битый ответ модели иногда бывает и на исправном сервере: кусок повторяем.
ATTEMPTS = 3
REQUEST_TIMEOUT = 900
SKIPPED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}

EXAMPLE = {
    "title": "Авторизация пользователя",
    "priority": "High",
    "preconditions": ["Сервис запущен.", "Учётная запись admin существует."],
    "steps": [
        {"action": "Откройте страницу входа.", "test_data": "—", "expected": "Открывается форма входа."},
        {"action": "Оставьте поля пустыми и нажмите кнопку «Войти».", "test_data": "Логин = пусто; Пароль = пусто",
         "expected": "У полей отображается сообщение «Обязательное поле». Вход не выполняется."},
        {"action": "Введите логин и неверный пароль, нажмите кнопку «Войти».", "test_data": "Логин = admin; Пароль = wrong",
         "expected": "Отображается сообщение «Неверный логин или пароль». Вход не выполняется."},
        {"action": "Введите логин и верный пароль, нажмите кнопку «Войти».", "test_data": "Логин = admin; Пароль = admin",
         "expected": "Открывается главная страница."},
    ],
    "questions": "",
}

SYSTEM = """Ты — опытный QA-инженер. По фрагменту официальной документации составь ручные функциональные тест-кейсы.

Содержание:
- Опирайся ТОЛЬКО на приведённый фрагмент. Не выдумывай параметры, команды, пункты меню, порты и значения, которых нет в тексте.
- Один тест-кейс — один функциональный блок, например «Авторизация пользователя». Все проверки блока — валидация полей (пустые значения, неверный формат, граничные значения), обработка ошибок и успешный сценарий — пиши шагами внутри одного кейса, а не отдельными кейсами. Не дроби блок на мелкие кейсы.
- title — краткое название проверяемого функционального блока. preconditions — состояние системы и окружения перед началом теста.
- Путь по меню и названия кнопок пиши в action. В test_data пиши только конкретные вводимые значения, валидные и невалидные: «Поле = значение», IP-адреса, логины, пароли, имена файлов, команды CLI, фрагменты конфигов. Если данных для шага нет — test_data = «—».
- expected — результат именно этого шага.
- Негативные проверки добавляй только там, где документация описывает ограничения, ошибки или валидацию.
- questions — неясности, противоречия и пробелы в требованиях, которые мешают проверить блок: сформулируй вопросами. Если вопросов нет — пустая строка.
- Если во фрагменте нечего тестировать (оглавление, справка, лицензия, список ссылок) — верни пустой список.
- Пиши на русском языке."""

SCHEMA = {
    "type": "object",
    "properties": {
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "preconditions": {"type": "array", "items": {"type": "string"}},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "test_data": {"type": "string"},
                                "expected": {"type": "string"},
                            },
                            "required": ["action", "test_data", "expected"],
                        },
                    },
                    "questions": {"type": "string"},
                },
                "required": ["title", "priority", "preconditions", "steps", "questions"],
            },
        }
    },
    "required": ["test_cases"],
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "test_cases", "schema": SCHEMA, "strict": True}}

REVIEW = """Ты — строгий ревьюер тест-кейсов. Твоя задача — найти ошибки в структуре, контенте и оформлении. Сверь тест-кейсы с фрагментом документации и правилами:

Правила оформления (строго эта последовательность колонок):
ID (TC-001, TC-002...), Название, Приоритет (High/Medium/Low), Предусловия, Тестовые данные, Шаги, Ожидаемый результат, Вопросы.
Номера в колонках «Тестовые данные» и «Ожидаемый результат» — номера шагов, к которым они относятся: это правильный формат.

Правила группировки:
Один тест-кейс = один функциональный блок. Не дроби блок на мелкие кейсы. (Например, кейс «Авторизация» должен включать валидацию всех полей, обработку ошибок и успех внутри одного кейса через разные шаги).

Выпиши замечания со ссылкой на ID кейса:
1. Что из фрагмента документации не покрыто ни одним кейсом — по разделам.
2. Кейсы, которых нет в документации (выдуманные параметры, пункты меню, значения).
3. Шаги без проверяемого ожидаемого результата или с расплывчатыми ожиданиями («работает корректно», «успешно»).
4. Нарушение группировки (разбитый на части блок) или дубли.
5. Замечания автоматической проверки стиля (если они есть ниже) — перенеси их своими словами.

Пиши по-русски, только замечания, без пересказа кейсов. В конце — строка «Вывод: …»: можно ли отдавать кейсы этого фрагмента в работу и что поправить в первую очередь."""

COLUMNS = ("ID", "Название", "Приоритет", "Предусловия", "Тестовые данные", "Шаги", "Ожидаемый результат", "Вопросы")

# Проверка стиля по правилам ASD-STE100 для русского языка: глагол в начале шага, длина, канцелярит.
MAX_SENTENCE_WORDS = 20
BANNED = [
    "осуществ", "произв", "выполнить нажатие", "данн", "являет", "в рамках", "соответствующ",
    "успешн", "корректн", "на сегодняшний день", "необходимо",
]
BANNED_RE = re.compile(r"(?i)(?<![а-яё])(" + "|".join(BANNED) + r")")
# «данные» (тестовые данные, данные кэша) — обычное слово, а не канцелярское «данный».
BANNED_OK = re.compile(r"(?i)(?<![а-яё])данн(ые|ых|ым|ыми)(?![а-яё])")
# Повелительное наклонение на «вы»: «Откройте», «Убедитесь».
IMPERATIVE = re.compile(r"(те|тесь)$")


class Unavailable(Exception):
    """Сервер модели недоступен: работа встаёт, готовые куски сохранены."""


class ModelError(Exception):
    """Сервер ответил ошибкой или модель — не тем, что просили: кусок не получился."""


@dataclass(frozen=True)
class Page:
    relative: str
    title: str
    location: str
    source: str
    text: str


@dataclass
class PageResult:
    relative: str
    output: str = ""
    cases: int = 0
    chunks: int = 0
    style_issues: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: str = ""


class Model:
    """Модель сессии через OpenAI-совместимый API (Ollama, LM Studio, облако)."""

    def __init__(self) -> None:
        self.url = os.environ.get("MEEPO_LLM_URL", "").rstrip("/")
        self.model = os.environ.get("MEEPO_LLM_MODEL", "")
        self.key = os.environ.get("MEEPO_LLM_KEY", "")
        verify = os.environ.get("MEEPO_LLM_VERIFY_SSL", "true").lower() != "false"
        self.context = None if verify else ssl._create_unverified_context()
        # Рассуждения модели замедляют генерацию в разы, а качество кейсов почти не меняют.
        self.reasoning_off = True

    def chat(self, system: str, user: str, response_format: dict | None = None) -> str:
        payload: dict = {
            "model": self.model, "stream": False, "temperature": TEMPERATURE,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.reasoning_off:
            payload["reasoning_effort"] = "none"
        if response_format:
            payload["response_format"] = response_format
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})}
        request = urllib.request.Request(
            self.url + "/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT, context=self.context) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = " ".join(exc.read().decode("utf-8", errors="replace").split())[:300]
            if 400 <= exc.code < 500:
                # Сервер мог не принять reasoning_effort: дальше обходимся без него.
                self.reasoning_off = False
            raise ModelError(f"сервер модели вернул HTTP {exc.code}: {detail}") from None
        except (OSError, ValueError) as exc:
            raise Unavailable(f"Сервер модели недоступен: {exc}") from None
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("сервер вернул ответ без текста") from None
        # Модель с рассуждениями может прислать их в начале ответа.
        return re.sub(r"(?s)^\s*<think>.*?</think>", "", content).strip()


# ---------- страницы ----------


def find_pages(root: Path, start: Path) -> list[Path]:
    """Markdown-страницы раздела по порядку обхода; папка testgen/ и служебные папки пропускаются."""
    if start.is_file():
        return [start]
    config = (root / CONFIG_DIR).resolve()
    pages: list[Path] = []
    for directory, dirs, files in os.walk(start):
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIPPED_DIRS and not name.startswith(".") and (Path(directory) / name).resolve() != config
        )
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() in {".md", ".markdown"} and path.resolve().is_relative_to(root):
                pages.append(path)
    return pages


def parse_page(path: Path, root: Path) -> Page:
    text = path.read_text(encoding="utf-8")
    relative = path.relative_to(root).as_posix()
    meta: dict[str, str] = {}
    front = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if front:
        for line in front.group(1).splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"')
        text = text[front.end():]
    heading = re.search(r"(?m)^# (.+)$", text)
    title = meta.get("title") or (heading.group(1).strip() if heading else path.stem)
    # Картинки модели не видны, оглавления и списки дочерних страниц не несут тестируемого содержания.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"(?s)\n---\n\n### Дочерние страницы\n.*$", "", text)
    text = re.sub(r"(?m)^[ \t]*[-*][ \t]*\[[^\]]*\]\([^)]*\)[ \t]*$\n?", "", text)
    # Заголовок страницы и так передаётся отдельно.
    text = re.sub(r"(?m)^# .*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Page(
        relative=relative, title=title, location=meta.get("path") or relative, source=meta.get("source", ""), text=text,
    )


def chunks(text: str, max_chars: int) -> list[str]:
    """Режет страницу по заголовкам ##/### так, чтобы кусок помещался в max_chars."""
    if len(text) <= max_chars:
        return [text]
    parts = re.split(r"(?m)^(?=#{2,3} )", text)
    out: list[str] = []
    current = ""
    for part in parts:
        # Раздел сам по себе огромный — режем по абзацам.
        while len(part) > max_chars:
            cut = part.rfind("\n\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if current:
                out.append(current)
                current = ""
            out.append(part[:cut])
            part = part[cut:]
        if current and len(current) + len(part) > max_chars:
            out.append(current)
            current = ""
        current += part
    if current.strip():
        out.append(current)
    return out


def system_prompt(root: Path) -> str:
    config = root / CONFIG_DIR
    rules = config / RULES_FILE
    example_file = config / EXAMPLE_FILE
    example = json.loads(example_file.read_text(encoding="utf-8")) if example_file.is_file() else EXAMPLE
    parts = [SYSTEM]
    if rules.is_file() and rules.read_text(encoding="utf-8").strip():
        parts.append("Правила проекта:\n" + rules.read_text(encoding="utf-8").strip())
    parts.append(
        "Пример правильно оформленного тест-кейса (только формат и стиль, не содержание):\n"
        + json.dumps(example, ensure_ascii=False, indent=1)
    )
    return "\n\n".join(parts)


# ---------- таблица и стиль ----------


def _cell(value: object) -> str:
    return " ".join(str(value or "").replace("|", "\\|").split())


def _numbered(steps: list[dict], key: str) -> str:
    items = [f"{index}. {_cell(step.get(key))}" for index, step in enumerate(steps, start=1)
             if _cell(step.get(key)).strip("—- ")]
    return "<br>".join(items) or "—"


def rows(cases: list[dict], first: int = 1) -> list[str]:
    """Строки таблицы: тестовые данные и ожидаемые результаты пронумерованы как шаги, к которым относятся;
    шаги без данных или без отдельного результата в этих столбцах пропускаются."""
    lines = []
    for number, case in enumerate(cases, start=first):
        row = (
            f"TC-{number:03d}", _cell(case["title"]), _cell(case["priority"]),
            "<br>".join(_cell(item) for item in case["preconditions"] if _cell(item)) or "—",
            _numbered(case["steps"], "test_data"), _numbered(case["steps"], "action"),
            _numbered(case["steps"], "expected"),
            # Вопросов нет — столбец пустой, а не «—»: так видно, где они действительно есть.
            _cell(case.get("questions")),
        )
        lines.append("| " + " | ".join(row) + " |")
    return lines


def table(cases: list[dict], first: int = 1) -> str:
    return "\n".join(["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS), *rows(cases, first)])


def render(page: Page, cases: list[dict]) -> str:
    """Все кейсы страницы — одна таблица."""
    lines = [
        f"# Тест-кейсы: {page.title}", "",
        f"- Документация: {page.location}",
        *([f"- Источник: {page.source}"] if page.source else []),
        f"- Количество кейсов: {len(cases)}", "",
        table(cases),
    ]
    return "\n".join(lines) + "\n"


def lint(cases: list[dict], first: int = 1) -> list[str]:
    issues: list[str] = []
    for number, case in enumerate(cases, start=first):
        for index, step in enumerate(case["steps"], start=1):
            where = f"TC-{number:03d}, шаг {index}"
            word = (step["action"].split() or [""])[0].strip("«»\"'.,:")
            if not IMPERATIVE.search(word) and not step["action"].startswith("Если"):
                issues.append(f"{where}: шаг не начинается с глагола («{word}»)")
            for name in ("action", "expected"):
                for sentence in re.split(r"(?<=[.!?])\s+", step[name]):
                    if len(sentence.split()) > MAX_SENTENCE_WORDS:
                        issues.append(f"{where}: в {name} предложение длиннее {MAX_SENTENCE_WORDS} слов")
                for match in BANNED_RE.finditer(BANNED_OK.sub("", step[name])):
                    issues.append(f"{where}: в {name} запрещённое слово «{match.group(1)}…»")
    return issues


def _valid(case: object) -> bool:
    # Без вопросов кейс полноценный: сервер без строгой схемы мог опустить пустое поле.
    required = set(SCHEMA["properties"]["test_cases"]["items"]["required"]) - {"questions"}
    if not isinstance(case, dict) or not all(key in case for key in required):
        return False
    steps = case["steps"]
    return isinstance(steps, list) and isinstance(case["preconditions"], list) and all(
        isinstance(step, dict) and all(isinstance(step.get(key), str) for key in ("action", "expected"))
        for step in steps
    )


# ---------- работа по кускам ----------


def progress(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _path(root: Path, folder: str, relative: str, suffix: str = "") -> Path:
    return root / folder / (relative + suffix)


def load_work(root: Path, page: Page, total: int, fresh: bool = False) -> dict:
    """Кейсы и ревью по кускам страницы. Страница изменилась (или fresh) — работа начинается заново."""
    digest = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
    path = _path(root, WORK_DIR, page.relative, ".json")
    if not fresh and path.is_file():
        try:
            work = json.loads(path.read_text(encoding="utf-8"))
            if work.get("hash") == digest and len(work.get("chunks", [])) == total:
                return work
        except ValueError:
            pass
    return {"hash": digest, "chunks": [None] * total, "review": [None] * total}


def save_work(root: Path, page: Page, work: dict) -> None:
    path = _path(root, WORK_DIR, page.relative, ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(work, ensure_ascii=False), encoding="utf-8")


def ask_cases(model: Model, system: str, page: Page, chunk: str, index: int, total: int) -> list[dict]:
    user = (
        f"Страница: {page.title}\nПуть в документации: {page.location}\n"
        + (f"Фрагмент {index} из {total}.\n" if total > 1 else "")
        + f"\n<документация>\n{chunk}\n</документация>\n\n"
        f"Составь тест-кейсы (не более {MAX_CASES}) по этому фрагменту."
    )
    error: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            data = json.loads(model.chat(system, user, RESPONSE_FORMAT))
        except ModelError as exc:
            error = exc
            continue
        except ValueError as exc:
            error = ModelError(f"ответ модели — не JSON: {exc}")
            continue
        cases = data.get("test_cases") if isinstance(data, dict) else None
        return [case for case in cases or [] if _valid(case)]
    raise error or ModelError("модель не ответила")


def generate_page(model: Model, root: Path, path: Path, system: str, *, force: bool, whole: bool) -> PageResult:
    relative = path.relative_to(root).as_posix()
    result = PageResult(relative)
    if not whole and path.stat().st_size > MAX_PAGE_BYTES:
        result.skipped = "файл больше 1 МБ — похоже на сводный; чтобы обработать его, укажите путь к нему"
        return result
    page = parse_page(path, root)
    if len(page.text) < MIN_CHARS:
        result.skipped = "мало текста"
        return result
    parts = chunks(page.text, CHUNK_CHARS)
    result.chunks = len(parts)
    work = load_work(root, page, len(parts), fresh=force)
    for index, part in enumerate(parts):
        if work["chunks"][index] is not None:
            continue
        progress(f"{relative}: кусок {index + 1} из {len(parts)}")
        try:
            work["chunks"][index] = ask_cases(model, system, page, part, index + 1, len(parts))
        except ModelError as exc:
            result.errors.append(f"кусок {index + 1}/{len(parts)}: {exc}")
            continue
        save_work(root, page, work)
    # Страница с потерянными кусками не записывается: иначе она считалась бы готовой без части кейсов.
    if result.errors:
        return result
    cases = [case for chunk in work["chunks"] for case in chunk]
    out = _path(root, OUTPUT_DIR, relative)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(page, cases), encoding="utf-8")
    issues = lint(cases)
    report = out.with_name(out.stem + ".style.txt")
    if issues:
        report.write_text("\n".join(issues) + "\n", encoding="utf-8")
    elif report.exists():
        report.unlink()
    # Кейсы новые — прежнее ревью к ним не относится.
    work["review"] = [None] * len(parts)
    save_work(root, page, work)
    result.output = out.relative_to(root).as_posix()
    result.cases = len(cases)
    result.style_issues = len(issues)
    return result


def generate(model: Model, root: Path, start: Path, limit: int, force: bool) -> tuple[str, bool]:
    """Обрабатывает до limit страниц раздела; короткие страницы в лимит не входят. Возвращает итог и то, удалась ли работа:
    не удалась, если сервер модели пропал или ни одна взятая страница не записалась."""
    whole = start.is_file()
    pages = find_pages(root, start)
    system = system_prompt(root)
    results: list[PageResult] = []
    processed = 0
    try:
        for path in pages:
            if processed >= limit:
                break
            relative = path.relative_to(root).as_posix()
            if _path(root, OUTPUT_DIR, relative).exists() and not force:
                continue
            result = generate_page(model, root, path, system, force=force, whole=whole)
            results.append(result)
            if result.output:
                progress(f"+ {result.output}: кейсов {result.cases}")
            if not result.skipped:
                processed += 1
    except Unavailable as exc:
        text = summary(root, start, pages, results, whole)
        return f"{text}\n{exc}. Готовые куски сохранены, повторный запуск продолжит с места остановки.", False
    attempted = [page for page in results if not page.skipped]
    return summary(root, start, pages, results, whole), not attempted or any(page.output for page in attempted)


def _pending(root: Path, path: Path, whole: bool) -> bool:
    relative = path.relative_to(root).as_posix()
    if _path(root, OUTPUT_DIR, relative).exists():
        return False
    if not whole and path.stat().st_size > MAX_PAGE_BYTES:
        return False
    return len(parse_page(path, root).text) >= MIN_CHARS


def summary(root: Path, start: Path, pages: list[Path], results: list[PageResult], whole: bool) -> str:
    remaining = sum(1 for path in pages if _pending(root, path, whole))
    section = start.relative_to(root).as_posix() or "."
    lines = [f"Раздел: {section}. Страниц: {len(pages)}, осталось без тест-кейсов: {remaining}."]
    done = [page for page in results if page.output]
    failed = [page for page in results if page.errors]
    skipped = [page for page in results if page.skipped]
    if done:
        lines.append("Готово:")
        lines += [
            f"- {page.output}: кейсов {page.cases}, кусков {page.chunks}, замечаний по стилю {page.style_issues}"
            for page in done
        ]
    if failed:
        lines.append("Не записано из-за ошибок (повторите запуск — готовые куски сохранены):")
        lines += [f"- {page.relative}: {'; '.join(page.errors)}" for page in failed]
    if skipped:
        lines.append(f"Пропущено страниц: {len(skipped)} ({'; '.join(sorted({page.skipped for page in skipped}))}).")
    if not done and not failed:
        lines.append("Новых страниц для генерации нет." if not remaining else "Ничего не сгенерировано.")
    return "\n".join(lines)


# ---------- ревью ----------


def ask_review(model: Model, page: Page, chunk: str, cases: list[dict], first: int, index: int, total: int) -> str:
    issues = lint(cases, first)
    user = (
        f"Страница: {page.title}\nПуть в документации: {page.location}\n"
        + (f"Фрагмент {index} из {total}.\n" if total > 1 else "")
        + f"\n<документация>\n{chunk}\n</документация>\n\n<тест-кейсы>\n"
        + (table(cases, first) if cases else "(по этому фрагменту кейсов нет)")
        + "\n</тест-кейсы>\n"
        + ("\nЗамечания автоматической проверки стиля:\n" + "\n".join(f"- {issue}" for issue in issues) + "\n" if issues else "")
    )
    error: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            text = model.chat(REVIEW, user)
        except ModelError as exc:
            error = exc
            continue
        if text:
            return text
        error = ModelError("пустой ответ модели")
    raise error or ModelError("модель не ответила")


def review_page(model: Model, root: Path, path: Path) -> tuple[str, str, bool]:
    """Ревью страницы по кускам: каждый кусок документации — вместе с кейсами, написанными по нему.
    Возвращает (файл с замечаниями или "", выводы или причина, почему ревью нет, звали ли модель)."""
    page = parse_page(path, root)
    if not _path(root, WORK_DIR, page.relative, ".json").is_file():
        return "", "кейсы сделаны прежней версией генератора — для ревью сгенерируйте их заново (force)", False
    output = _path(root, OUTPUT_DIR, page.relative)
    parts = chunks(page.text, CHUNK_CHARS)
    work = load_work(root, page, len(parts))
    if any(chunk is None for chunk in work["chunks"]):
        return "", "страница изменилась после генерации — сгенерируйте кейсы заново", False
    first = 1
    sections = []
    worked = False
    for index, part in enumerate(parts):
        cases = work["chunks"][index]
        if work["review"][index] is None:
            worked = True
            progress(f"{page.relative}: ревью куска {index + 1} из {len(parts)}")
            work["review"][index] = ask_review(model, page, part, cases, first, index + 1, len(parts))
            save_work(root, page, work)
        span = f" (TC-{first:03d}–TC-{first + len(cases) - 1:03d})" if cases else " (кейсов нет)"
        sections.append(f"## Фрагмент {index + 1} из {len(parts)}{span}\n\n{work['review'][index].strip()}")
        first += len(cases)
    out = _path(root, REVIEW_DIR, page.relative)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"# Ревью тест-кейсов: {page.title}\n\n- Документация: {page.location}\n"
        f"- Тест-кейсы: {output.relative_to(root).as_posix()}\n\n" + "\n\n".join(sections) + "\n",
        encoding="utf-8",
    )
    # «**Вывод:** …» — модель любит выделять вывод: разметку убираем, чтобы найти и показать строку.
    lines = (re.sub(r"[*_`#>]+", "", line).strip() for text in work["review"] for line in text.splitlines())
    verdicts = [line for line in lines if line.startswith("Вывод")]
    return out.relative_to(root).as_posix(), "; ".join(verdicts[-3:]), worked


def review(model: Model, root: Path, start: Path, limit: int) -> tuple[str, bool]:
    """Ревью до limit страниц, которым оно ещё нужно; уже проверенные только перечисляются."""
    pages = find_pages(root, start)
    reviewed: list[str] = []
    waiting = 0
    worked = 0
    try:
        for path in pages:
            relative = path.relative_to(root).as_posix()
            if not _path(root, OUTPUT_DIR, relative).is_file():
                continue
            if worked >= limit:
                waiting += 1
                continue
            try:
                file, verdict, called = review_page(model, root, path)
            except ModelError as exc:
                reviewed.append(f"- {relative}: ревью не получилось ({exc}) — повторите запуск")
                continue
            worked += called
            reviewed.append(f"- {file} — {verdict or 'вывода нет'}" if file else f"- {relative}: {verdict}")
    except Unavailable as exc:
        lines = ["Ревью остановлено.", *reviewed, f"{exc}. Готовые куски ревью сохранены, повторный запуск продолжит."]
        return "\n".join(lines), False
    if not reviewed:
        return "Тест-кейсов для ревью нет: сначала их нужно сгенерировать.", False
    head = f"Ревью тест-кейсов: страниц {len(reviewed)}" + (f", ещё ждут ревью: {waiting}" if waiting else "") + "."
    return "\n".join([head, *reviewed]), True


# ---------- запуск ----------


def main() -> int:
    try:
        arguments = json.load(sys.stdin)
    except ValueError:
        print("Аргументы — JSON-объект в stdin")
        return 2
    root = Path.cwd().resolve()
    mode = arguments.get("mode", "generate")
    limit = arguments.get("limit", DEFAULT_LIMIT)
    force = arguments.get("force", False)
    raw = arguments.get("path")
    if mode not in ("generate", "review"):
        print("mode — generate или review")
        return 2
    if isinstance(limit, str) and limit.strip().isdigit():
        limit = int(limit)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        print(f"limit — целое число от 1 до {MAX_LIMIT}")
        return 2
    if not isinstance(raw, str) or not raw.strip():
        print("path — папка раздела или файл .md в проекте")
        return 2
    start = (root / raw.strip().strip("`")).resolve()
    if not start.is_relative_to(root) or not start.exists():
        print(f"Нет папки или файла {raw} в проекте")
        return 2
    model = Model()
    if not model.url or not model.model:
        print("Не задана модель: скрипт запускают шаги скилла, они передают MEEPO_LLM_URL и MEEPO_LLM_MODEL")
        return 2
    text, ok = generate(model, root, start, limit, force is True) if mode == "generate" else review(model, root, start, limit)
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
