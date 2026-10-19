"""Помощник для скриптов скиллов, без зависимостей: `import meepo_skill` (папка sdk — в PYTHONPATH скилла).

    import meepo_skill as ms
    args = ms.args()                       # аргументы — JSON из stdin
    ms.progress("кусок 3 из 12")           # строка прогресса — в stderr, её видно в UI
    text = ms.ask_llm(system, user)        # запрос к модели сессии; json_schema — строгий JSON-ответ
    state = ms.State("work")               # JSON-файл в MEEPO_STATE_DIR: продолжение с места остановки
    for part in ms.chunks(text, 20000): …  # нарезка Markdown по заголовкам

Правило: большие объёмы — по кускам, каждый готовый кусок сразу в State, повторный запуск продолжает
с места остановки, в stdout — только итог.
"""

import json
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ATTEMPTS = 3
REQUEST_TIMEOUT = 900
TEMPERATURE = 0.2
# Gemma 4 закрывает рассуждения маркером «<channel|>», Qwen и DeepSeek пишут их в <think>…</think>.
THINKING = re.compile(r"(?s)^\s*<think>.*?</think>|^.*<channel\|>")


class Unavailable(Exception):
    """Сервер модели недоступен: работу нужно остановить, готовые куски сохранены."""


class ModelError(Exception):
    """Сервер ответил ошибкой или модель — не тем, что просили: этот кусок не получился."""


def args() -> dict:
    try:
        value = json.load(sys.stdin)
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def progress(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def project() -> Path:
    return Path(os.environ.get("MEEPO_PROJECT") or Path.cwd()).resolve()


_reasoning_off = True


def ask_llm(system: str, user: str, json_schema: dict | None = None) -> str:
    """Ответ модели сессии. Пустой или битый ответ повторяется до ATTEMPTS раз; сервер недоступен — Unavailable.
    Рассуждения отключаются (reasoning_effort: none): они замедляют работу в разы, а качество почти не меняют."""
    global _reasoning_off
    url = os.environ.get("MEEPO_LLM_URL", "").rstrip("/")
    model = os.environ.get("MEEPO_LLM_MODEL", "")
    if not url or not model:
        raise Unavailable("Не задана модель: скрипт запускает Мипо, он передаёт MEEPO_LLM_URL и MEEPO_LLM_MODEL")
    key = os.environ.get("MEEPO_LLM_KEY", "")
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    error: Exception | None = None
    for _ in range(ATTEMPTS):
        payload: dict = {"model": model, "stream": False, "temperature": TEMPERATURE,
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if _reasoning_off:
            payload["reasoning_effort"] = "none"
        if json_schema:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "result", "schema": json_schema, "strict": True}}
        request = urllib.request.Request(url + "/chat/completions", headers=headers,
                                         data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT, context=_ssl()) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = " ".join(exc.read().decode("utf-8", errors="replace").split())[:300]
            if 400 <= exc.code < 500 and _reasoning_off:
                # Сервер мог не принять reasoning_effort: дальше обходимся без него.
                _reasoning_off = False
            error = ModelError(f"сервер модели вернул HTTP {exc.code}: {detail}")
            continue
        except (OSError, ValueError) as exc:
            raise Unavailable(f"Сервер модели недоступен: {exc}") from None
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            error = ModelError("сервер вернул ответ без текста")
            continue
        content = THINKING.sub("", content).strip()
        if content:
            return content
        error = ModelError("пустой ответ модели")
    raise error or ModelError("модель не ответила")


def ask_json(system: str, user: str, json_schema: dict) -> dict:
    """ask_llm со строгой схемой; ответ не JSON-объект — повтор, затем ModelError."""
    error: Exception | None = None
    for _ in range(ATTEMPTS):
        text = ask_llm(system, user, json_schema)
        # Модель без строгой схемы иногда оборачивает JSON в блок ```json.
        text = re.sub(r"(?s)^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            value = json.loads(text)
        except ValueError as exc:
            error = ModelError(f"ответ модели — не JSON: {exc}")
            continue
        if isinstance(value, dict):
            return value
        error = ModelError("ответ модели — не JSON-объект")
    raise error


def _ssl():
    if os.environ.get("MEEPO_LLM_VERIFY_SSL", "true").lower() == "false":
        return ssl._create_unverified_context()
    return None


class State:
    """Простой JSON-файл в MEEPO_STATE_DIR. Каждое изменение пишется сразу и целиком (через временный файл)."""

    def __init__(self, name: str) -> None:
        folder = Path(os.environ.get("MEEPO_STATE_DIR") or project() / ".meepo" / "state")
        safe = re.sub(r"[^\w.-]+", "__", name).strip("_") or "state"
        self.path = folder / f"{safe}.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def clear(self) -> None:
        self.data = {}
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
            json.dump(self.data, stream, ensure_ascii=False)
        Path(stream.name).replace(self.path)


def chunks(text: str, max_chars: int) -> list[str]:
    """Режет Markdown по заголовкам ##/### так, чтобы кусок помещался в max_chars; огромный раздел — по абзацам."""
    if len(text) <= max_chars:
        return [text]
    parts = re.split(r"(?m)^(?=#{2,3} )", text)
    out: list[str] = []
    current = ""
    for part in parts:
        while len(part) > max_chars:
            cut = part.rfind("\n\n", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if current:
                out.append(current)
                current = ""
            out.append(part[:cut])
            part = part[cut:]
        if current and len(current) + len(part) > max_chars:
            out.append(current)
            current = ""
        current += part
    if current.strip():
        out.append(current)
    return out


def inside(path: object, name: str = "path") -> Path:
    """Путь из аргументов — внутри папки проекта; иначе ValueError с понятным текстом."""
    root = project()
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"Нужен {name}: путь относительно папки проекта")
    target = (root / path.strip().strip("`")).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"{name} выходит за пределы папки проекта")
    return target
