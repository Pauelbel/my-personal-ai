# skills

Скиллы — папки в `data/skills/` с `SKILL.md` и `scripts/`.

- `registry.py` — список и разбор `SKILL.md`; `runner.py` — запуск скрипта (stdin/stdout/stderr, таймаут).
- `env.py` — общее venv `data/skills-env/`; `secrets.py` — значения секретов вне папки скилла.
- `history.py` — копии перед правкой и откат.
- `sdk/` — помощник для скриптов скиллов; `builtin/` — стартовые скиллы.
