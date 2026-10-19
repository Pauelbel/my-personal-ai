"""Шаги агента: что нужно от пользователя и шаги по порядку. Разбор steps.md — steps/files.py, выполнение — steps/engine.py."""

from dataclasses import dataclass, field

# Виды шагов: model — один ответ модели без инструментов; agent — модель с инструментами, пока не закончит;
# tool — один вызов инструмента с заданными параметрами; command — команда в папке проекта;
# script — скрипт из папки агента (scripts/) в песочнице.
STEP_KINDS = ("model", "agent", "tool", "command", "script")
STEP_NAMES = {"model": "модель", "agent": "агент", "tool": "инструмент", "command": "команда", "script": "скрипт"}
# Что делать, если шаг не получился: остановить работу, идти дальше или вернуться к шагу и повторить.
ON_ERROR = ("stop", "continue", "retry")


@dataclass(frozen=True)
class StepInput:
    name: str
    hint: str = ""
    optional: bool = False


@dataclass(frozen=True)
class Step:
    title: str
    kind: str
    # model и agent — задание модели; command — команда; tool — id инструмента; script — имя скрипта в scripts/.
    text: str
    # Файлы и папки проекта, которые шаг получает целиком (в пределах окна модели).
    files: tuple[str, ...] = ()
    # Куда записать итог шага (ответ модели или вывод команды).
    save: str = ""
    # Только model: документ проекта, который шаг проходит по частям — задание выполняется для каждой части
    # отдельно, ответы подряд пишутся в save.
    parts: str = ""
    # Только agent: инструменты шага; «браузер» и «файлы» — наборы.
    tools: tuple[str, ...] = ()
    # tool и script: параметры вызова.
    params: dict = field(default_factory=dict)
    on_error: str = "stop"
    # on_error == "retry": к какому шагу (номер с нуля) вернуться и сколько раз пробовать.
    retry_to: int | None = None
    attempts: int = 0
    timeout: int = 0
    rounds: int = 0


@dataclass(frozen=True)
class Steps:
    inputs: tuple[StepInput, ...]
    steps: tuple[Step, ...]
    # Текст до первого раздела: что получится в итоге, своими словами.
    about: str = ""

    def view(self) -> dict:
        """Шаги для интерфейса: карточка агента, карточка запуска и предложение агента рисуют их по нему."""
        return {
            "about": self.about,
            "inputs": [{"name": item.name, "hint": item.hint, "optional": item.optional} for item in self.inputs],
            "steps": [
                {
                    "title": step.title, "kind": step.kind, "text": step.text, "files": list(step.files),
                    "save": step.save, "parts": step.parts, "tools": list(step.tools), "params": step.params,
                    "on_error": step.on_error, "attempts": step.attempts,
                    "retry_to": None if step.retry_to is None else self.steps[step.retry_to].title,
                }
                for step in self.steps
            ],
        }

    def effects(self) -> list[dict]:
        """Что шаги делают с проектом и системой — пользователь видит это до запуска: текст и, если есть, код
        (команда целиком, пути файлов)."""
        saves = [step.save for step in self.steps if step.save]
        tools = {name.casefold() for step in self.steps for name in step.tools}
        found = [{"text": "Запускает команду в папке проекта", "code": step.text}
                 for step in self.steps if step.kind == "command"]
        found += [{"text": "Запускает скрипт агента в песочнице: он может менять файлы проекта и обращаться к модели",
                   "code": step.text} for step in self.steps if step.kind == "script"]
        found += [{"text": "Проходит документ по частям — запрос к модели на каждую часть; это может быть долго",
                   "code": step.parts} for step in self.steps if step.parts]
        if tools & {"файлы", "files", "write_file", "edit_file"} or saves:
            found.append({"text": "Создаёт и меняет файлы в папке проекта", "code": "\n".join(saves)})
        if tools & {"браузер", "browser"}:
            found.append({"text": "Открывает сайты в браузере и действует на них", "code": ""})
        if tools & {"интернет", "поиск", "search"}:
            found.append({"text": "Ищет в интернете", "code": ""})
        found += [{"text": f"Вызывает инструмент {step.text}", "code": ""} for step in self.steps if step.kind == "tool"]
        return found
