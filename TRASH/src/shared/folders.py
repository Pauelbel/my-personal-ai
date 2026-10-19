"""Общие правила обхода рабочей папки: служебные папки пропускаются, у поиска есть лимиты."""

MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_FILES = 5000
SKIPPED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}
