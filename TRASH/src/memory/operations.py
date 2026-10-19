"""Модели операций памяти. Полный контракт: docs/memory-contract.md."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PATCH_VERSION = 2
MAX_OPERATIONS = 100


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    file: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*\.md$")
    source_message_ids: list[str] = Field(min_length=1)

    @field_validator("source_message_ids")
    @classmethod
    def unique_sources(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value) or len(value) != len(set(value)):
            raise ValueError("source_message_ids должны быть непустыми и уникальными")
        return value


class AddOperation(_Operation):
    """Новая строка памяти в указанном разделе."""

    op: Literal["add"]
    section: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("section", "content")
    @classmethod
    def single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("запись памяти и название раздела должны занимать одну строку")
        return value


class UpdateOperation(_Operation):
    """Замена единственной строки с точно указанным прежним текстом."""

    op: Literal["update"]
    old_content: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("old_content", "content")
    @classmethod
    def single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("запись памяти должна занимать одну строку")
        return value


class DeleteOperation(_Operation):
    """Удаление единственной строки с точно указанным текстом."""

    op: Literal["delete"]
    old_content: str = Field(min_length=1, max_length=2000)

    @field_validator("old_content")
    @classmethod
    def single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("запись памяти должна занимать одну строку")
        return value


MemoryOperation = Annotated[
    AddOperation | UpdateOperation | DeleteOperation,
    Field(discriminator="op"),
]


class MemoryPatch(BaseModel):
    """Проверенный набор операций, который применяется целиком или не применяется вовсе."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[2]
    operations: list[MemoryOperation] = Field(max_length=MAX_OPERATIONS)
