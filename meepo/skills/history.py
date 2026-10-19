"""История скиллов: перед каждой правкой — копия всей папки в `data/skills/.history/<name>/<дата-время>/`.

Откат — тоже правка: текущая версия сначала уходит в историю, затем на её место встаёт выбранная.
"""

import re
import shutil
from datetime import datetime
from pathlib import Path

VERSION = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


class SkillHistory:
    def __init__(self, skills_root: Path) -> None:
        self.root = skills_root / ".history"

    def snapshot(self, folder: Path) -> str:
        """Копия папки скилла; возвращает имя версии."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        version, number = stamp, 1
        while (self.root / folder.name / version).exists():
            number += 1
            version = f"{stamp}-{number}"
        shutil.copytree(folder, self.root / folder.name / version, ignore=shutil.ignore_patterns("__pycache__"))
        return version

    def versions(self, name: str) -> list[str]:
        """Версии от новой к старой."""
        folder = self.root / name
        if not folder.is_dir():
            return []
        return sorted((path.name for path in folder.iterdir() if path.is_dir() and VERSION.fullmatch(path.name)), reverse=True)

    def restore(self, folder: Path, version: str) -> str:
        """Вернуть версию; возвращает имя версии, в которую ушло текущее состояние."""
        source = self.root / folder.name / version
        if not VERSION.fullmatch(version) or not source.is_dir():
            raise ValueError("Такой версии нет")
        saved = self.snapshot(folder) if folder.exists() else ""
        if folder.exists():
            shutil.rmtree(folder)
        shutil.copytree(source, folder)
        return saved
