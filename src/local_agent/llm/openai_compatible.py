"""HTTP-адаптер вызывает OpenAI-compatible API, доступный в LM Studio."""

import httpx

from local_agent.llm.base import LLMProviderError, LLMProviderUnavailable
from local_agent.llm.models import ChatMessage, ChatResult, ToolCall


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
        )

    async def chat(
        self, model: str, messages: list[ChatMessage],
        tools: list[dict[str, object]] | None = None,
    ) -> ChatResult:
        payload: dict[str, object] = {
            "model": model,
            "messages": [self._message_payload(message) for message in messages],
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        data = await self._request(
            "POST",
            "chat/completions",
            json=payload,
        )
        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
            calls = tuple(
                ToolCall(
                    id=item["id"],
                    name=item["function"]["name"],
                    arguments=item["function"]["arguments"],
                )
                for item in message.get("tool_calls", [])
            )
            if (not isinstance(content, str) or not content.strip()) and not calls:
                raise ValueError("empty assistant content")
            if calls and any(not call.id or not call.name or not isinstance(call.arguments, str) for call in calls):
                raise ValueError("invalid tool call")
            usage = data.get("usage") or {}
            return ChatResult(
                content=content,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                tool_calls=calls,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMProviderError("LM Studio returned an invalid chat response") from exc

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

    async def list_models(self) -> list[str]:
        data = await self._request("GET", "models")
        try:
            return [item["id"] for item in data["data"] if isinstance(item["id"], str)]
        except (KeyError, TypeError) as exc:
            raise LLMProviderError("LM Studio returned an invalid models response") from exc

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            return data
        except httpx.TimeoutException as exc:
            raise LLMProviderUnavailable("LM Studio request timed out") from exc
        except httpx.RequestError as exc:
            raise LLMProviderUnavailable("Cannot connect to LM Studio") from exc
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(
                f"LM Studio returned HTTP {exc.response.status_code}"
            ) from exc
        except ValueError as exc:
            raise LLMProviderError("LM Studio returned invalid JSON") from exc
