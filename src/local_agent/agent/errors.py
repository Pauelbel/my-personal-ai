"""Ошибки хода агента: их показывает API и UI."""


class AgentRuntimeError(Exception):
    """Сессию нельзя обработать из-за неподдерживаемой конфигурации."""


class SessionNotFoundError(AgentRuntimeError):
    """Запрошенная сессия не существует."""


class SessionBusyError(AgentRuntimeError):
    """В сессии уже выполняется другой ход."""


class NothingPendingError(AgentRuntimeError):
    """Решение пришло, а ход ничего не ждёт: его уже продолжили или отменили."""
