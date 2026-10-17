"""Разбор steps.md — шагов скилла в Markdown. Имя и описание у скилла в SKILL.md, здесь только работа:

    Свободный текст: что получится в итоге (необязательно).

    ## Нужно от вас
    - сайт — адрес сайта
    - шаблон — ссылка на репозиторий с шаблоном
    - документ — файл с требованиями

    ## Скачать шаблон
    команда: git clone --depth 1 {шаблон} .meepo/template

    ## Выписать правила шаблона
    модель: Прочитай файлы шаблона и выпиши …
    файлы: .meepo/template
    сохранить: .meepo/rules.md

    ## Выписать требования
    модель: Выпиши требования из этой части документа списком.
    по частям: {документ}
    сохранить: .meepo/requirements.md

Раздел «## …» — шаг, его заголовок — название шага. Строка «ключ: значение» с известным ключом — настройка шага,
остальные строки продолжают задание модели (или команду). {имя} подставляет значение из «Нужно от вас».
«по частям: файл» — шаг-модель проходит большой документ по частям: задание для каждой части отдельно,
ответы подряд — в файл «сохранить».
"""

import json
import re
from pathlib import Path

from local_agent.steps.models import Step, StepInput, Steps

# Ключи строк шага; у каждого есть русское и английское имя.
STEP_KEYS = {
    "модель": "model", "model": "model",
    "агент": "agent", "agent": "agent",
    "инструмент": "tool", "tool": "tool",
    "команда": "command", "command": "command",
    "скрипт": "script", "script": "script",
    "файлы": "files", "files": "files",
    "сохранить": "save", "save": "save",
    "по частям": "parts", "parts": "parts",
    "инструменты": "tools", "tools": "tools",
    "параметры": "params", "params": "params",
    "при ошибке": "on_error", "on error": "on_error", "on_error": "on_error",
    "таймаут": "timeout", "timeout": "timeout",
    "шагов": "rounds", "rounds": "rounds",
}
KINDS = ("model", "agent", "tool", "command", "script")
KEY_LINE = re.compile(r"^([^\W\d_][\w ]{0,20}?)\s*:(?:\s(.*)|)$")
PLACEHOLDER = re.compile(r"\{([^\W\d][\w-]*)\}")
INPUT_LINE = re.compile(r"^[-*]\s+`?([^\W\d][\w-]*)`?\s*(?:(?:—|–|-|:)\s*(.*))?$")
INPUT_HEADINGS = ("нужно", "входные", "что нужно", "inputs")
STEP_PREFIX = re.compile(r"^(?:шаг\s*\d*\s*[.:)—-]?\s*|\d+\s*[.)]\s*)", re.IGNORECASE)
TOOL_ID = re.compile(r"^[a-z0-9_]{2,64}$")
# Скрипт — файл .py прямо в папке scripts/ скилла.
SCRIPT_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.py$")
STOP_WORDS = {"стоп", "остановить", "stop"}
CONTINUE_WORDS = {"дальше", "продолжить", "continue", "пропустить"}
DEFAULT_ATTEMPTS = 2
MAX_ATTEMPTS = 5
MAX_TIMEOUT = 3600
MAX_ROUNDS = 100


def parse_steps(text: str, where: Path | str = "steps.md") -> Steps:
    """Разбирает и проверяет шаги целиком: ошибка — ValueError с местом и понятной причиной."""
    where = Path(where)
    text = text.replace("\r\n", "\n")
    about, sections = _sections(text)
    inputs: list[StepInput] = []
    raw_steps: list[tuple[str, list[str]]] = []
    for title, lines in sections:
        if title.casefold().startswith(INPUT_HEADINGS):
            if inputs:
                raise ValueError(f"{where}: раздел «Нужно от вас» должен быть один")
            inputs = _inputs(where, lines)
        else:
            raw_steps.append((title, lines))
    if not raw_steps:
        raise ValueError(f"{where}: нет ни одного шага — каждый шаг начинается заголовком «## Название шага»")
    titles = [_step_title(title) for title, _ in raw_steps]
    names = {item.name for item in inputs}
    steps = tuple(
        _step(where, index, titles, lines, names) for index, (_, lines) in enumerate(raw_steps)
    )
    return Steps(inputs=tuple(inputs), steps=steps, about=about)


def _sections(body: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Текст до первого «## » и разделы: (заголовок, строки). Заголовки глубже (###) — часть текста шага."""
    about: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        elif not line.startswith("# "):
            about.append(line)
    return "\n".join(about).strip(), sections


def _inputs(where: Path, lines: list[str]) -> list[StepInput]:
    inputs: list[StepInput] = []
    for line in lines:
        if not line.strip():
            continue
        match = INPUT_LINE.match(line.strip())
        if match is None:
            raise ValueError(f"{where}: в «Нужно от вас» строка «{line.strip()}» — пишите «- имя — что это»")
        name, hint = match[1], (match[2] or "").strip()
        if any(item.name == name for item in inputs):
            raise ValueError(f"{where}: «{name}» в «Нужно от вас» указано дважды")
        optional = "необязательно" in hint.casefold() or "optional" in hint.casefold()
        hint = re.sub(r"\s*\((?:необязательно|optional)\)\s*", " ", hint, flags=re.IGNORECASE).strip()
        inputs.append(StepInput(name=name, hint=hint, optional=optional))
    return inputs


def _step_title(heading: str) -> str:
    return STEP_PREFIX.sub("", heading).strip() or heading.strip()


def _step(where: Path, index: int, titles: list[str], lines: list[str], names: set[str]) -> Step:
    title = titles[index]
    place = f"{where}, шаг {index + 1} «{title}»"
    values: dict[str, str] = {}
    kind = ""
    value = ""
    # Строки без ключа по порядку: у модели и агента это задание, у команды и инструмента — комментарий.
    text: list[str] = []
    fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fence = not fence
        match = None if fence else KEY_LINE.match(line.strip())
        key = STEP_KEYS.get(" ".join(match[1].casefold().split())) if match else None
        if key is None:
            text.append(line)
            continue
        if key in KINDS:
            if kind:
                raise ValueError(f"{place}: у шага два вида — {kind_name(kind)} и {kind_name(key)}; оставьте один")
            kind, value = key, (match[2] or "").strip()
            text.append(value)
            continue
        if key in values:
            raise ValueError(f"{place}: «{match[1].strip()}» указано дважды")
        values[key] = (match[2] or "").strip()
    if not kind:
        raise ValueError(
            f"{place}: не указано, что делает шаг — строка «модель: задание», «агент: задание», "
            "«команда: …», «инструмент: id» или «скрипт: имя.py»"
        )
    if kind == "tool":
        body = value
        if not TOOL_ID.fullmatch(body):
            raise ValueError(f"{place}: после «инструмент:» — id инструмента, например jira")
    elif kind == "script":
        body = value
        if not SCRIPT_NAME.fullmatch(body):
            raise ValueError(f"{place}: после «скрипт:» — имя файла .py из папки scripts скилла, например testcases.py")
    elif kind == "command":
        # Команда — в строке «команда: …» или, если она длинная, в блоке ``` сразу под ней.
        block = re.search(r"```[^\n]*\n(.*?)```", "\n".join(text), re.S)
        body = value or (block[1].strip() if block else "")
        if not body:
            raise ValueError(f"{place}: пустая команда — пишите «команда: pytest -q»")
    else:
        body = "\n".join(text).strip()
        if not body:
            raise ValueError(f"{place}: пустое задание — что модели сделать на этом шаге")
    if kind == "agent" and not values.get("tools"):
        raise ValueError(f"{place}: агенту нужны инструменты — строка «инструменты: браузер, файлы»")
    if values.get("tools") and kind != "agent":
        raise ValueError(f"{place}: «инструменты» бывают только у шага-агента")
    if values.get("params") and kind not in ("tool", "script"):
        raise ValueError(f"{place}: «параметры» бывают только у шага-инструмента и шага-скрипта")
    params = _params(place, values.get("params", ""))
    files = tuple(item for item in _items(values.get("files", "")))
    save = values.get("save", "").strip()
    parts = values.get("parts", "").strip().strip("`")
    if "parts" in values and kind != "model":
        raise ValueError(f"{place}: «по частям» бывает только у шага-модели")
    if "parts" in values and not parts:
        raise ValueError(f"{place}: после «по частям:» — файл документа, например {{документ}}")
    if parts and not save:
        raise ValueError(f"{place}: результат по частям большой — укажите, куда его записать: «сохранить: файл»")
    # Опечатка в {имени} должна быть видна сразу, а не уйти модели или в файл «{сйат}». В блоках ``` задания
    # фигурные скобки могут быть кодом — их не проверяем.
    prompt = re.sub(r"```.*?(```|$)", "", body, flags=re.S) if kind in ("model", "agent") else body
    checked = [*files, save, parts, *_strings(params), prompt]
    for value in checked:
        for name in PLACEHOLDER.findall(value):
            if name not in names:
                raise ValueError(f"{place}: {{{name}}} — такого нет в «Нужно от вас»")
    on_error, retry_to, attempts = _on_error(place, values.get("on_error", ""), index, titles)
    return Step(
        title=title, kind=kind, text=body, files=files, save=save, parts=parts,
        tools=tuple(_items(values.get("tools", ""))), params=params,
        on_error=on_error, retry_to=retry_to, attempts=attempts,
        timeout=_number(place, "таймаут", values.get("timeout", ""), MAX_TIMEOUT),
        rounds=_number(place, "шагов", values.get("rounds", ""), MAX_ROUNDS),
    )


def _params(place: str, raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError(f'{place}: параметры — JSON-объект в одну строку, например {{"path": "{{папка}}"}}') from None
    if not isinstance(value, dict):
        raise ValueError(f"{place}: параметры — JSON-объект")
    return value


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _on_error(place: str, raw: str, index: int, titles: list[str]) -> tuple[str, int | None, int]:
    value = raw.strip().strip(".")
    if not value or value.casefold() in STOP_WORDS:
        return "stop", None, 0
    if value.casefold() in CONTINUE_WORDS:
        return "continue", None, 0
    target, attempts = value, DEFAULT_ATTEMPTS
    if "," in value:
        target, count = (part.strip() for part in value.rsplit(",", 1))
        digits = re.sub(r"\D", "", count)
        if not digits:
            raise ValueError(f"{place}: «при ошибке: шаг, число попыток» — после запятой нужно число")
        attempts = int(digits)
    if not 1 <= attempts <= MAX_ATTEMPTS:
        raise ValueError(f"{place}: попыток — от 1 до {MAX_ATTEMPTS}")
    target = target.strip().strip("«»\"'").strip()
    number = re.fullmatch(r"(?:шаг\s*)?(\d+)", target, re.IGNORECASE)
    if number:
        to = int(number[1]) - 1
    else:
        found = [position for position, title in enumerate(titles) if title.casefold() == target.casefold()]
        if not found:
            raise ValueError(
                f"{place}: «при ошибке: {raw.strip()}» — шага «{target}» нет; пишите «стоп», «дальше» "
                "или «название шага, число попыток»"
            )
        to = found[0]
    if not 0 <= to <= index:
        raise ValueError(f"{place}: при ошибке можно вернуться только к этому шагу или к одному из прежних")
    return "retry", to, attempts


def _number(place: str, name: str, raw: str, limit: int) -> int:
    if not raw.strip():
        return 0
    digits = re.sub(r"\D", "", raw)
    if not digits or not 1 <= int(digits) <= limit:
        raise ValueError(f"{place}: {name} — число от 1 до {limit}")
    return int(digits)


def _items(value: str) -> list[str]:
    return [item.strip().strip("`") for item in value.split(",") if item.strip().strip("`")]


def kind_name(kind: str) -> str:
    return {"model": "модель", "agent": "агент", "tool": "инструмент", "command": "команда", "script": "скрипт"}[kind]
