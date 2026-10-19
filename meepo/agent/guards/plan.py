"""Guard «план задачи»: маленькая модель на многочастной задаче делает часть и пишет итог, забыв остальное.

Как ловит: в плане, записанном в этом ходе (plan set), остались несделанные пункты, инструменты ещё предлагаются,
а ответ — не вопрос пользователю (вопрос ход заканчивает: без ответа дальше нельзя). Тогда текст остаётся в чате
промежуточной репликой, а модель просят сделать следующий пункт. Не больше MAX раз за ход.
"""

from meepo.agent.tools.plan import open_steps

MAX = 4


def hint(remaining: list[tuple[int, str]]) -> str:
    steps = "; ".join(f"{index}. {text}" for index, text in remaining)
    return (f"В плане ещё не сделано: {steps}. Сделай пункт {remaining[0][0]} сейчас — вызовом инструмента, "
            "и отметь его через plan done. Если пункт сделать нельзя — отметь его done с причиной в note "
            "или задай пользователю вопрос.")


def check(state) -> str | None:
    if state.counts.get("plan", 0) >= MAX or state.stopped or state.answer.rstrip().endswith("?"):
        return None
    session = state.session()
    if session.get("plan_request") != state.request_id:
        return None
    remaining = open_steps(session.get("plan") or [])
    return hint(remaining) if remaining else None
