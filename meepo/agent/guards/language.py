"""Guard «язык»: маленькая модель срывается на английский — обычно после английских страниц, файлов и снимков браузера.

Как ловит: итоговый ответ в основном на латинице (код, ссылки и `идентификаторы` не считаются), а пользователь
сам не пишет не по-русски и не просил другой язык. Тогда ответ не сохраняется, модель просят переписать его
по-русски. Срабатывает не больше MAX раз за ход: дальше ответ принимается как есть.
"""

import re

MAX = 3
HINT = (
    "Ты ответил не по-русски. Перепиши свой последний ответ по-русски целиком, ничего не меняя по смыслу и не "
    "вызывая инструментов; код, команды, названия и цитаты оставь как есть."
)


def is_foreign(text: str, minimum: int = 60) -> bool:
    """minimum — сколько латинских букв нужно, чтобы судить: в короткой реплике это «OK» или название."""
    prose = re.sub(r"```.*?(```|$)|`[^`]*`|https?://\S+|\S+\.[a-z]{1,5}\b", " ", text, flags=re.S)
    latin = len(re.findall(r"[A-Za-z]", prose))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", prose))
    return latin >= minimum and cyrillic < latin * 0.25


def foreign_allowed(user_texts: list[str]) -> bool:
    """Пользователь сам пишет не по-русски или просил другой язык — тогда ответ на нём законен."""
    if any(re.search(r"английск|по-англ|in english|на другом языке|по-немец|по-француз|translate", text, re.I)
           for text in user_texts):
        return True
    return bool(user_texts) and is_foreign(user_texts[-1], minimum=20)


def check(state) -> str | None:
    if state.counts.get("language", 0) >= MAX:
        return None
    if not is_foreign(state.answer) or foreign_allowed(state.user_texts()):
        return None
    return HINT
