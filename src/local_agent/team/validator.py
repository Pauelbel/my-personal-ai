"""Проверка холста: всё, на что он ссылается, должно существовать."""

from collections.abc import Callable

from local_agent.team.models import ENTRY_NODE_ID, Canvas


def canvas_errors(canvas: Canvas, agent_exists: Callable[[str], bool]) -> list[str]:
    """Пустой список — команду можно запускать. Циклы разрешены: их ограничивает лимит шагов."""
    errors = []
    node_ids = [node.id for node in canvas.nodes]
    if duplicates := sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1}):
        errors.append(f"Повторяются id узлов: {', '.join(duplicates)}")
    for node in canvas.nodes:
        if node.id != ENTRY_NODE_ID and not agent_exists(node.agent_id):
            errors.append(f"Узел {node.name}: агент {node.agent_id} не найден")
    pairs = [(edge.source, edge.target) for edge in canvas.edges]
    for source, target in dict.fromkeys(pairs):
        if source not in node_ids or target not in node_ids:
            errors.append(f"Стрелка {source} → {target} ссылается на несуществующий узел")
        if source == target:
            errors.append(f"Стрелка {source} → {target} ведёт в тот же узел")
        if pairs.count((source, target)) > 1:
            errors.append(f"Стрелка {source} → {target} повторяется")
    return errors
