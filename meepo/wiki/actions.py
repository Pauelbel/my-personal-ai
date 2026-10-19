"""Действия вики — ваши проверки в меню «Действия ▾» чата вики: название и что искать.

Файл в папке данных, по разделу на действие:

    ## Проверить вики
    найди противоречия между заметками, устаревшее и дубли, которые стоит слить

Пока файла нет, в меню встроенное действие. Остальное в проверке не правится: агент сам ничего
не меняет и показывает список правок с галочками — на этом держится карточка.
"""

from pathlib import Path

DEFAULT_ACTIONS = [
    {"title": "Проверить вики", "text": "найди противоречия между заметками, устаревшее и дубли, которые стоит слить"},
]
MAX_ACTIONS = 20
MAX_TITLE = 60
MAX_TEXT = 1000


class WikiActions:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> list[dict]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [dict(action) for action in DEFAULT_ACTIONS]
        actions: list[dict] = []
        for line in content.splitlines():
            if line.startswith("## "):
                actions.append({"title": line[3:].strip(), "text": ""})
            elif actions:
                actions[-1]["text"] += line + "\n"
        return [{"title": action["title"], "text": action["text"].strip()} for action in actions]

    def write(self, actions: list[dict]) -> list[dict]:
        if len(actions) > MAX_ACTIONS:
            raise ValueError(f"Действий — не больше {MAX_ACTIONS}")
        clean = []
        for number, action in enumerate(actions, start=1):
            title = " ".join(str(action.get("title") or "").split())
            text = str(action.get("text") or "").replace("\r\n", "\n").strip()
            if not title or not text:
                raise ValueError(f"Действие {number}: заполните название и что искать")
            if len(title) > MAX_TITLE or len(text) > MAX_TEXT:
                raise ValueError(f"Действие {number}: название — до {MAX_TITLE} символов, текст — до {MAX_TEXT}")
            if any(line.startswith("## ") for line in text.splitlines()):
                raise ValueError(f"Действие {number}: строка текста не может начинаться с «## »")
            if title in (item["title"] for item in clean):
                raise ValueError(f"Действие «{title}» уже есть")
            clean.append({"title": title, "text": text})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(f"## {action['title']}\n{action['text']}\n\n" for action in clean), encoding="utf-8", newline="\n",
        )
        return clean
