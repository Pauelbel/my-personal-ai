"""Вики для UI: список страниц, граф ссылок, поиск, чтение и ручная правка."""

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from wiki import notes

router = APIRouter(prefix="/wiki", tags=["wiki"])


class PageContent(BaseModel):
    path: str
    content: str


class NoteMeta(BaseModel):
    type: str = ""
    title: str = ""
    description: str = ""
    tags: list[str] = []
    keywords: list[str] = []


class PageView(PageContent):
    """Для просмотра: тело без фронтматтера и разобранные поля OKF отдельно."""

    body: str
    meta: NoteMeta | None = None


@router.get("")
def pages(request: Request) -> list[dict]:
    return [
        {
            "path": page.path, "title": page.title, "updated_at": page.updated_at.isoformat(), "size": page.size,
            "description": page.description, "tags": list(page.tags),
        }
        for page in request.app.state.wiki_service.pages()
    ]


class ChatModel(BaseModel):
    provider: str | None = None
    model: str | None = None


CHAT_TITLE = "Чат вики"


def _chat(request: Request):
    """Чат вики — одна служебная сессия вне проектов; самая свежая, если их почему-то несколько."""
    sessions = [session for session in request.app.state.session_service.list() if session.kind == "wiki"]
    return max(sessions, key=lambda session: session.created_at, default=None)


def _busy(request: Request, session_id: str) -> bool:
    state = request.app.state
    return state.turn_hub.running(session_id) is not None or state.agent_runtime.is_busy(session_id)


@router.get("/chat")
def chat_state(request: Request) -> dict:
    """Сессия чата вики (сообщения, ход и карточки читаются обычными API сессий) и сколько реплик он помнит."""
    session = _chat(request)
    return {
        "session": session.model_dump(mode="json") if session else None,
        "running": bool(session) and _busy(request, session.id),
        "memory": request.app.state.settings.wiki_context_messages,
    }


@router.post("/chat")
def ensure_chat(payload: ChatModel, request: Request) -> dict:
    """Чат вики с моделью, выбранной в интерфейсе: создаётся при первом сообщении, модель следует за выбором."""
    state = request.app.state
    session = _chat(request)
    projects = sorted(state.project_service.list(), key=lambda project: project.updated_at, reverse=True)
    provider = payload.provider or (session.provider if session else None) or (projects[0].provider if projects else "lm_studio")
    model = payload.model or (session.model if session else "") or (projects[0].model if projects else "") or state.settings.default_model
    if not model:
        raise HTTPException(status_code=400, detail="Выберите модель под полем ввода чата")
    if session is None:
        session = state.session_service.create(
            title=CHAT_TITLE, model=model, provider=provider, project_id=None, hidden=True, kind="wiki",
        )
    elif (session.provider, session.model) != (provider, model) and not _busy(request, session.id):
        session = state.session_service.configure(session.id, provider=provider, model=model) or session
    return {"session": session.model_dump(mode="json")}


@router.delete("/chat", status_code=204)
def clear_chat(request: Request) -> None:
    """«Очистить»: разговор вики начинается заново, заметки не трогаются."""
    state = request.app.state
    sessions = [session for session in state.session_service.list() if session.kind == "wiki"]
    if any(_busy(request, session.id) for session in sessions):
        raise HTTPException(status_code=409, detail="Сначала дождитесь ответа или остановите его")
    for session in sessions:
        state.session_service.delete(session.id)


class Action(BaseModel):
    title: str
    text: str


class Actions(BaseModel):
    actions: list[Action]


@router.get("/actions")
def actions(request: Request) -> dict:
    """Меню «Действия ▾» чата вики: ваши проверки — название и что искать."""
    return {"actions": request.app.state.wiki_actions.read()}


@router.put("/actions")
def save_actions(payload: Actions, request: Request) -> dict:
    try:
        return {"actions": request.app.state.wiki_actions.write([action.model_dump() for action in payload.actions])}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/graph")
def graph(request: Request) -> dict:
    return request.app.state.wiki_service.graph()


@router.get("/search")
def search(q: str, request: Request) -> list[dict]:
    return [
        {"path": path, "title": title, "line": line, "text": snippet(plain_text(text), q)}
        for path, title, line, text in request.app.state.wiki_service.search(q, limit=SEARCH_LIMIT)
    ]


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


@router.get("/file")
def image(path: str, request: Request) -> FileResponse:
    """Картинка из заметки: путь от корня вики, как он записан в заметке."""
    try:
        return FileResponse(request.app.state.wiki_service.image(path))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/page", response_model=PageView)
def read(path: str, request: Request) -> PageView:
    try:
        content = request.app.state.wiki_service.read(path)
        frontmatter, body = notes.split_frontmatter(content)
        meta = None
        if frontmatter:
            note = notes.parse(path, content)
            meta = NoteMeta(type=note.type, title=note.title, description=note.description,
                            tags=list(note.tags), keywords=list(note.keywords))
        return PageView(path=path, content=content, body=body, meta=meta)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/page", response_model=PageView)
def write(payload: PageContent, request: Request) -> PageView:
    try:
        request.app.state.wiki_service.write(payload.path, payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return read(payload.path, request)


@router.delete("/page", status_code=204)
def delete(path: str, request: Request) -> None:
    try:
        request.app.state.wiki_service.delete(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
