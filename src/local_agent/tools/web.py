"""Чтение веб-страницы по ссылке: текст статьи без разметки, чтобы сделать заметку в вики."""

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

from local_agent.core.tool import ToolResult

MAX_DOWNLOAD_BYTES = 3 * 1024 * 1024
DEFAULT_MAX_CHARS = 12_000
SKIPPED_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
BLOCK_TAGS = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}


class FetchUrlTool:
    id = "fetch_url"
    name = "Чтение страницы по ссылке"
    description = "Скачать веб-страницу по http(s)-ссылке и вернуть её текст без разметки (статьи, документация)"
    requires_approval = False
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Адрес страницы, http или https"},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 40000,
                          "description": f"Сколько символов текста вернуть, по умолчанию {DEFAULT_MAX_CHARS}"},
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(self, timeout: float = 20, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._timeout = timeout
        self._transport = transport

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        url = arguments.get("url")
        limit = arguments.get("max_chars", DEFAULT_MAX_CHARS)
        if not isinstance(limit, int) or isinstance(limit, bool):
            limit = DEFAULT_MAX_CHARS
        try:
            await asyncio.to_thread(check_public_url, url)
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True, transport=self._transport,
                headers={"User-Agent": "Mozilla/5.0 (Meepo local agent)"},
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    # Редирект мог увести во внутреннюю сеть.
                    await asyncio.to_thread(check_public_url, str(response.url))
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_DOWNLOAD_BYTES:
                            break
                    kind = response.headers.get("content-type", "")
                    text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult(f"Не удалось скачать страницу: {exc}", is_error=True)
        if "html" in kind or text.lstrip().startswith("<"):
            title, text = html_to_text(text)
        else:
            title = ""
        text = text.strip()
        cut = len(text) > limit
        return ToolResult(
            (f"# {title}\n" if title else "") + f"Источник: {url}\n\n" + text[:limit]
            + ("\n\n… (обрезано)" if cut else "")
        )


def check_public_url(url: object) -> None:
    """Только http(s) и только публичные адреса: агент не должен ходить в локальную сеть и на localhost."""
    if not isinstance(url, str):
        raise ValueError("Нужна ссылка строкой")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Поддерживаются только ссылки http и https")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except OSError as exc:
        raise ValueError(f"Адрес {parsed.hostname} не найден") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Ссылки на локальную сеть и localhost не открываются")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in SKIPPED_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
            if tag in {"h1", "h2", "h3"}:
                self.parts.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    return " ".join(parser.title.split()), text
