// Боковая панель: проекты со своими сессиями, затем сессии без проекта.

export function renderSidebar(container, options) {
  const { projects, sessions, filter, selectedId, collapsed } = options;
  container.replaceChildren();
  const matches = (text) => !filter || text.toLocaleLowerCase("ru-RU").includes(filter);
  const known = new Set(projects.map((project) => project.id));
  const groups = projects
    .map((project) => ({
      project,
      sessions: sessions.filter(
        (session) => session.project_id === project.id && (matches(session.title) || matches(project.name)),
      ),
    }))
    .filter((group) => !filter || group.sessions.length > 0 || matches(group.project.name));
  const loose = sessions.filter((session) => !known.has(session.project_id) && matches(session.title));

  if (groups.length === 0 && loose.length === 0) {
    const empty = document.createElement("p");
    empty.className = "sidebar-empty";
    empty.textContent = options.emptyText;
    container.append(empty);
    return;
  }

  for (const group of groups) {
    const hasSelected = group.sessions.some((session) => session.id === selectedId);
    // При поиске и для открытой сессии группа раскрыта, иначе найденное было бы не видно.
    const expanded = !collapsed.has(group.project.id) || Boolean(filter) || hasSelected;
    container.append(projectRow(group, expanded, options));
    if (!expanded) continue;
    for (const session of group.sessions) container.append(sessionRow(session, options, true));
    if (group.sessions.length === 0) {
      const hint = document.createElement("p");
      hint.className = "project-empty";
      hint.textContent = "Сессий нет — нажмите ＋";
      container.append(hint);
    }
  }
  if (loose.length > 0) {
    if (groups.length > 0) {
      const label = document.createElement("div");
      label.className = "sidebar-group-label";
      label.textContent = "Без проекта";
      container.append(label);
    }
    for (const session of loose) container.append(sessionRow(session, options, false));
  }
}

function projectRow({ project, sessions }, expanded, options) {
  const row = document.createElement("div");
  row.className = "project-row";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "project-toggle";
  toggle.title = project.workspace;
  toggle.setAttribute("aria-expanded", String(expanded));
  const arrow = document.createElement("span");
  arrow.className = "project-arrow";
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = expanded ? "▾" : "▸";
  const name = document.createElement("span");
  name.className = "project-name";
  name.textContent = project.name;
  const count = document.createElement("span");
  count.className = "project-count";
  count.textContent = String(sessions.length);
  toggle.append(arrow, name, count);
  toggle.addEventListener("click", () => options.onToggle(project.id));

  const add = iconButton("＋", `Новая сессия в проекте «${project.name}»`, () => options.onCreate(project.id));
  add.disabled = options.actionsDisabled;
  const edit = iconButton("✎", `Настройки проекта «${project.name}»`, () => options.onEdit(project));
  row.append(toggle, add, edit);
  return row;
}

function sessionRow(session, options, inProject) {
  const row = document.createElement("div");
  row.className = "session-row";
  row.classList.toggle("in-project", inProject);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "session-item";
  button.classList.toggle("active", session.id === options.selectedId);
  button.setAttribute("aria-current", session.id === options.selectedId ? "page" : "false");

  const name = document.createElement("span");
  name.className = "session-name";
  name.textContent = session.title;
  button.append(name);
  button.addEventListener("click", () => options.onSelect(session.id));
  row.append(button);

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "session-delete-button";
  deleteButton.setAttribute("aria-label", `Удалить сессию «${session.title}»`);
  deleteButton.title = "Удалить сессию";
  deleteButton.disabled = options.actionsDisabled;
  deleteButton.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 3h4l1 4H9l1-4ZM6 7l1 14h10l1-14M10 11v6m4-6v6"/></svg>';
  deleteButton.addEventListener("click", () => options.onDelete(session.id));
  row.append(deleteButton);
  return row;
}

function iconButton(label, title, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "project-action";
  button.textContent = label;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.addEventListener("click", onClick);
  return button;
}
