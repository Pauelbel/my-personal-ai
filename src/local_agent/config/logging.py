"""Базовое логирование делает ошибки и HTTP-запросы видимыми при локальном запуске."""

import logging


def configure_logging(level: str) -> None:
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        raise ValueError(f"Недопустимое значение LOG_LEVEL: {level}")

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
