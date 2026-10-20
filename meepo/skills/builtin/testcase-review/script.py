"""testcase-review: ревью Markdown-файлов с тест-кейсами пачками, с продолжением с места остановки.

Аргументы — JSON в stdin: {"cases": "cases/login.md", "docs": "docs/auth.md", "limit": 20}. Каждая пачка кейсов —
отдельный запрос к модели с чистым контекстом. Готовые пачки — в MEEPO_STATE_DIR; итог — <файл>.review.md рядом
с кейсами и вопросы в колонке «Вопросы», если она есть.

Части файла: разбор файла кейсов на пункты и пачки → документация для пачки → ревью и запуск.
"""

import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import meepo_skill as ms

CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

# ---------- разбор файла тест-кейсов на пункты и пачки, колонка «Вопросы» ----------

BATCH_CHARS = 12000
BATCH_CASES = 10


@dataclass
class Item:
    """Кейс (строка таблицы) или раздел файла. text — без колонки «Вопросы»: её дописывает само ревью."""

    id: str
    text: str
    row: int = -1


@dataclass
class CaseFile:
    lines: list[str]
    items: list[Item]
    header: str = ""
    questions_column: int = -1


def split_row(line: str) -> list[str]:
    """Ячейки строки таблицы; экранированная \\| — часть ячейки."""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") and not body.endswith("\\|") else body
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def join_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def parse(text: str) -> CaseFile:
    """Первая таблица с заголовком и строками — кейсы построчно; таблицы нет — разделы по заголовкам."""
    lines = text.splitlines()
    for index in range(len(lines) - 2):
        if lines[index].lstrip().startswith("|") and re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", lines[index + 1]) \
                and "-" in lines[index + 1]:
            header = split_row(lines[index])
            column = next((number for number, name in enumerate(header) if "вопрос" in name.lower()), -1)
            shown = [name for number, name in enumerate(header) if number != column]
            head = join_row(shown) + "\n|" + "---|" * len(shown)
            items = []
            row = index + 2
            while row < len(lines) and lines[row].lstrip().startswith("|"):
                cells = split_row(lines[row])
                kept = [cell for number, cell in enumerate(cells) if number != column]
                items.append(Item(cells[0] or f"строка {len(items) + 1}", join_row(kept), row))
                row += 1
            if items:
                return CaseFile(lines, items, head, column)
    parts = ms.chunks(text, BATCH_CHARS)
    return CaseFile(lines, [Item(_title(part, number), part) for number, part in enumerate(parts, 1)])


def _title(part: str, number: int) -> str:
    heading = re.search(r"(?m)^#{1,4} (.+)$", part)
    return heading.group(1).strip() if heading else f"раздел {number}"


def batches(cases: CaseFile) -> list[list[Item]]:
    out: list[list[Item]] = []
    current: list[Item] = []
    size = 0
    for item in cases.items:
        if current and (len(current) >= BATCH_CASES or size + len(item.text) > BATCH_CHARS):
            out.append(current)
            current, size = [], 0
        current.append(item)
        size += len(item.text)
    if current:
        out.append(current)
    return out


def batch_text(cases: CaseFile, batch: list[Item]) -> str:
    rows = "\n".join(item.text for item in batch)
    return f"{cases.header}\n{rows}" if cases.header else rows


def fill_questions(cases: CaseFile, questions: dict[str, list[str]]) -> int:
    """Дописывает вопросы в колонку «Вопросы» строк таблицы; уже записанные не повторяются. Возвращает сколько добавлено."""
    if cases.questions_column < 0:
        return 0
    added = 0
    for item in cases.items:
        new = questions.get(normalize(item.id), [])
        if not new:
            continue
        cells = split_row(cases.lines[item.row])
        while len(cells) <= cases.questions_column:
            cells.append("")
        cell = cells[cases.questions_column]
        for question in new:
            question = " ".join(question.replace("|", "\\|").split())
            if question and question not in cell:
                cell = f"{cell}<br>{question}" if cell else question
                added += 1
        cells[cases.questions_column] = cell
        cases.lines[item.row] = join_row(cells)
    return added


def normalize(case_id: str) -> str:
    return re.sub(r"[\s*_`]+", "", case_id).upper()


# ---------- документация для пачки ----------

DOCS_CHARS = 16000
DOCS_CHUNK_CHARS = 6000
WORD = re.compile(r"[a-zа-яё0-9]{4,}", re.I)


def load_docs(path: Path | None) -> str:
    if path is None:
        return ""
    files = [path] if path.is_file() else sorted(item for item in path.rglob("*.md") if ".meepo" not in item.parts)
    root = ms.project()
    return "\n\n".join(f"### Файл {file.relative_to(root).as_posix()}\n\n{file.read_text(encoding='utf-8')}"
                       for file in files)


def docs_for(docs: str, text: str) -> str:
    """Документация для пачки: вся, если помещается, иначе куски с наибольшим числом общих слов — в исходном порядке."""
    if len(docs) <= DOCS_CHARS:
        return docs
    words = {word.lower() for word in WORD.findall(text)}
    parts = ms.chunks(docs, DOCS_CHUNK_CHARS)
    scored = sorted(range(len(parts)), key=lambda index: -len(words & {w.lower() for w in WORD.findall(parts[index])}))
    picked, used = [], 0
    for index in scored:
        if used + len(parts[index]) > DOCS_CHARS:
            continue
        picked.append(index)
        used += len(parts[index])
    return "\n\n…\n\n".join(parts[index] for index in sorted(picked))


# ---------- ревью и запуск ----------

DEFAULT_LIMIT = 20
MAX_LIMIT = 200

RULES = """Ты — строгий ревьюер ручных тест-кейсов. Найди ошибки в контенте, структуре и оформлении кейсов этой пачки.
Один тест-кейс = один функциональный блок: валидация полей, ошибки и успешный сценарий — шагами внутри одного кейса.
Номера в колонках «Тестовые данные» и «Ожидаемый результат» — номера шагов, к которым они относятся: это правильный формат."""

WITH_DOCS = RULES + """

Сверь кейсы с фрагментом документации и выпиши замечания со ссылкой на ID кейса:
1. Что из фрагмента документации, относящегося к этим кейсам, не покрыто (case — «покрытие»).
2. Кейсы и шаги, которых нет в документации: выдуманные параметры, пункты меню, значения.
3. Шаги без проверяемого ожидаемого результата или с расплывчатыми ожиданиями («работает корректно», «успешно»).
4. Нарушение группировки (разбитый на части блок) или дубли.
5. Стиль: шаг не начинается с глагола, длинные предложения, канцелярит.
questions — вопросы к требованиям: неясности и противоречия документации, мешающие проверить кейс."""

WITHOUT_DOCS = RULES + """

Документации нет: не суди о полноте покрытия и не выдумывай требования. Выпиши замечания со ссылкой на ID кейса:
1. Шаги без проверяемого ожидаемого результата или с расплывчатыми ожиданиями («работает корректно», «успешно»).
2. Непонятные или неполные предусловия и тестовые данные; шаги, которые нельзя выполнить однозначно.
3. Нарушение группировки (разбитый на части блок) или дубли внутри пачки.
4. Стиль: шаг не начинается с глагола, длинные предложения, канцелярит.
questions — вопросы к требованиям, без ответа на которые кейс нельзя проверить."""

TAIL = "\n\nПиши по-русски, только замечания, без пересказа кейсов. verdict — можно ли отдавать эти кейсы в работу и что поправить в первую очередь."

SCHEMA = {
    "type": "object",
    "properties": {
        "remarks": {"type": "array", "items": {"type": "object", "properties": {
            "case": {"type": "string"}, "issue": {"type": "string"}}, "required": ["case", "issue"]}},
        "questions": {"type": "array", "items": {"type": "object", "properties": {
            "case": {"type": "string"}, "question": {"type": "string"}}, "required": ["case", "question"]}},
        "verdict": {"type": "string"},
    },
    "required": ["remarks", "questions", "verdict"],
}

DUPLICATES = """Ты — ревьюер тест-кейсов. Ниже — список всех кейсов файла: ID и название. Найди дубли и сильные пересечения
(два кейса проверяют один и тот же функциональный блок). Ответ — список строк «ID, ID — почему дубль»; если дублей нет — «Дублей нет». По-русски."""


def review_batch(system: str, text: str, docs: str) -> dict:
    user = (f"<документация>\n{docs}\n</документация>\n\n" if docs else "") + f"<тест-кейсы>\n{text}\n</тест-кейсы>"
    data = ms.ask_json(system, user, SCHEMA)
    return {"remarks": [item for item in data.get("remarks") or [] if isinstance(item, dict)],
            "questions": [item for item in data.get("questions") or [] if isinstance(item, dict)],
            "verdict": str(data.get("verdict") or "")}


def review_file(root: Path, path: Path, docs_path: Path | None, docs: str) -> tuple[str, str, int]:
    """(файл замечаний, вывод, сколько вопросов дописано). Модель недоступна — ms.Unavailable, куски сохранены."""
    relative = path.relative_to(root).as_posix()
    cases = parse(path.read_text(encoding="utf-8"))
    groups = batches(cases)
    system = (WITH_DOCS if docs else WITHOUT_DOCS) + TAIL
    texts = [batch_text(cases, group) for group in groups]
    mark = hashlib.sha256(json.dumps([texts, docs, system, CODE_HASH], ensure_ascii=False).encode("utf-8")).hexdigest()
    state = ms.State(f"review-{relative}")
    if state.get("fingerprint") != mark:
        state.data = {"fingerprint": mark, "batches": [None] * len(groups), "duplicates": None}
        state.save()
    for index, text in enumerate(texts):
        if state.data["batches"][index] is not None:
            continue
        ms.progress(f"{relative}: пачка {index + 1} из {len(texts)}")
        state.data["batches"][index] = review_batch(system, text, docs_for(docs, text))
        state.save()
    if len(groups) > 1 and state.data.get("duplicates") is None:
        ms.progress(f"{relative}: поиск дублей по всему файлу")
        titles = "\n".join(f"{item.id} — {item.text[:160]}" for item in cases.items)
        state.set("duplicates", ms.ask_llm(DUPLICATES, titles))
    report = render(relative, docs_path, root, groups, state.data)
    out = path.with_name(path.stem + ".review.md")
    out.write_text(report, encoding="utf-8")
    questions: dict[str, list[str]] = {}
    for result in state.data["batches"]:
        for item in result["questions"]:
            questions.setdefault(normalize(str(item.get("case", ""))), []).append(str(item.get("question", "")))
    added = fill_questions(cases, questions)
    if added:
        backup = Path(state.path).parent / "backup" / relative
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        path.write_text("\n".join(cases.lines) + "\n", encoding="utf-8")
    verdicts = [result["verdict"] for result in state.data["batches"] if result["verdict"]]
    return out.relative_to(root).as_posix(), verdicts[-1] if verdicts else "", added


def render(relative: str, docs_path: Path | None, root: Path, groups: list, data: dict) -> str:
    docs = docs_path.relative_to(root).as_posix() if docs_path else "не указана — проверены качество, стиль, проверяемость и дубли"
    lines = [f"# Ревью тест-кейсов: {relative}", "", f"- Тест-кейсы: {relative}", f"- Документация: {docs}", ""]
    for index, (group, result) in enumerate(zip(groups, data["batches"], strict=True), start=1):
        span = group[0].id if len(group) == 1 else f"{group[0].id} – {group[-1].id}"
        lines += [f"## Пачка {index} из {len(groups)} ({span})", ""]
        lines += [f"- **{item.get('case', '?')}**: {item.get('issue', '')}" for item in result["remarks"]] or ["Замечаний нет."]
        if result["questions"]:
            lines += ["", "Вопросы к требованиям:"]
            lines += [f"- **{item.get('case', '?')}**: {item.get('question', '')}" for item in result["questions"]]
        lines += ["", f"Вывод: {result['verdict']}", ""]
    if data.get("duplicates"):
        lines += ["## Дубли по всему файлу", "", data["duplicates"], ""]
    return "\n".join(lines)


def case_files(start: Path) -> list[Path]:
    if start.is_file():
        return [start]
    return sorted(path for path in start.rglob("*.md")
                  if not path.name.endswith(".review.md") and ".meepo" not in path.parts and ".git" not in path.parts)


def main() -> int:
    arguments = ms.args()
    root = ms.project()
    limit = arguments.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, str) and limit.strip().isdigit():
        limit = int(limit)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        print(f"limit — целое число от 1 до {MAX_LIMIT}")
        return 2
    try:
        start = ms.inside(arguments.get("cases"), "cases")
        docs_path = ms.inside(arguments["docs"], "docs") if arguments.get("docs") else None
    except ValueError as exc:
        print(exc)
        return 2
    for name, path in (("cases", start), ("docs", docs_path)):
        if path is not None and not path.exists():
            print(f"Нет {name}: {path.relative_to(root).as_posix()}")
            return 2
    docs = load_docs(docs_path)
    files = case_files(start)
    if not files:
        print("Файлов тест-кейсов (.md) не найдено")
        return 2
    lines, total = [], 0
    try:
        for path in files[:limit]:
            out, verdict, added = review_file(root, path, docs_path, docs)
            total += added
            lines.append(f"- {out} — {verdict or 'вывода нет'}" + (f" (вопросов дописано: {added})" if added else ""))
    except ms.Unavailable as exc:
        print("\n".join(["Ревью остановлено.", *lines, f"{exc}. Готовые пачки сохранены, повторный запуск продолжит."]))
        return 1
    except ms.ModelError as exc:
        print("\n".join(["Ревью не закончено.", *lines, f"Модель не справилась с пачкой: {exc}. Повторите запуск."]))
        return 1
    head = f"Ревью тест-кейсов: файлов {len(lines)}" + (f", ещё ждут: {len(files) - limit}" if len(files) > limit else "") + "."
    print("\n".join([head, *lines]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
