"""HTTP-адаптер вызывает OpenAI-compatible API: LM Studio, Ollama или облачный сервер."""

import json
import time
from collections.abc import AsyncIterator

import httpx

from local_agent.llm.base import LLMProviderError, LLMProviderUnavailable
from local_agent.llm.models import ChatMessage, ChatResult, ReasoningDelta, ToolCall

CONTEXT_CACHE_SECONDS = 60
# Сколько символов пояснения сервера показать в ошибке: «model does not exist» и подобное.
ERROR_DETAIL_CHARS = 300


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        name: str = "LM Studio",
        api_key: str = "",
    ) -> None:
        self.name = name
        base = base_url.rstrip("/")
        # Нативный API LM Studio (/api/v0) живёт рядом с /v1 и сообщает размер окна модели.
        self._native_root = base.removesuffix("/v1") + "/"
        self._client = httpx.AsyncClient(
            base_url=base + "/",
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
        )
        self._context_cache: dict[str, tuple[float, int | None]] = {}

    async def chat(
        self, model: str, messages: list[ChatMessage],
        tools: list[dict[str, object]] | None = None,
        response_format: dict[str, object] | None = None,
    ) -> ChatResult:
        payload = self._payload(model, messages, tools, stream=False)
        if response_format:
            payload["response_format"] = response_format
        data = await self._request("POST", "chat/completions", json=payload)
        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
            calls = tuple(
                ToolCall(
                    id=item["id"],
                    name=item["function"]["name"],
                    arguments=item["function"]["arguments"],
                )
                for item in message.get("tool_calls") or []
            )
            return self._result(content, calls, data.get("usage") or {})
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMProviderError(f"{self.name} вернул некорректный ответ модели") from exc

    async def chat_stream(
        self, model: str, messages: list[ChatMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> AsyncIterator[str | ReasoningDelta | ChatResult]:
        payload = self._payload(model, messages, tools, stream=True)
        payload["stream_options"] = {"include_usage": True}
        parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: dict = {}
        try:
            async with self._client.stream("POST", "chat/completions", json=payload) as response:
                if response.is_error:
                    await response.aread()
                    raise LLMProviderError(self._http_error(response))
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
                        # LM Studio отдаёт рассуждения в reasoning_content, Ollama и OpenRouter — в reasoning.
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            yield ReasoningDelta(reasoning)
                        if text := delta.get("content"):
                            parts.append(text)
                            yield text
                        for item in delta.get("tool_calls") or []:
                            slot = calls.setdefault(item.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            slot["id"] = item.get("id") or slot["id"]
                            function = item.get("function") or {}
                            slot["name"] += function.get("name") or ""
                            slot["arguments"] += function.get("arguments") or ""
        except httpx.TimeoutException as exc:
            raise LLMProviderUnavailable(f"{self.name} не ответил за отведённое время") from exc
        except httpx.RequestError as exc:
            raise LLMProviderUnavailable(f"Не удалось подключиться к {self.name}") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise LLMProviderError(f"{self.name} вернул некорректный поток ответа") from exc
        tool_calls = tuple(
            ToolCall(id=slot["id"] or f"call-{index}", name=slot["name"], arguments=slot["arguments"] or "{}")
            for index, slot in sorted(calls.items())
        )
        try:
            yield self._result("".join(parts), tool_calls, usage)
        except ValueError as exc:
            raise LLMProviderError(f"{self.name} вернул некорректный ответ модели") from exc

    async def list_models(self) -> list[str]:
        data = await self._request("GET", "models")
        try:
            return [item["id"] for item in data["data"] if isinstance(item["id"], str)]
        except (KeyError, TypeError) as exc:
            raise LLMProviderError(f"{self.name} вернул некорректный список моделей") from exc

    async def context_length(self, model: str) -> int | None:
        cached = self._context_cache.get(model)
        if cached and time.monotonic() - cached[0] < CONTEXT_CACHE_SECONDS:
            return cached[1]
        # LM Studio сообщает окно в нативном API, vLLM — в поле max_model_len списка /v1/models.
        length = await self._window(self._native_root + "api/v0/models", model, "loaded_context_length", "max_context_length")
        if length is None:
            length = await self._window("models", model, "max_model_len")
        if length is None:
            length = await self._ollama_window(model)
        # Ollama ещё не загрузила модель — окно станет известно после первого запроса, не запоминаем пустой ответ.
        if length is not None:
            self._context_cache[model] = (time.monotonic(), length)
        return length

    async def _window(self, url: str, model: str, *fields: str) -> int | None:
        """Размер окна модели из списка моделей сервера; None, если сервер его не сообщает."""
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            for item in response.json().get("data", []):
                if item.get("id") == model:
                    value = next((item[field] for field in fields if item.get(field)), None)
                    return value if isinstance(value, int) and value > 0 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            # Сервер этого не умеет: runtime возьмёт DEFAULT_CONTEXT_TOKENS.
            return None
        return None

    async def _ollama_window(self, model: str) -> int | None:
        """Ollama сообщает окно только у загруженной модели, в /api/ps; обычно оно меньше максимума модели."""
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
        return f"Сервер модели «{self.name}» вернул ошибку HTTP {response.status_code}" + (f": {detail}" if detail else "")

    async def close(self) -> None:
        await self._client.aclose()

    @classmethod
    def _payload(
        cls, model: str, messages: list[ChatMessage],
        tools: list[dict[str, object]] | None, *, stream: bool,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": model,
            "messages": [cls._message_payload(message) for message in messages],
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        return payload

    @staticmethod
    def _result(content: object, calls: tuple[ToolCall, ...], usage: dict) -> ChatResult:
        if (not isinstance(content, str) or not content.strip()) and not calls:
            raise ValueError("модель вернула пустой ответ")
        if any(not call.id or not call.name or not isinstance(call.arguments, str) for call in calls):
            raise ValueError("некорректный вызов инструмента")
        return ChatResult(
            content=content if isinstance(content, str) and content.strip() else None,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            tool_calls=calls,
        )

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict[str, object]:
        payload: dict[str, object] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("ответ не является JSON-объектом")
            return data
        except httpx.TimeoutException as exc:
            raise LLMProviderUnavailable(f"{self.name} не ответил за отведённое время") from exc
        except httpx.RequestError as exc:
            raise LLMProviderUnavailable(f"Не удалось подключиться к {self.name}") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(self._http_error(exc.response)) from exc
        except ValueError as exc:
            raise LLMProviderError(f"{self.name} вернул некорректный JSON") from exc
