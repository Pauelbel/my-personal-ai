"""Поиск по долговременной памяти: каждая запись «- текст» — отдельный кусок полнотекстового индекса.

В промпт попадает не вся память, а короткий профиль и несколько записей, найденных по сообщению.
"""

import re
from pathlib import Path

from local_agent.shared.markdown_index import MarkdownIndex

ENTRY = re.compile(r"^- (.+)$")
SECTION = re.compile(r"^##\s+(.+?)\s*$")
# Частые слова совпадают почти с любой записью и только шумят в выдаче.
STOPWORDS = {
    "что", "как", "это", "так", "для", "или", "все", "всё", "ещё", "еще", "уже", "мне", "меня", "мой", "моя",
    "мои", "ты", "вы", "тебя", "тебе", "его", "она", "они", "оно", "был", "была", "было", "быть", "есть",
    "нет", "да", "вот", "там", "тут", "где", "когда", "кто", "чем", "чтобы", "если", "надо", "нужно",
    "можно", "давай", "пожалуйста", "привет", "спасибо", "только", "очень", "сейчас", "потом", "the", "and",
    "you", "for", "this", "that", "with",
}


def entry_chunks(text: str, fallback_title: str) -> list[tuple[str, int, str]]:
    """Записи файла памяти: заголовок куска — раздел, текст — сама запись."""
    section = fallback_title
    chunks = []
    for number, line in enumerate(text.splitlines(), start=1):
        if heading := SECTION.match(line):
            section = heading.group(1)
        elif entry := ENTRY.match(line.strip()):
            chunks.append((section, number, entry.group(1).strip()))
    return chunks


def search_terms(text: str) -> str:
    words = [word for word in re.findall(r"\w+", text.casefold()) if word not in STOPWORDS]
    return " ".join(words)


class MemorySearch:
    def __init__(self, memory_root: Path, index_path: Path) -> None:
        self._index = MarkdownIndex(memory_root, index_path, entry_chunks)

    def search(self, query: str, limit: int = 5) -> list[tuple[str, str, str]]:
        """(файл, раздел, запись) по ключевым словам запроса."""
        rows = self._index.search(search_terms(query), limit=limit)
        return [(path, title, text) for path, title, _line, text in rows]
