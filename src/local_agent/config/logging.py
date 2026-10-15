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
    # uvicorn и часть библиотек ставят логгерам свой уровень, и basicConfig до них не достаёт:
    # LOG_LEVEL должен действовать на все логи, поэтому выравниваем каждый уже созданный логгер.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", *logging.root.manager.loggerDict):
        logger = logging.getLogger(name)
        if logger.level != logging.NOTSET or name.startswith("uvicorn"):
            logger.setLevel(numeric_level)
