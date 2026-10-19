// Панель слева: проекты (цветной значок с буквой) и их сессии. Точка у сессии — идёт ход, «ждёт» — нужна карточка.
// Что делать по щелчкам, решает app.js: actions = {select, create, rename, remove, configure}.
import { initial, tone } from "./marks.js";
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
      label: project.name, group: true, open, mark: initial(project.name), title: project.path,
      note: !open && sessions.length ? String(sessions.length) : "",
      onClick: () => (sessions.length ? toggle(state, actions, project.id) : actions.create(project.id)),
      extra: [
        rowAction("＋", "Новая сессия в проекте", () => actions.create(project.id)),
        rowAction("⚙", "Настройки проекта", () => actions.configure(project)),
      ],
    });
    head.querySelector(".trow-mark").style.setProperty("--tone", tone(project.id));
    return open ? [head, ...sessions.map((session) => sessionRow(state, actions, session))] : [head];
  });
  $("#session-list").replaceChildren(...(rows.length ? rows : [treeRow({ label: "проектов пока нет", mark: "" })]));
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
