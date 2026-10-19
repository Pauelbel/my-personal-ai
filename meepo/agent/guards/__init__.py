"""Заплатки для маленькой модели. Каждая — свой файл с функцией check(state) -> str | None и переключателем
в data/settings.json → guards. Ответные (language, plan, promise) проверяют итоговый текст хода; retry — сбой
запроса к модели (пустой ответ или зацикливание)."""

from meepo.agent.guards import language, plan, promise, retry

# Порядок важен: сначала язык (ответ переписывается целиком), затем план, затем обещание.
ANSWER_GUARDS = {"language": language, "plan": plan, "promise": promise}
LOG = {"language": "ответ не по-русски — прошу переписать", "plan": "в плане есть несделанные пункты — прошу продолжить",
       "promise": "модель пообещала действие без вызова инструмента — прошу сделать вызов"}

__all__ = ["ANSWER_GUARDS", "LOG", "answer_hint", "retry"]


def answer_hint(state) -> tuple[str, str] | None:
    """(имя guard, подсказка модели) — первый сработавший включённый guard; None — ответ принимается."""
    for name, guard in ANSWER_GUARDS.items():
        if state.guards.get(name, True) and (hint := guard.check(state)):
            return name, hint
    return None
