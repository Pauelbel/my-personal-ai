"""Картинки из чата вики: файлы в data/attachments, в сообщении хранятся только их имена.

Имя — случайный id с расширением по типу картинки: по нему файл находят и отдают, а путь к чужому
файлу в имя не подставить. В заметку вики уходит копия картинки.
"""

import base64
import binascii
import re
from pathlib import Path
from uuid import uuid4

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DATA_URL = re.compile(r"^data:(image/[a-z+.-]+);base64,(.+)$", re.DOTALL)
NAME = re.compile(r"^[0-9a-f]{12}\.(png|jpg|webp|gif)$")


class AttachmentStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, data_url: str) -> str:
        """Картинка из data URL (так её присылает интерфейс) — в файл; возвращает имя для сообщения."""
        match = DATA_URL.match(data_url.strip())
        if not match or match[1] not in IMAGE_TYPES:
            raise ValueError("Нужна картинка PNG, JPEG, WebP или GIF")
        try:
            data = base64.b64decode(match[2], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Картинка повреждена") from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"Картинка больше {MAX_IMAGE_BYTES // 1024 // 1024} МБ")
        name = f"{uuid4().hex[:12]}.{IMAGE_TYPES[match[1]]}"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(data)
        return name

    def path(self, name: str) -> Path | None:
        """Файл картинки по имени из сообщения; None — имя не такого вида или файла уже нет."""
        if not isinstance(name, str) or not NAME.fullmatch(name):
            return None
        path = self.root / name
        return path if path.is_file() else None
