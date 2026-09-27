"""Модели операций ограничивают точечные изменения долговременной памяти."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    file: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*\.md$")
    source_message_ids: list[str] = Field(min_length=1)

    @field_validator("source_message_ids")
    @classmethod
    def unique_sources(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value) or len(value) != len(set(value)):
            raise ValueError("source_message_ids must be non-empty and unique")
        return value


class AddOperation(_Operation):
    op: Literal["add"]
    section: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("section", "content")
    @classmethod
    def single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("memory entries and section names must use one line")
        return value


class UpdateOperation(_Operation):
    op: Literal["update"]
    entry_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("memory entries must use one line")
        return value


class DeleteOperation(_Operation):
    op: Literal["delete"]
    entry_id: str = Field(pattern=r"^[a-f0-9]{32}$")


MemoryOperation = Annotated[
    AddOperation | UpdateOperation | DeleteOperation,
    Field(discriminator="op"),
]


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    operations: list[MemoryOperation] = Field(max_length=100)
