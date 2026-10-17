"""Скилл: инструкции для типовой задачи, которые модель подгружает по id, когда они нужны."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    instructions: str
