"""Большой текстовый документ: оглавление по заголовкам Markdown и деление на части, каждая из которых помещается
в окно модели. Части режутся по заголовкам, а если раздел больше части — по абзацам, затем по строкам."""

import math
import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Больше этого документ не читается даже по частям: это уже не текст, а дамп.
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
# Сколько заголовков показывает оглавление; при большем числе остаются верхние уровни.
OUTLINE_LINES = 150


def tokens(text: str) -> int:
    """Та же грубая оценка, что у сборщика контекста: около 4 байт UTF-8 на токен."""
    return math.ceil(len(text.encode("utf-8")) / 4)


@dataclass(frozen=True)
class Heading:
    line: int
    level: int
    title: str


@dataclass(frozen=True)
class Part:
    # Строки документа с 1, включительно.
    start: int
    end: int
    # Раздел, в котором часть начинается: «Глава 2 › Вход по паролю».
    title: str
    text: str


def headings(lines: list[str]) -> list[Heading]:
    """Заголовки Markdown вне блоков кода."""
    found: list[Heading] = []
    fence = False
    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if not fence and (match := HEADING.match(line)):
            found.append(Heading(number, len(match[1]), match[2].strip()))
    return found


def outline(text: str, limit: int = OUTLINE_LINES) -> str:
    """Оглавление строками «строка N: заголовок» с отступом по уровню. Не влезает в limit — мелкие уровни убираются;
    но если верхних заголовков совсем мало (одна «# Спецификация»), лучше следующий уровень, обрезанный по limit."""
    found = headings(text.splitlines())
    if not found:
        return ""
    levels = sorted({item.level for item in found})
    depth = len(levels)
    while depth > 1 and sum(1 for item in found if item.level in levels[:depth]) > limit:
        depth -= 1
    shown = [item for item in found if item.level in levels[:depth]]
    if depth < len(levels) and len(shown) < limit // 3:
        shown = [item for item in found if item.level in levels[:depth + 1]]
    top = min(item.level for item in shown)
    lines = [f"{'  ' * (item.level - top)}строка {item.line}: {item.title}" for item in shown[:limit]]
    if len(shown) > limit:
        lines.append(f"… и ещё {len(shown) - limit} заголовков, следующий — на строке {shown[limit].line}")
    elif len(shown) < len(found):
        lines.append(f"(показаны верхние уровни: {len(shown)} из {len(found)} заголовков)")
    return "\n".join(lines)


def split_parts(text: str, max_tokens: int) -> list[Part]:
    """Части не больше max_tokens. Граница части — по возможности перед заголовком, иначе после пустой строки
    (во второй половине части), иначе там, где часть заполнилась. Пустые части не возвращаются."""
    max_tokens = max(max_tokens, 50)
    # Строки — как у read_file (splitlines): номера частей совпадают с offset при чтении кусками.
    lines = text.splitlines()
    marks = {item.line: item for item in headings(lines)}
    # Строки длиннее половины части режутся на куски — у всех кусков номер исходной строки.
    items: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        cost = tokens(line)
        if cost <= max_tokens // 2:
            items.append((number, line))
            continue
        step = max(int(len(line) * (max_tokens // 2) / cost), 1)
        items += [(number, line[start:start + step]) for start in range(0, len(line), step)]
    path: dict[int, str] = {}
    titles: dict[int, str] = {}
    for position, (number, _) in enumerate(items):
        if number in marks and (position == 0 or items[position - 1][0] != number):
            heading = marks[number]
            path = {level: title for level, title in path.items() if level < heading.level}
            path[heading.level] = heading.title
        titles[position] = " › ".join(path[level] for level in sorted(path))

    parts: list[Part] = []
    start, used = 0, 0
    for position, (_, line) in enumerate(items):
        cost = tokens(line) + 1
        while position > start and used + cost > max_tokens:
            cut = _cut(items, marks, start, position)
            parts.append(_part(items, titles, start, cut))
            start = cut
            used = sum(tokens(items[index][1]) + 1 for index in range(start, position))
        used += cost
    if start < len(items):
        parts.append(_part(items, titles, start, len(items)))
    return [part for part in parts if part.text.strip()]


def _cut(items: list[tuple[int, str]], marks: dict, start: int, end: int) -> int:
    """Где закончить часть items[start:end]: перед заголовком или после пустой строки во второй половине."""
    middle = start + (end - start) // 2
    for index in range(end - 1, middle, -1):
        number = items[index][0]
        if number in marks and items[index - 1][0] != number:
            return index
    for index in range(end - 1, middle - 1, -1):
        if not items[index][1].strip():
            return index + 1
    return end


def _part(items: list[tuple[int, str]], titles: dict[int, str], start: int, end: int) -> Part:
    chunk = items[start:end]
    text = "\n".join(line for _, line in chunk).strip("\n")
    return Part(chunk[0][0], chunk[-1][0], titles.get(start, ""), text)
