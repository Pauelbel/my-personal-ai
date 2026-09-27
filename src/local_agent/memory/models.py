"""Модель сообщения представляет запись разговора независимо от базы и API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Message(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
