"""OpenAI-совместимый клиент модели: LM Studio, Ollama, vLLM.

Сообщения — словари OpenAI API: {"role", "content", "tool_calls", "tool_call_id"}; вызов инструмента внутри
Мипо — {"id", "name", "arguments"} (arguments — строка JSON), в запрос он уходит в формате OpenAI.
`stream` отдаёт по частям ("delta", текст) и ("reasoning", текст), в конце — Reply.
"""

import base64
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from meepo.llm.markup import ThinkingRouter, clean_reasoning, recover

CONTEXT_CACHE_SECONDS = 60
# Сколько символов пояснения сервера показать в ошибке: «model does not exist» и подобное.
ERROR_DETAIL_CHARS = 300
# Gemma 4 закрывает рассуждения маркером «<channel|>». Бывает, что сервер шлёт их обычным текстом без
# открывающей метки: тогда всё до маркера — рассуждения, а ответ — после него.
THINKING_END = "<channel|>"
# Коды, которыми сервер отвергает незнакомый параметр запроса (reasoning_effort).
REJECTED_PARAMETER = (400, 422)


class LLMError(Exception):
    """Сервер модели не смог вернуть корректный ответ."""


class _ReasoningRejected(Exception):
    """Сервер не принял reasoning_effort: запрос повторяется без него."""


class LLMUnavailable(LLMError):
    """Сервер модели недоступен или превысил время ожидания."""


class LLMEmpty(LLMError):
    """Модель ответила пустотой: ни текста, ни вызовов (бывает у thinking-моделей, ушедших в рассуждения)."""


class LLMRepetition(LLMError):
    """Модель зациклилась: по кругу пишет одно и то же — в рассуждениях или в ответе."""


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    reasoning: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


class RepetitionDetector:
    """Замечает, что модель пишет по кругу: хвост текста уже встречался в недавнем выводе несколько раз.

    Маленькие thinking-модели иногда без конца повторяют один и тот же план в рассуждениях; поток при этом
    не молчит, и тайм-аут не срабатывает. Проверка — раз в CHECK_EVERY символов, по последним WINDOW символам.
    """

    WINDOW = 200
    REPEATS = 5
    # Однообразный хвост (меньше четырёх разных слов) — цикл, только если повторился много раз: так модель
    # застревает на служебных токенах, а не пишет одну линию из чёрточек в таблице.
    MONOTONE_REPEATS = 20
    CHECK_EVERY = 400
    KEEP = 16_000

    def __init__(self) -> None:
        self._text = ""
        self._unchecked = 0

    def looped(self, chunk: str) -> bool:
        self._text = (self._text + chunk)[-self.KEEP:]
        self._unchecked += len(chunk)
        if self._unchecked < self.CHECK_EVERY or len(self._text) < self.WINDOW * self.REPEATS:
            return False
        self._unchecked = 0
        tail = self._text[-self.WINDOW:]
        repeats = self.REPEATS if len(set(tail.split())) > 3 else self.MONOTONE_REPEATS
        return self._text.count(tail) >= repeats


class LLMClient:
    def __init__(
        self, base_url: str, *, api_key: str = "", timeout: float = 120.0, verify_ssl: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Нативный API LM Studio (/api/v0) и Ollama (/api/ps) живут рядом с /v1 и сообщают размер окна модели.
        self._native_root = self.base_url.removesuffix("/v1") + "/"
        self._client = httpx.AsyncClient(
            base_url=self.base_url + "/", timeout=timeout, transport=transport, verify=verify_ssl,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
        )
        self._context_cache: dict[str, tuple[float, int]] = {}
        # Рассуждения отключаются (reasoning_effort: none), как и в скиллах: Ollama с qwen3.5 кладёт в них и сам
        # ответ, оставляя текст пустым, — ход падал с «пустым ответом»; без рассуждений ответ ещё и в 2–3 раза быстрее.
        # Сервер параметр не принял — дальше запросы идут без него.
        self._reasoning_off = True

    async def stream(
        self, model: str, messages: list[dict], tools: list[dict] | None = None, response_format: dict | None = None,
    ) -> AsyncIterator[tuple[str, str] | Reply]:
        """response_format — строгий JSON-ответ по схеме ({"type": "json_schema", …}): для служебных запросов (память)."""
        payload = {"model": model, "messages": [_message_payload(item) for item in messages], "stream": True,
                   "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        if self._reasoning_off:
            payload["reasoning_effort"] = "none"
        parts: list[str] = []
        thoughts: list[str] = []
        # Рассуждения, которые сервер не отделил и прислал в ответе (метки Gemma, <think>), уходят в рассуждения.
        router = ThinkingRouter()
        detector = RepetitionDetector()
        calls: dict[int, dict[str, str]] = {}
        usage: dict = {}
        try:
            async with self._client.stream("POST", "chat/completions", json=payload) as response:
                if response.is_error:
                    await response.aread()
                    if response.status_code in REJECTED_PARAMETER and "reasoning_effort" in payload:
                        self._reasoning_off = False
                        raise _ReasoningRejected
                    raise LLMError(self._http_error(response))
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        # LM Studio отдаёт рассуждения в reasoning_content, Ollama — в reasoning.
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        pieces = [(True, reasoning)] if isinstance(reasoning, str) and reasoning else []
                        if text := delta.get("content"):
                            pieces += router.feed(text)
                        for thinking, piece in pieces:
                            if detector.looped(piece):
                                raise LLMRepetition("Модель зациклилась: одно и то же повторяется по кругу")
                            yield self._route(thinking, piece, parts, thoughts)
                        for item in delta.get("tool_calls") or []:
                            slot = calls.setdefault(item.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            slot["id"] = item.get("id") or slot["id"]
                            function = item.get("function") or {}
                            slot["name"] += function.get("name") or ""
                            slot["arguments"] += function.get("arguments") or ""
        except _ReasoningRejected:
            async for item in self.stream(model, messages, tools, response_format):
                yield item
            return
        except httpx.TimeoutException as exc:
            raise LLMUnavailable("Сервер модели не ответил за отведённое время") from exc
        except httpx.RequestError as exc:
            raise LLMUnavailable(f"Не удалось подключиться к серверу модели {self.base_url}: {type(exc).__name__}") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise LLMError("Сервер модели вернул некорректный поток ответа") from exc
        for thinking, piece in router.flush():
            yield self._route(thinking, piece, parts, thoughts)
        tool_calls = [
            {"id": slot["id"] or f"call-{index}", "name": slot["name"], "arguments": slot["arguments"] or "{}"}
            for index, slot in sorted(calls.items()) if slot["name"]
        ]
        content = "".join(parts)
        if THINKING_END in content:
            content = content.rsplit(THINKING_END, 1)[-1]
        if not tool_calls and tools:
            tool_calls, content = recover(tools, content, "".join(thoughts))
        if not content.strip() and not tool_calls:
            raise LLMEmpty("Модель вернула пустой ответ")
        yield Reply(content.strip(), tool_calls, "".join(thoughts),
                    usage.get("prompt_tokens"), usage.get("completion_tokens"))

    @staticmethod
    def _route(thinking: bool, piece: str, parts: list[str], thoughts: list[str]) -> tuple[str, str]:
        if thinking:
            thoughts.append(piece)
            return "reasoning", clean_reasoning(piece)
        parts.append(piece)
        return "delta", piece

    async def list_models(self) -> list[str]:
        try:
            response = await self._client.get("models")
            response.raise_for_status()
            return [item["id"] for item in response.json()["data"] if isinstance(item.get("id"), str)]
        except httpx.RequestError as exc:
            raise LLMUnavailable(f"Не удалось подключиться к серверу модели {self.base_url}") from exc
        except (httpx.HTTPStatusError, ValueError, KeyError, TypeError) as exc:
            raise LLMError("Сервер модели вернул некорректный список моделей") from exc

    async def context_length(self, model: str) -> int | None:
        """Размер окна модели, если сервер его сообщает; иначе None — тогда берётся окно из настроек."""
        cached = self._context_cache.get(model)
        if cached and time.monotonic() - cached[0] < CONTEXT_CACHE_SECONDS:
            return cached[1]
        # LM Studio сообщает окно в нативном API, vLLM — в поле max_model_len списка /v1/models.
        length = await self._window(self._native_root + "api/v0/models", model, "loaded_context_length", "max_context_length")
        if length is None:
            length = await self._window("models", model, "max_model_len")
        if length is None:
            length = await self._ollama_window(model)
        # Ollama ещё не загрузила модель — окно станет известно после первого запроса, пустой ответ не запоминаем.
        if length is not None:
            self._context_cache[model] = (time.monotonic(), length)
        return length

    async def _window(self, url: str, model: str, *fields: str) -> int | None:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            for item in response.json().get("data", []):
                if item.get("id") == model:
                    value = next((item[name] for name in fields if item.get(name)), None)
                    return value if isinstance(value, int) and value > 0 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        return None

    async def _ollama_window(self, model: str) -> int | None:
        """Ollama сообщает окно только у загруженной модели, в /api/ps."""
        try:
            response = await self._client.get(self._native_root + "api/ps")
            response.raise_for_status()
            for item in response.json().get("models", []):
                if model in (item.get("model"), item.get("name")):
                    value = item.get("context_length")
                    return value if isinstance(value, int) and value > 0 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        return None

    def _http_error(self, response: httpx.Response) -> str:
        """Ошибка HTTP с пояснением сервера: по одному коду 404 не понять, что не так с моделью."""
        detail = ""
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            detail = (error.get("message") if isinstance(error, dict) else error) or data.get("detail") or data.get("message")
        except (ValueError, AttributeError):
            detail = response.text
        detail = " ".join(str(detail or "").split())[:ERROR_DETAIL_CHARS]
        return f"Сервер модели вернул ошибку HTTP {response.status_code}" + (f": {detail}" if detail else "")

    async def close(self) -> None:
        await self._client.aclose()


IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


def _message_payload(message: dict) -> dict:
    payload = {"role": message["role"], "content": message.get("content")}
    if message.get("images"):
        # Картинки — частями содержимого в формате OpenAI: так их понимают Ollama, LM Studio и vLLM.
        parts = [{"type": "text", "text": message.get("content") or ""}]
        for file in map(Path, message["images"]):
            if file.is_file():
                media = IMAGE_MEDIA.get(file.suffix.lower(), "image/png")
                url = f"data:{media};base64,{base64.b64encode(file.read_bytes()).decode()}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
        payload["content"] = parts
    if message.get("tool_calls"):
        payload["tool_calls"] = [
            {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}
            for call in message["tool_calls"]
        ]
    if message.get("tool_call_id"):
        payload["tool_call_id"] = message["tool_call_id"]
    return payload
