"""Разбор файла тест-кейсов на пункты и пачки, выбор кусков документации для пачки, колонка «Вопросы»."""

import re
from dataclasses import dataclass
from pathlib import Path

import meepo_skill as ms

BATCH_CHARS = 12000
BATCH_CASES = 10
DOCS_CHARS = 16000
DOCS_CHUNK_CHARS = 6000
WORD = re.compile(r"[a-zа-яё0-9]{4,}", re.I)


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
