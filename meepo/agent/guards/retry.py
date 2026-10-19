"""Guard «повтор»: маленькая модель иногда отвечает пустотой (ушла в рассуждения) или зацикливается —
по кругу пишет одно и то же (клиент ловит это на лету и обрывает поток).

Как ловит: запрос к модели закончился LLMEmpty или LLMRepetition (state.failure). Тогда тот же запрос
повторяется, второй повтор — без инструментов (state.without_tools), дальше — ошибка хода. Счётчик —
на каждый запрос к модели: удачный ответ его сбрасывает. Выключен — ошибка сразу.
"""

from meepo.llm.client import LLMRepetition

MAX = 2


def check(state) -> str | None:
    tries = state.counts.get("retry", 0)
    if tries >= MAX:
        return None
    problem = "зациклилась" if isinstance(state.failure, LLMRepetition) else "вернула пустой ответ"
    # Второй повтор — без инструментов: часто модель застревает именно на выборе вызова.
    state.without_tools = tries + 1 == MAX
    return f"Модель {problem} — повторяю" + (" без инструментов" if state.without_tools else "")
