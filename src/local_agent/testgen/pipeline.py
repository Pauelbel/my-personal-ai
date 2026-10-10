"""Генерация ручных тест-кейсов по Markdown-документации: страница → куски → LLM со схемой ответа → Markdown.

Настройки проекта и результаты лежат в папке testgen/ рабочей папки:
- testgen/rules.md — что за продукт и правила стиля, дописываются в системный промпт;
- testgen/example.json — пример тест-кейса для формата вместо встроенного;
- testgen/test_cases/<путь страницы> — тест-кейсы; готовые страницы при повторном запуске пропускаются.
"""

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from local_agent.llm.base import LLMProvider, LLMProviderError, LLMProviderUnavailable
from local_agent.llm.models import ChatMessage
from local_agent.testgen.style import lint
from local_agent.tools.filesystem import SKIPPED_DIRS

CONFIG_DIR = "testgen"
OUTPUT_DIR = f"{CONFIG_DIR}/test_cases"
RULES_FILE = "rules.md"
EXAMPLE_FILE = "example.json"
# Кусок около 6–7 тыс. токенов: вместе с промптом и ответом помещается в окно 16K.
CHUNK_CHARS = 20000
MAX_CASES = 8
MIN_CHARS = 400
# Сводный файл всей документации не обрабатываем: его страницы и так лежат по отдельности.
MAX_PAGE_BYTES = 1024 * 1024
TEMPERATURE = 0.2
# Пустой или битый ответ модели иногда бывает и на исправном сервере: кусок повторяем.
ATTEMPTS = 3

EXAMPLE = {
    "title": "Вход в веб-интерфейс с неверным паролем",
    "component": "Веб-интерфейс",
    "priority": "High",
    "type": "Негативный",
    "preconditions": ["Сервис запущен.", "Учётная запись admin существует."],
    "steps": [
        {"action": "Откройте страницу входа.", "test_data": "—", "expected": "Открывается форма входа."},
        {"action": "Введите логин и неверный пароль.", "test_data": "Логин = admin; Пароль = wrong",
         "expected": "Поля содержат введённые значения."},
        {"action": "Нажмите кнопку «Войти».", "test_data": "—",
         "expected": "Отображается сообщение «Неверный логин или пароль». Вход не выполняется."},
    ],
    "source_section": "Вход в систему",
}

SYSTEM = """Ты — опытный QA-инженер. По фрагменту официальной документации составь ручные функциональные тест-кейсы.

Содержание:
- Опирайся ТОЛЬКО на приведённый фрагмент. Не выдумывай параметры, команды, пункты меню, порты и значения, которых нет в тексте.
- Каждый тест-кейс — законченный сценарий проверки одной функции: от настройки до проверки результата. Не дроби одну процедуру на несколько кейсов.
- Путь по меню и названия кнопок пиши в action. В test_data пиши только вводимые значения: «Поле = значение», IP-адреса, логины, пароли, имена файлов, команды CLI, фрагменты конфигов. Если данных для шага нет — test_data = «—».
- source_section — текст заголовка раздела документации, из которого взят кейс (не номер).
- Добавляй негативные кейсы только там, где документация описывает ограничения, ошибки или валидацию.
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
                    "component": {"type": "string"},
                    "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "type": {"type": "string", "enum": ["Позитивный", "Негативный"]},
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
                    "source_section": {"type": "string"},
                },
                "required": ["title", "component", "priority", "type", "preconditions", "steps", "source_section"],
            },
        }
    },
    "required": ["test_cases"],
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {"name": "test_cases", "schema": SCHEMA, "strict": True}}


@dataclass(frozen=True)
class Page:
    relative: str
    title: str
    location: str
    source: str
    page_id: str
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


@dataclass
class BatchResult:
    section: str
    total: int
    remaining: int
    pages: list[PageResult]


def output_path(root: Path, relative: str) -> Path:
    return root / OUTPUT_DIR / relative


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
    source = meta.get("source", "")
    page_id = re.search(r"pageId=(\d+)", source)
    return Page(
        relative=relative,
        title=title,
        location=meta.get("path") or relative,
        source=source,
        page_id=page_id.group(1) if page_id else hashlib.sha1(relative.encode()).hexdigest()[:8],
        text=text,
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


def render(page: Page, cases: list[dict]) -> str:
    def cell(value: object) -> str:
        return (str(value or "") or "—").strip().replace("|", "\\|").replace("\n", "<br>") or "—"

    lines = [
        f"# Тест-кейсы: {page.title}", "",
        f"- Документация: {page.location}",
        *([f"- Источник: {page.source}"] if page.source else []),
        f"- Количество кейсов: {len(cases)}", "",
    ]
    for number, case in enumerate(cases, start=1):
        lines += [
            f"## TC-{page.page_id}-{number:02d}. {case['title']}", "",
            "| Поле | Значение |", "|---|---|",
            f"| Компонент | {cell(case['component'])} |",
            f"| Приоритет | {cell(case['priority'])} |",
            f"| Тип | {cell(case['type'])} |",
            f"| Раздел документации | {cell(case['source_section'])} |", "",
            "**Предусловия:**", "",
            *([f"- {item}" for item in case["preconditions"]] or ["- Нет"]), "",
            "| # | Шаг | Тестовые данные | Ожидаемый результат |", "|---|---|---|---|",
        ]
        for index, step in enumerate(case["steps"], start=1):
            lines.append(f"| {index} | {cell(step['action'])} | {cell(step.get('test_data'))} | {cell(step['expected'])} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _valid(case: object) -> bool:
    if not isinstance(case, dict) or not all(key in case for key in SCHEMA["properties"]["test_cases"]["items"]["required"]):
        return False
    steps = case["steps"]
    return isinstance(steps, list) and isinstance(case["preconditions"], list) and all(
        isinstance(step, dict) and all(isinstance(step.get(key), str) for key in ("action", "expected"))
        for step in steps
    )


class CaseGenerator:
    def __init__(
        self, provider: LLMProvider, model: str, root: Path, *,
        chunk_chars: int = CHUNK_CHARS, max_cases: int = MAX_CASES,
    ) -> None:
        self._provider = provider
        self._model = model
        self._root = root
        self._chunk_chars = chunk_chars
        self._max_cases = max_cases
        # Рассуждения модели замедляют генерацию в разы, а качество кейсов почти не меняют.
        self._reasoning_off = True

    async def run(
        self, start: Path, *, limit: int = 0, force: bool = False,
        on_page: Callable[[PageResult], None] | None = None,
    ) -> BatchResult:
        """Обрабатывает до limit страниц раздела (0 — все); короткие страницы в лимит не входят."""
        pages = find_pages(self._root, start)
        system = system_prompt(self._root)
        results: list[PageResult] = []
        processed = 0
        for path in pages:
            if limit and processed >= limit:
                break
            relative = path.relative_to(self._root).as_posix()
            if output_path(self._root, relative).exists() and not force:
                continue
            result = await self.generate(path, system)
            results.append(result)
            if on_page:
                on_page(result)
            if not result.skipped:
                processed += 1
        remaining = sum(1 for path in pages if self._pending(path))
        return BatchResult(start.relative_to(self._root).as_posix() or ".", len(pages), remaining, results)

    def _pending(self, path: Path) -> bool:
        relative = path.relative_to(self._root).as_posix()
        if output_path(self._root, relative).exists():
            return False
        return path.stat().st_size <= MAX_PAGE_BYTES and len(parse_page(path, self._root).text) >= MIN_CHARS

    async def generate(self, path: Path, system: str | None = None) -> PageResult:
        relative = path.relative_to(self._root).as_posix()
        result = PageResult(relative)
        if path.stat().st_size > MAX_PAGE_BYTES:
            result.skipped = "файл больше 1 МБ"
            return result
        page = parse_page(path, self._root)
        if len(page.text) < MIN_CHARS:
            result.skipped = "мало текста"
            return result
        system = system or system_prompt(self._root)
        parts = chunks(page.text, self._chunk_chars)
        result.chunks = len(parts)
        cases: list[dict] = []
        for index, part in enumerate(parts, start=1):
            try:
                cases += await self._ask(system, page, part, index, len(parts))
            except LLMProviderUnavailable:
                raise
            except (LLMProviderError, ValueError) as exc:
                result.errors.append(f"кусок {index}/{len(parts)}: {exc}")
        # Страница с потерянными кусками не записывается: иначе она считалась бы готовой без части кейсов.
        if result.errors:
            return result
        out = output_path(self._root, relative)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(page, cases), encoding="utf-8")
        issues = lint(cases)
        report = out.with_name(out.stem + ".style.txt")
        if issues:
            report.write_text("\n".join(issues) + "\n", encoding="utf-8")
        elif report.exists():
            report.unlink()
        result.output = out.relative_to(self._root).as_posix()
        result.cases = len(cases)
        result.style_issues = len(issues)
        return result

    async def _ask(self, system: str, page: Page, chunk: str, index: int, total: int) -> list[dict]:
        user = (
            f"Страница: {page.title}\nПуть в документации: {page.location}\n"
            + (f"Фрагмент {index} из {total}.\n" if total > 1 else "")
            + f"\n<документация>\n{chunk}\n</документация>\n\n"
            f"Составь тест-кейсы (не более {self._max_cases}) по этому фрагменту."
        )
        messages = [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]
        error: Exception | None = None
        for _ in range(ATTEMPTS):
            options: dict[str, object] = {"temperature": TEMPERATURE}
            if self._reasoning_off:
                options["reasoning_effort"] = "none"
            try:
                reply = await self._provider.chat(
                    self._model, messages, response_format=RESPONSE_FORMAT, options=options,
                )
                data = json.loads(reply.content or "")
            except LLMProviderUnavailable:
                raise
            except LLMProviderError as exc:
                # Ошибка HTTP 4xx — сервер мог не принять reasoning_effort: дальше обходимся без него.
                if "HTTP 4" in str(exc):
                    self._reasoning_off = False
                error = exc
                continue
            except ValueError as exc:
                error = ValueError(f"ответ модели — не JSON: {exc}")
                continue
            cases = data.get("test_cases") if isinstance(data, dict) else None
            return [case for case in cases or [] if _valid(case)]
        raise error or ValueError("модель не ответила")


def summary(batch: BatchResult) -> str:
    lines = [f"Раздел: {batch.section}. Страниц: {batch.total}, осталось без тест-кейсов: {batch.remaining}."]
    done = [page for page in batch.pages if page.output]
    failed = [page for page in batch.pages if page.errors]
    skipped = [page for page in batch.pages if page.skipped]
    if done:
        lines.append("Готово:")
        lines += [
            f"- {page.output}: кейсов {page.cases}, кусков {page.chunks}, замечаний по стилю {page.style_issues}"
            for page in done
        ]
    if failed:
        lines.append("Не записано из-за ошибок (повторите запуск):")
        lines += [f"- {page.relative}: {'; '.join(page.errors)}" for page in failed]
    if skipped:
        lines.append(f"Пропущено страниц: {len(skipped)} ({', '.join(sorted({page.skipped for page in skipped}))}).")
    if not done and not failed:
        lines.append("Новых страниц для генерации нет." if not batch.remaining else "Ничего не сгенерировано.")
    return "\n".join(lines)
