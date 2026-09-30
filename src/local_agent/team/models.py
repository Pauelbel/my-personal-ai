"""Холст сессии: агенты, которым агент сессии может поручать работу, и стрелки «кто кому пишет»."""

from pydantic import BaseModel, ConfigDict, Field

# Карточка агента самой сессии: через неё сообщение пользователя входит в команду и через неё возвращается итог.
ENTRY_NODE_ID = "main"


class CanvasNode(BaseModel):
    """Агент на холсте. Инструменты и навыки узел берёт у агента целиком."""

    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    name: str
    # У входной карточки агент — агент сессии, поле не используется.
    agent_id: str = ""
    # Место и размер карточки на холсте; на работу команды не влияют. Размер None — по содержимому.
    x: float = 0
    y: float = 0
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)


class CanvasEdge(BaseModel):
    """Стрелка — разрешение писать от узла `from` узлу `to`. Обратная связь — отдельная стрелка."""

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")


class Canvas(BaseModel):
    nodes: list[CanvasNode] = Field(default_factory=list)
    edges: list[CanvasEdge] = Field(default_factory=list)

    def node(self, node_id: str) -> CanvasNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def recipients(self, node_id: str) -> list[CanvasNode]:
        """Узлы, которым этот узел может написать."""
        targets = {edge.target for edge in self.edges if edge.source == node_id}
        return [node for node in self.nodes if node.id in targets]
