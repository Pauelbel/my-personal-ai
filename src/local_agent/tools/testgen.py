"""Инструмент генерации тест-кейсов по документации: агент запускает конвейер порциями страниц."""

from pathlib import Path

from local_agent.llm.base import LLMProvider, LLMProviderUnavailable
from local_agent.testgen.pipeline import OUTPUT_DIR, CaseGenerator, summary
from local_agent.tools.base import ToolResult
from local_agent.tools.filesystem import _inside_workspace, _workspace_root

DEFAULT_LIMIT = 3
MAX_LIMIT = 20


class GenerateTestCasesTool:
    id = "generate_test_cases"
    name = "Генерация тест-кейсов"
    description = (
        "Сгенерировать ручные тест-кейсы по Markdown-документации: страницы раздела обрабатываются по очереди, "
        f"готовые пропускаются, результат — {OUTPUT_DIR}/<путь страницы>. Долгая операция: за вызов не больше "
        "limit страниц; повторный вызов с тем же path продолжает с места остановки."
    )
    # Пишет файлы и надолго занимает модель: запуск подтверждает пользователь.
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Папка раздела или страница документации относительно рабочей папки"},
            "limit": {
                "type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                "description": f"Сколько страниц обработать за вызов, по умолчанию {DEFAULT_LIMIT}",
            },
            "force": {"type": "boolean", "description": "Перегенерировать уже готовые страницы"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, provider: LLMProvider | None = None, model: str = "") -> None:
        self._provider = provider
        self._model = model

    def bind(self, provider: LLMProvider, model: str) -> "GenerateTestCasesTool":
        """Копия на модели текущей сессии: генерация идёт на той модели, что выбрана в чате."""
        return GenerateTestCasesTool(provider, model)

    async def execute(self, arguments: dict[str, object], workspace: Path) -> ToolResult:
        try:
            if self._provider is None or not self._model:
                raise ValueError("Модель для генерации не выбрана")
            limit = arguments.get("limit", DEFAULT_LIMIT)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
                raise ValueError(f"limit — целое число от 1 до {MAX_LIMIT}")
            force = arguments.get("force", False)
            if not isinstance(force, bool):
                raise ValueError("force должен быть true или false")
            root = _workspace_root(workspace)
            start = _inside_workspace(workspace, arguments.get("path"))
            batch = await CaseGenerator(self._provider, self._model, root).run(start, limit=limit, force=force)
            return ToolResult(summary(batch), is_error=bool(batch.pages) and all(page.errors for page in batch.pages))
        except LLMProviderUnavailable as exc:
            return ToolResult(f"{exc}. Готовые страницы сохранены, повторный запуск продолжит с места остановки.", is_error=True)
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True)
