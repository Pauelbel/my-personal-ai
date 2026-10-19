"""remember: записать факт в память сразу, по прямой просьбе пользователя («запомни, что…»). Без карточки:
это память о самом пользователе, и он сам попросил."""

from meepo.agent.tools.base import Tool, ToolContext, ToolResult


def remember(args: dict, ctx: ToolContext) -> ToolResult:
    store = ctx.services["memory"]
    content = " ".join(args["content"].split())
    section = " ".join(args["section"].split()) or "Разное"
    if not content:
        raise ValueError("content — факт одной строкой")
    store.add(args["file"], section, content)
    return ToolResult(f"Запомнил в {args['file']}, раздел «{section}»: {content}")


TOOLS = [
    Tool("remember", "Сразу записать в память факт, который пользователь прямо попросил запомнить. Одна запись — "
         "один факт, коротко, в третьем лице («Зовут Павел», «Тест-кейсы оформлять таблицей»).",
         {"type": "object", "properties": {
             "file": {"type": "string", "description": "profile.md — кто он, preferences.md — предпочтения, "
                                                       "projects.md — проекты, solutions.md — успешные решения"},
             "section": {"type": "string", "description": "Раздел файла, например «Обращение» или «Тест-кейсы»"},
             "content": {"type": "string", "description": "Факт одной строкой"},
         }, "required": ["file", "section", "content"], "additionalProperties": False},
         remember),
]
