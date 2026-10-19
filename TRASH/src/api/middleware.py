"""Middleware добавляет request ID и фиксирует итог каждого HTTP-запроса в логах."""

import logging
import time
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LocalOriginMiddleware(BaseHTTPMiddleware):
    """Отклоняет чужой Host (DNS rebinding) и изменяющие запросы с чужих страниц (CSRF)."""

    def __init__(self, app, allowed_hosts: set[str]) -> None:
        super().__init__(app)
        self._allowed_hosts = allowed_hosts

    async def dispatch(self, request: Request, call_next) -> Response:
        host = urlsplit(f"//{request.headers.get('host', '')}").hostname
        if host not in self._allowed_hosts:
            return PlainTextResponse("Недопустимый заголовок Host", status_code=400)
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin is not None:
            if urlsplit(origin).hostname not in self._allowed_hosts:
                return PlainTextResponse("Запрос со сторонней страницы отклонён", status_code=403)
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "HTTP-запрос завершился ошибкой request_id=%s method=%s path=%s latency_ms=%.1f",
                request_id,
                request.method,
                request.url.path,
                (time.perf_counter() - started) * 1000,
            )
            raise

        response.headers["X-Request-ID"] = request_id
        logger.info(
            "HTTP-запрос выполнен request_id=%s method=%s path=%s status=%s latency_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response
