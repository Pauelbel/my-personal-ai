// Панель слева: обычные чаты, проекты и их сессии. Точка у сессии — идёт ход, «ждёт» — нужна карточка.
// Что делать по щелчкам, решает app.js: actions = {select, create, rename, remove, configure}.
import { editLabel, rowAction, treeRow } from "./tree.js";
import { remember } from "./util.js";

const $ = (selector) => document.querySelector(selector);

export function renderSidebar(state, actions) {
  // Пока название правят, дерево не перерисовывается: поле ввода пропало бы.
  if ($("#session-list .trow-input")) return;
  const rows = state.projects.flatMap((project) => {
    const sessions = state.sessions.filter((session) => session.project_id === project.id);
    const open = !state.collapsed.has(project.id);
    const head = treeRow({
      label: project.name, group: true, open, icon: sidebarIcon("folder"), title: project.path,
      note: !open && sessions.length ? String(sessions.length) : "",
      onClick: () => (sessions.length ? toggle(state, actions, project.id) : actions.create(project.id)),
      extra: [
        rowAction("＋", "Новая сессия в проекте", () => actions.create(project.id)),
        rowAction("⚙", "Настройки проекта", () => actions.configure(project)),
      ],
    });
    return open ? [head, ...sessions.map((session) => sessionRow(state, actions, session))] : [head];
  });
  const standalone = state.sessions.filter((session) => session.project_id == null);
  const open = !state.collapsed.has("__no_project__");
  const head = treeRow({
    label: "Чаты", group: true, open, icon: sidebarIcon("chat"), note: !open && standalone.length ? String(standalone.length) : "",
    onClick: () => toggle(state, actions, "__no_project__"),
    extra: [rowAction("＋", "Новый чат", () => actions.create(null))],
  });
  $("#session-list").replaceChildren(head, ...(open ? standalone.map((session) => sessionRow(state, actions, session)) : []), ...rows);
}

function sidebarIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", name === "folder"
    ? "M3.5 6.5h6l2 2h9v9a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"
    : "M5.5 5.5h13a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-7l-4.5 3v-3H5.5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2z");
  svg.append(path);
  return svg;
}

function sessionRow(state, actions, session) {
  const waiting = Boolean(session.pending);
  const running = state.live && session.id === state.current;
  const rename = () => editLabel(row, session.title, (title) => actions.rename(session, title));
  const row = treeRow({
    label: session.title, title: `${session.title}\nДвойной щелчок — переименовать`, current: session.id === state.current,
    mark: "", state: waiting ? "waiting" : running ? "running" : "", note: waiting ? "ждёт" : "", noteClass: "waiting",
    onClick: () => actions.select(session.id),
    extra: [rowAction("✎", "Переименовать", rename), rowAction("×", "Удалить сессию", () => actions.remove(session))],
  });
  row.addEventListener("dblclick", rename);
  return row;
}

function toggle(state, actions, id) {
  if (!state.collapsed.delete(id)) state.collapsed.add(id);
  remember("collapsed-projects", JSON.stringify([...state.collapsed]));
  renderSidebar(state, actions);
}
