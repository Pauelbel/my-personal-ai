"""Guard «пообещал и не сделал»: маленькая модель пишет «Перехожу в корзину.» и не вызывает инструмент.

Текст без вызова заканчивает ход, и пользователю пришлось бы писать «продолжай». Как ловит: в ходе уже были
вызовы инструментов, инструменты ещё предлагаются, а последняя строка ответа — обещание действия (глагол первого
лица «Проверяю», «Перейду», «Теперь…», строка с двоеточием на конце), а не итог и не вопрос. Тогда текст остаётся
в чате промежуточной репликой, а модель просят сделать шаг вызовом. Не больше MAX раз за ход.
"""

import re

MAX = 3
HINT = (
    "Ты написал, что делаешь следующий шаг, но не вызвал инструмент — так ход закончится и работа встанет. "
    "Сделай этот шаг сейчас вызовом инструмента. Если задача уже выполнена целиком — напиши итог; "
    "если без ответа пользователя дальше нельзя — задай вопрос."
)
NEXT_STEP_WORDS = {"сейчас", "теперь", "далее", "дальше", "следующий", "следующим", "затем", "потом", "приступаю"}
# Вводные слова перед обещанием: «Для начала я перейду на сайт», «Итак, проверю корзину».
LEAD_WORDS = {"для", "начала", "сначала", "итак", "хорошо", "отлично", "ок", "я", "и", "а", "так", "что", "ж", "теперь", "сейчас"}
# Первое лицо, но не действие: ждёт пользователя, предлагает или подводит итог.
NOT_ACTIONS = {
    "жду", "могу", "предлагаю", "рекомендую", "надеюсь", "прошу", "благодарю", "хочу", "думаю", "считаю",
    "вижу", "советую", "напомню", "готову", "остаюсь", "уточню", "подытожу", "резюмирую",
    "сообщу", "доложу", "отвечу", "вернусь", "дождусь", "подожду", "оповещу", "уведомлю",
}


def promises_action(text: str) -> bool:
    """Ответ кончается обещанием действия («Заполняю данные.», «Теперь проверю корзину»), а не итогом или вопросом."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1].lstrip("#*->•0123456789.) ").rstrip("*_ ")
    if not last or last.endswith("?"):
        return False
    if last.endswith(":"):
        return True
    words = re.findall(r"[а-яё]+", last.casefold())
    if not words:
        return False
    if words[0] in NEXT_STEP_WORDS - LEAD_WORDS:
        return True
    lead = next((index for index, word in enumerate(words) if word not in LEAD_WORDS), len(words))
    if lead == len(words) or len(words) > 16:
        return False
    if lead and words[0] in NEXT_STEP_WORDS:
        return True
    verb = words[lead]
    return verb.endswith(("ю", "у")) and len(verb) >= 4 and verb not in NOT_ACTIONS


def check(state) -> str | None:
    if state.counts.get("promise", 0) >= MAX or not state.rounds or state.stopped:
        return None
    return HINT if promises_action(state.answer) else None
