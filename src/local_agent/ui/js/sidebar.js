// Боковая панель показывает сессии и прямую кнопку удаления при наведении.

export function renderSessions(
  container, sessions, selectedId, onSelect, onDelete, actionsDisabled = false, emptyText = "Пока нет сессий"
) {
  container.replaceChildren();

  if (sessions.length === 0) {
    const empty = document.createElement("p");
    empty.className = "sidebar-empty";
    empty.textContent = emptyText;
    container.append(empty);
    return;
  }

  for (const session of sessions) {
    const row = document.createElement("div");
    row.className = "session-row";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-item";
    button.classList.toggle("active", session.id === selectedId);
    button.setAttribute("aria-current", session.id === selectedId ? "page" : "false");

    const name = document.createElement("span");
    name.className = "session-name";
    name.textContent = session.title;
    button.append(name);
    button.addEventListener("click", () => onSelect(session.id));
    row.append(button);

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "session-delete-button";
    deleteButton.setAttribute("aria-label", `Удалить сессию «${session.title}»`);
    deleteButton.title = "Удалить сессию";
    deleteButton.disabled = actionsDisabled;
    deleteButton.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 3h4l1 4H9l1-4ZM6 7l1 14h10l1-14M10 11v6m4-6v6"/></svg>';
    deleteButton.addEventListener("click", () => onDelete(session.id));
    row.append(deleteButton);

    container.append(row);
  }
}
