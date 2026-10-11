"""Вики для UI: список страниц, поиск, чтение и ручная правка."""

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/wiki", tags=["wiki"])


class PageContent(BaseModel):
    path: str
    content: str


@router.get("")
def pages(request: Request) -> list[dict]:
    return [
        {"path": page.path, "title": page.title, "updated_at": page.updated_at.isoformat(), "size": page.size}
        for page in request.app.state.wiki_service.pages()
    ]


@router.get("/search")
def search(q: str, request: Request) -> list[dict]:
    return [
        {"path": path, "title": title, "line": line, "text": snippet(plain_text(text), q)}
        for path, title, line, text in request.app.state.wiki_service.search(q, limit=10)
    ]


SNIPPET_CHARS = 160
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


@router.get("/page", response_model=PageContent)
def read(path: str, request: Request) -> PageContent:
    try:
        return PageContent(path=path, content=request.app.state.wiki_service.read(path))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/page", response_model=PageContent)
def write(payload: PageContent, request: Request) -> PageContent:
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
