"""Тест-кейс: промпт и строгая JSON-схема для модели, таблица Markdown и автоматическая проверка стиля."""

import json
import re
from pathlib import Path

CONFIG_DIR = "testgen"
RULES_FILE = "rules.md"
EXAMPLE_FILE = "example.json"
MAX_CASES = 8

EXAMPLE = {
    "title": "Авторизация пользователя",
    "priority": "High",
    "preconditions": ["Сервис запущен.", "Учётная запись admin существует."],
    "steps": [
        {"action": "Откройте страницу входа.", "test_data": "—", "expected": "Открывается форма входа."},
        {"action": "Оставьте поля пустыми и нажмите кнопку «Войти».", "test_data": "Логин = пусто; Пароль = пусто",
         "expected": "У полей отображается сообщение «Обязательное поле». Вход не выполняется."},
        {"action": "Введите логин и неверный пароль, нажмите кнопку «Войти».", "test_data": "Логин = admin; Пароль = wrong",
         "expected": "Отображается сообщение «Неверный логин или пароль». Вход не выполняется."},
        {"action": "Введите логин и верный пароль, нажмите кнопку «Войти».", "test_data": "Логин = admin; Пароль = admin",
         "expected": "Открывается главная страница."},
    ],
    "questions": "",
}

SYSTEM = """Ты — опытный QA-инженер. По фрагменту официальной документации составь ручные функциональные тест-кейсы.

Содержание:
- Опирайся ТОЛЬКО на приведённый фрагмент. Не выдумывай параметры, команды, пункты меню, порты и значения, которых нет в тексте.
- Один тест-кейс — один функциональный блок, например «Авторизация пользователя». Все проверки блока — валидация полей (пустые значения, неверный формат, граничные значения), обработка ошибок и успешный сценарий — пиши шагами внутри одного кейса, а не отдельными кейсами. Не дроби блок на мелкие кейсы.
- title — краткое название проверяемого функционального блока. preconditions — состояние системы и окружения перед началом теста.
- Путь по меню и названия кнопок пиши в action. В test_data пиши только конкретные вводимые значения, валидные и невалидные: «Поле = значение», IP-адреса, логины, пароли, имена файлов, команды CLI, фрагменты конфигов. Если данных для шага нет — test_data = «—».
- expected — результат именно этого шага.
- Негативные проверки добавляй только там, где документация описывает ограничения, ошибки или валидацию.
- questions — неясности, противоречия и пробелы в требованиях, которые мешают проверить блок: сформулируй вопросами. Если вопросов нет — пустая строка.
- Если во фрагменте нечего тестировать (оглавление, справка, лицензия, список ссылок) — верни пустой список.
- Пиши на русском языке."""

SCHEMA = {
    "type": "object",
    "properties": {
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "preconditions": {"type": "array", "items": {"type": "string"}},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "test_data": {"type": "string"},
                                "expected": {"type": "string"},
                            },
                            "required": ["action", "test_data", "expected"],
                        },
                    },
                    "questions": {"type": "string"},
                },
                "required": ["title", "priority", "preconditions", "steps", "questions"],
            },
        }
    },
    "required": ["test_cases"],
}

COLUMNS = ("ID", "Название", "Приоритет", "Предусловия", "Тестовые данные", "Шаги", "Ожидаемый результат", "Вопросы")

# Проверка стиля по правилам ASD-STE100 для русского языка: глагол в начале шага, длина, канцелярит.
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


def system_prompt(root: Path) -> str:
    """Встроенный промпт + правила проекта (testgen/rules.md) + пример кейса (testgen/example.json или встроенный)."""
    config = root / CONFIG_DIR
    rules = config / RULES_FILE
    example_file = config / EXAMPLE_FILE
    example = json.loads(example_file.read_text(encoding="utf-8")) if example_file.is_file() else EXAMPLE
    parts = [SYSTEM]
    if rules.is_file() and rules.read_text(encoding="utf-8").strip():
        parts.append("Правила проекта:\n" + rules.read_text(encoding="utf-8").strip())
    parts.append("Пример правильно оформленного тест-кейса (только формат и стиль, не содержание):\n"
                 + json.dumps(example, ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


def valid(case: object) -> bool:
    # Без вопросов кейс полноценный: сервер без строгой схемы мог опустить пустое поле.
    required = set(SCHEMA["properties"]["test_cases"]["items"]["required"]) - {"questions"}
    if not isinstance(case, dict) or not all(key in case for key in required):
        return False
    steps = case["steps"]
    return isinstance(steps, list) and isinstance(case["preconditions"], list) and all(
        isinstance(step, dict) and all(isinstance(step.get(key), str) for key in ("action", "expected"))
        for step in steps
    )


def _cell(value: object) -> str:
    return " ".join(str(value or "").replace("|", "\\|").split())


def _numbered(steps: list[dict], key: str) -> str:
    items = [f"{index}. {_cell(step.get(key))}" for index, step in enumerate(steps, start=1)
             if _cell(step.get(key)).strip("—- ")]
    return "<br>".join(items) or "—"


def table(cases: list[dict], first: int = 1) -> str:
    """Тестовые данные и ожидаемые результаты пронумерованы как шаги, к которым относятся."""
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for number, case in enumerate(cases, start=first):
        row = (
            f"TC-{number:03d}", _cell(case["title"]), _cell(case["priority"]),
            "<br>".join(_cell(item) for item in case["preconditions"] if _cell(item)) or "—",
            _numbered(case["steps"], "test_data"), _numbered(case["steps"], "action"),
            _numbered(case["steps"], "expected"),
            # Вопросов нет — столбец пустой, а не «—»: так видно, где они действительно есть.
            _cell(case.get("questions")),
        )
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def lint(cases: list[dict], first: int = 1) -> list[str]:
    issues: list[str] = []
    for number, case in enumerate(cases, start=first):
        for index, step in enumerate(case["steps"], start=1):
            where = f"TC-{number:03d}, шаг {index}"
            word = (step["action"].split() or [""])[0].strip("«»\"'.,:")
            if not IMPERATIVE.search(word) and not step["action"].startswith("Если"):
                issues.append(f"{where}: шаг не начинается с глагола («{word}»)")
            for name in ("action", "expected"):
                for sentence in re.split(r"(?<=[.!?])\s+", step[name]):
                    if len(sentence.split()) > MAX_SENTENCE_WORDS:
                        issues.append(f"{where}: в {name} предложение длиннее {MAX_SENTENCE_WORDS} слов")
                for match in BANNED_RE.finditer(BANNED_OK.sub("", step[name])):
                    issues.append(f"{where}: в {name} запрещённое слово «{match.group(1)}…»")
    return issues
