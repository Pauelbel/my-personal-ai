"""Подгружает ограниченные инструкции только из выбранной рабочей папки."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)
INSTRUCTION_PATHS = ("AGENTS.md", ".github/instructions/learnings.instructions.md")
MAX_FILE_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 16_000


def load_project_instructions(workspace: str | None, *, max_bytes: int = MAX_CONTEXT_BYTES) -> str:
    if not workspace or max_bytes <= 0:
        return ""
    budget = min(max_bytes, MAX_CONTEXT_BYTES)
    parts: list[str] = []
    try:
        root = Path(workspace).resolve(strict=True)
        for relative in INSTRUCTION_PATHS:
            path = root / relative
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    continue
                with resolved.open("rb") as source:
                    raw = source.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    logger.warning("Файл инструкций превышает 64 КБ: %s", relative)
                    continue
                content = raw.decode("utf-8-sig").strip()
                if not content:
                    continue
                header = f"\n\nФайл {relative}:\n"
                marker = "\n[Инструкции обрезаны по лимиту контекста]"
                available = budget - len((header + marker).encode("utf-8"))
                if available <= 0:
                    break
                encoded = content.encode("utf-8")
                if len(encoded) > available:
                    content = encoded[:available].decode("utf-8", errors="ignore") + marker
                part = header + content
                parts.append(part)
                budget -= len(part.encode("utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError, RuntimeError):
                logger.warning("Не удалось прочитать инструкции: %s", relative, exc_info=True)
    except (OSError, RuntimeError):
        logger.warning("Рабочая папка инструкций недоступна", exc_info=True)
    return "".join(parts).strip()
