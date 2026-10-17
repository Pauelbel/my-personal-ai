"""Проверка стиля тест-кейсов по правилам ASD-STE100 для русского языка: глагол в начале шага, длина, канцелярит."""

import re

MAX_SENTENCE_WORDS = 20
BANNED = [
    "осуществ", "произв", "выполнить нажатие", "данн", "являет", "в рамках", "соответствующ",
    "успешн", "корректн", "на сегодняшний день", "необходимо",
]
BANNED_RE = re.compile(r"(?i)(?<![а-яё])(" + "|".join(BANNED) + r")")
# «данные» (тестовые данные, данные кэша) — обычное слово, а не канцелярское «данный».
BANNED_OK = re.compile(r"(?i)(?<![а-яё])данн(ые|ых|ым|ыми)(?![а-яё])")
# Повелительное наклонение на «вы»: «Откройте», «Убедитесь».
IMPERATIVE = re.compile(r"(те|тесь)$")


def lint(cases: list[dict]) -> list[str]:
    issues: list[str] = []
    for number, case in enumerate(cases, start=1):
        for index, step in enumerate(case["steps"], start=1):
            where = f"кейс {number}, шаг {index}"
            first = (step["action"].split() or [""])[0].strip("«»\"'.,:")
            if not IMPERATIVE.search(first) and not step["action"].startswith("Если"):
                issues.append(f"{where}: шаг не начинается с глагола («{first}»)")
            for name in ("action", "expected"):
                for sentence in re.split(r"(?<=[.!?])\s+", step[name]):
                    if len(sentence.split()) > MAX_SENTENCE_WORDS:
                        issues.append(f"{where}: в {name} предложение длиннее {MAX_SENTENCE_WORDS} слов")
                for match in BANNED_RE.finditer(BANNED_OK.sub("", step[name])):
                    issues.append(f"{where}: в {name} запрещённое слово «{match.group(1)}…»")
    return issues
