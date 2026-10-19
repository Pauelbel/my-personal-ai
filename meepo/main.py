"""Запуск сервера: `python -m meepo.main`."""

import uvicorn

from meepo.app import create_app
from meepo.settings import Env


def run() -> None:
    env = Env.load()
    uvicorn.run(create_app(env), host=env.host, port=env.port)


if __name__ == "__main__":
    run()
