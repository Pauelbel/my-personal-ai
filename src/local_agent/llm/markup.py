"""Служебная разметка моделей, которую сервер не разобрал и прислал обычным текстом.

- Рассуждения внутри ответа: Gemma 4 — «<|channel>thought … <channel|>», Qwen и DeepSeek — «<think> … </think>».
  ThinkingRouter на лету отделяет их от ответа, чтобы в чат шёл только ответ.
- Вызов инструмента текстом: Gemma 4 пишет «<|tool_call>call:имя{ключ:<|"|>строка<|"|>,…}<tool_call|>»;
  LM Studio иногда оставляет его в рассуждениях или ответе, и действие терялось. recover_tool_calls
  достаёт такие вызовы, если сервер не вернул ни одного настоящего.
"""

import json
import re
from uuid import uuid4

from local_agent.llm.models import ToolCall

OPENERS = ("<|channel>thought", "<|channel>", "<think>")
CLOSERS = ("<channel|>", "</think>")
# Метки, которые в показанных рассуждениях только мешают читать.
NOISE = re.compile(r"<\|channel>(?:thought)?|<channel\|>|<\|tool_response>|<tool_response\|>")
GEMMA_CALL = re.compile(r"(?:<\|tool_call>)?\s*call:([A-Za-z0-9_.-]+)\s*(\{.*?\})\s*<tool_call\|>", re.S)
GEMMA_STRING = re.compile(r'<\|"\|>(.*?)<\|"\|>', re.S)
BARE_KEY = re.compile(r'([{,]\s*)([A-Za-z_][\w-]*)\s*:')


class ThinkingRouter:
    """Делит поток текста на рассуждения и ответ по меткам; метку, разрезанную между кусками, дожидается."""

    def __init__(self) -> None:
        self.thinking = False
        self._held = ""

    def feed(self, text: str) -> list[tuple[bool, str]]:
        """Возвращает куски (это рассуждения?, текст) в порядке потока."""
        buffer, self._held, out = self._held + text, "", []
        while buffer:
            markers = CLOSERS if self.thinking else OPENERS
            # Ближайшая метка; из двух в одном месте — длинная («<|channel>thought», а не «<|channel>»).
            found = min(((buffer.find(marker), marker) for marker in markers if marker in buffer),
                        key=lambda item: (item[0], -len(item[1])), default=None)
            if found:
                index, marker = found
                if index:
                    out.append((self.thinking, buffer[:index]))
                buffer = buffer[index + len(marker):]
                self.thinking = not self.thinking
                continue
            # Хвост может быть началом метки: придержим его до следующего куска.
            cut = _partial(buffer, markers)
            if cut:
                self._held = buffer[-cut:]
                buffer = buffer[:-cut]
            if buffer:
                out.append((self.thinking, buffer))
            break
        return out

    def flush(self) -> list[tuple[bool, str]]:
        held, self._held = self._held, ""
        return [(self.thinking, held)] if held else []


def _partial(buffer: str, markers: tuple[str, ...]) -> int:
    """Длина хвоста, который может оказаться началом метки."""
    for size in range(min(max(map(len, markers)) - 1, len(buffer)), 0, -1):
        if any(marker.startswith(buffer[-size:]) for marker in markers):
            return size
    return 0


def clean_reasoning(text: str) -> str:
    return NOISE.sub("", text)


def recover_tool_calls(text: str, known: set[str]) -> tuple[tuple[ToolCall, ...], str]:
    """Вызовы Gemma, оставшиеся текстом, и текст без них. Берутся только известные инструменты хода."""
    calls = []
    for match in GEMMA_CALL.finditer(text):
        name, raw = match.group(1), match.group(2)
        if name not in known:
            continue
        try:
            arguments = _gemma_arguments(raw)
        except ValueError:
            continue
        calls.append(ToolCall(f"call-{uuid4().hex[:12]}", name, arguments))
    if not calls:
        return (), text
    rest = GEMMA_CALL.sub("", text).replace("<|tool_response>", "").strip()
    return tuple(calls), rest


def _gemma_arguments(raw: str) -> str:
    """{ключ:<|"|>текст<|"|>,n:3} → JSON: строки Gemma обрамляет <|"|>, ключи пишет без кавычек."""
    strings: list[str] = []

    def keep(match: re.Match) -> str:
        strings.append(match.group(1))
        return f"\x00{len(strings) - 1}\x00"

    text = GEMMA_STRING.sub(keep, raw)
    text = BARE_KEY.sub(r'\1"\2":', text)
    text = re.sub(r"\x00(\d+)\x00", lambda match: json.dumps(strings[int(match.group(1))], ensure_ascii=False), text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("аргументы — не объект")
    return json.dumps(value, ensure_ascii=False)
