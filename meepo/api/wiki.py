"""Раздел «Вики»: список заметок, граф, поиск, чтение и ручная правка; чат вики (служебная сессия); картинки чата."""

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from meepo.wiki import notes
from meepo.wiki.agent import WIKI_CONTEXT_MESSAGES

router = APIRouter(prefix="/api")
CHAT_TITLE = "Чат вики"
SNIPPET_CHARS = 160
# Совпадения по названию и тегам интерфейс находит сам; сервер ищет по тексту заметок.
SEARCH_LIMIT = 20
MARKUP = [
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),          # ссылки и картинки — только текст
    (re.compile(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+", re.MULTILINE), ""),  # заголовки, цитаты, списки
    (re.compile(r"(\*\*|__|~~|`)"), ""),                          # жирный, зачёркнутый, код
    (re.compile(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)"), r"\1"),  # курсив
    (re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*$", re.MULTILINE), ""),  # разделитель таблицы
    (re.compile(r"^[ \t]*\||\|[ \t]*$", re.MULTILINE), ""),     # края строк таблицы
    (re.compile(r"[ \t]*\|[ \t]*"), " · "),                       # ячейки таблицы
]


class PageContent(BaseModel):
    path: str
    content: str


class Action(BaseModel):
    title: str
    text: str


class Actions(BaseModel):
    actions: list[Action]


class Upload(BaseModel):
    # Картинка как data URL: data:image/png;base64,…
    data: str


def _services(request: Request):
    return request.app.state.services


# ---------- заметки ----------

@router.get("/wiki")
def pages(request: Request) -> list[dict]:
    return [{"path": page.path, "title": page.title, "updated_at": page.updated_at.isoformat(), "size": page.size,
             "description": page.description, "tags": list(page.tags)} for page in _services(request).wiki.pages()]


@router.get("/wiki/graph")
def graph(request: Request) -> dict:
    return _services(request).wiki.graph()


@router.get("/wiki/search")
def search(q: str, request: Request) -> list[dict]:
    return [{"path": path, "title": title, "line": line, "text": snippet(plain_text(text), q)}
            for path, title, line, text in _services(request).wiki.search(q, limit=SEARCH_LIMIT)]


def plain_text(markdown: str) -> str:
    """Фрагмент страницы для списка найденного: без разметки Markdown, в одну строку."""
    text = markdown
    for pattern, replacement in MARKUP:
        text = pattern.sub(replacement, text)
    return " ".join(text.split()).strip(" ·")


def snippet(text: str, query: str) -> str:
    """Кусок текста вокруг первого слова запроса, а не начало абзаца."""
    lowered = text.casefold()
    hits = [lowered.find(word) for word in re.findall(r"\w{3,}", query.casefold())]
    hit = min((index for index in hits if index >= 0), default=0)
    start = max(0, hit - SNIPPET_CHARS // 3)
    piece = text[start:start + SNIPPET_CHARS]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET_CHARS < len(text) else "")


@router.get("/wiki/file")
def image(path: str, request: Request) -> FileResponse:
    """Картинка из заметки: путь от корня вики, как он записан в заметке."""
    try:
        return FileResponse(_services(request).wiki.image(path))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/wiki/page")
def read(path: str, request: Request) -> dict:
    try:
        content = _services(request).wiki.read(path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    frontmatter, body = notes.split_frontmatter(content)
    meta = None
    if frontmatter:
        note = notes.parse(path, content)
        meta = {"type": note.type, "title": note.title, "description": note.description,
                "tags": list(note.tags), "keywords": list(note.keywords)}
    return {"path": path, "content": content, "body": body, "meta": meta}


@router.put("/wiki/page")
def write(payload: PageContent, request: Request) -> dict:
    try:
        _services(request).wiki.write(payload.path, payload.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return read(payload.path, request)


@router.delete("/wiki/page", status_code=204)
def delete(path: str, request: Request) -> None:
    try:
        _services(request).wiki.delete(path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------- действия и чат вики ----------

@router.get("/wiki/actions")
def actions(request: Request) -> dict:
    """Меню «Действия ▾» чата вики: ваши проверки — название и что искать."""
    return {"actions": _services(request).wiki_actions.read()}


@router.put("/wiki/actions")
def save_actions(payload: Actions, request: Request) -> dict:
    try:
        return {"actions": _services(request).wiki_actions.write([action.model_dump() for action in payload.actions])}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def wiki_sessions(services) -> list[dict]:
    return [session for session in services.sessions.list() if session.get("kind") == "wiki"]


@router.get("/wiki/chat")
def chat_state(request: Request) -> dict:
    """Сессия чата вики (сообщения, ход и карточки — обычными API сессий) и сколько реплик он помнит."""
    services = _services(request)
    session = max(wiki_sessions(services), key=lambda item: item["created_at"], default=None)
    return {"session": session, "running": bool(session) and services.turns.running(session["id"]) is not None,
            "memory": WIKI_CONTEXT_MESSAGES}


@router.post("/wiki/chat")
def ensure_chat(request: Request) -> dict:
    services = _services(request)
    session = max(wiki_sessions(services), key=lambda item: item["created_at"], default=None)
    return {"session": session or services.sessions.create(None, CHAT_TITLE, kind="wiki")}


@router.delete("/wiki/chat", status_code=204)
def clear_chat(request: Request) -> None:
    """«Очистить»: разговор вики начинается заново, заметки не трогаются."""
    services = _services(request)
    sessions = wiki_sessions(services)
    if any(services.turns.running(session["id"]) for session in sessions):
        raise HTTPException(409, "Сначала дождитесь ответа или остановите его")
    for session in sessions:
        services.sessions.delete(session["id"])


# ---------- картинки чата вики ----------

@router.post("/attachments", status_code=201)
def upload(payload: Upload, request: Request) -> dict:
    try:
        name = _services(request).attachments.save(payload.data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"name": name, "url": f"/api/attachments/{name}"}


@router.get("/attachments/{name}")
def download(name: str, request: Request) -> FileResponse:
    path = _services(request).attachments.path(name)
    if path is None:
        raise HTTPException(404, "Картинки нет")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
