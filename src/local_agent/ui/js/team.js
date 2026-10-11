// Команда сессии: холст со связями «основной агент → саб-агенты» и полоса переключения над чатом.
// Связи рисует сам основной агент, когда поручает задачи; щелчок по агенту открывает разговор с ним.
// Карточки можно двигать, холст — тянуть и масштабировать; раскладка запоминается для каждой сессии.
// Связь между саб-агентами (кто кого может спросить посреди задачи) тянется от точки на краю карточки
// к другой карточке и удаляется щелчком по линии; хранится в файлах агентов.
import { el } from "./chat.js";

const STATUS = { idle: "свободен", queued: "в очереди", running: "работает", waiting: "ждёт вашего решения", done: "выполнил задачу", failed: "ошибка" };
const ACTIVE = new Set(["running", "queued", "waiting"]);
const SVG = "http://www.w3.org/2000/svg";
const CARD_WIDTH = 240;
const CARD_HEIGHT = 86;
const COLUMN_GAP = 140;
const ROW_GAP = 18;
const GRID = 20;
const ZOOM_MIN = 0.4;
const ZOOM_MAX = 1.6;
const ZOOM_STEP = 0.1;
// Наружная нормаль каждой стороны: в эту сторону провод выходит из карточки.
const SIDES = { top: { x: 0, y: -1 }, right: { x: 1, y: 0 }, bottom: { x: 0, y: 1 }, left: { x: -1, y: 0 } };

const view = { x: 40, y: 40, zoom: 1 };
// Что сейчас нарисовано: перерисовка по опросу не трогает холст, пока данные те же или карточку тянут.
let drawn = { owner: null, signature: "" };
let layout = {};
let team = null;
let shownCurrent = null;
let dragging = false;
let parts = null;

// onOpen(node) открывает разговор с агентом узла.
export function renderAgentBar(root, team, currentId, onOpen) {
  const agents = team?.nodes.filter((node) => node.id !== "main") ?? [];
  root.hidden = !agents.length;
  if (!agents.length) return;
  root.replaceChildren(...team.nodes.map((node) => {
    const chip = el("button", "agent-chip");
    chip.type = "button";
    chip.setAttribute("aria-current", String(node.session_id === currentId));
    chip.title = node.task ? `${STATUS[node.status] || node.status}: ${node.task}` : STATUS[node.status] || "";
    chip.append(el("span", `status-dot ${node.status}`), node.id === "main" ? "Основной" : node.name);
    chip.addEventListener("click", () => onOpen(node));
    return chip;
  }));
}

// actions: onOpen(node) — разговор с агентом, onLink(from, to) и onUnlink(from, to) — связи между саб-агентами.
export function renderTeam(root, data, currentId, actions) {
  if (!data) return;
  parts ??= buildCanvas(root);
  parts.actions = actions;
  const signature = JSON.stringify([data, currentId]);
  if (dragging || (drawn.owner === data.owner_id && drawn.signature === signature)) return;
  if (drawn.owner !== data.owner_id) {
    layout = loadLayout(data.owner_id);
    Object.assign(view, { x: 40, y: 40, zoom: 1 });
    applyView();
  }
  drawn = { owner: data.owner_id, signature };
  team = data;
  shownCurrent = currentId;
  placeNew(data.nodes);
  parts.cards.replaceChildren(...data.nodes.map((node) => card(node, currentId)));
  drawEdges();
}

// Новые агенты встают в столбик справа от основного, под уже стоящими; сдвинутые остаются, где их оставили.
function placeNew(nodes) {
  const [main, ...agents] = nodes;
  layout[main.id] ??= { x: 0, y: Math.max(0, (agents.length * (CARD_HEIGHT + ROW_GAP) - CARD_HEIGHT) / 2) };
  const column = layout[main.id].x + CARD_WIDTH + COLUMN_GAP;
  let next = Math.max(0, ...agents.filter((node) => layout[node.id]).map((node) => layout[node.id].y + CARD_HEIGHT + ROW_GAP));
  for (const node of agents) {
    if (layout[node.id]) continue;
    layout[node.id] = { x: column, y: next };
    next += CARD_HEIGHT + ROW_GAP;
  }
}

function card(node, currentId) {
  const position = layout[node.id];
  const item = el("div", `team-node${node.session_id && node.session_id === currentId ? " current" : ""}${node.archived ? " archived" : ""}`);
  item.dataset.nodeId = node.id;
  item.style.left = `${position.x}px`;
  item.style.top = `${position.y}px`;
  item.tabIndex = 0;
  item.setAttribute("role", "button");
  const head = el("span", "node-head");
  head.append(el("span", `status-dot ${node.status}`), node.name);
  item.append(head, el("span", "node-state", STATUS[node.status] || node.status), el("span", "node-task", node.task || node.description || ""));
  item.title = `${node.id === "main" ? "Открыть основной разговор" : `Открыть разговор с ${node.name}`} · карточку можно перетащить`;
  const open = () => parts.actions.onOpen(node);
  item.addEventListener("click", () => {
    if (!item.dataset.dragged) open();
  });
  item.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });
  makeDraggable(item, node.id);
  // Связывать можно только саб-агентов: основной и так может поручить задачу любому.
  if (node.id !== "main" && !node.archived) {
    for (const side of Object.keys(SIDES)) item.append(port(node.id, side));
  }
  return item;
}

function makeDraggable(item, id) {
  item.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest(".node-port")) return;
    event.preventDefault();
    event.stopPropagation();
    item.setPointerCapture(event.pointerId);
    dragging = true;
    item.classList.add("dragging");
    const position = layout[id];
    const start = { x: event.clientX, y: event.clientY, nodeX: position.x, nodeY: position.y };
    const move = (moved) => {
      // Карточки липнут к сетке, чтобы их было легко выровнять.
      position.x = Math.round((start.nodeX + (moved.clientX - start.x) / view.zoom) / (GRID / 2)) * (GRID / 2);
      position.y = Math.round((start.nodeY + (moved.clientY - start.y) / view.zoom) / (GRID / 2)) * (GRID / 2);
      item.style.left = `${position.x}px`;
      item.style.top = `${position.y}px`;
      drawEdges();
    };
    const stop = () => {
      item.removeEventListener("pointermove", move);
      item.classList.remove("dragging");
      dragging = false;
      if (position.x === start.nodeX && position.y === start.nodeY) return;
      // Щелчок, который браузер пришлёт сразу после отпускания, — конец перетаскивания, а не открытие разговора.
      item.dataset.dragged = "1";
      setTimeout(() => { delete item.dataset.dragged; }, 0);
      saveLayout();
    };
    item.addEventListener("pointermove", move);
    item.addEventListener("pointerup", stop, { once: true });
    item.addEventListener("pointercancel", stop, { once: true });
  });
}

function drawEdges() {
  parts.edges.replaceChildren(defs());
  for (const edge of team?.edges ?? []) {
    const source = rect(edge.from);
    const target = rect(edge.to);
    if (!source || !target) continue;
    const { start, startSide, end, endSide } = route(source, target);
    const path = bezier(start, startSide, end, endSide);
    const status = team.nodes.find((node) => node.id === edge.to)?.status;
    const group = svg("g", { class: `edge-group${edge.link ? " link" : ""}${ACTIVE.has(status) ? " active" : ""}` });
    group.append(
      svg("path", { d: path, class: "edge", "marker-end": "url(#team-arrow)" }),
      svg("circle", { cx: start.x, cy: start.y, r: 2.5, class: "edge-dot" }),
    );
    if (edge.tasks) {
      const label = svg("text", { class: "edge-label", x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 - 8, "text-anchor": "middle" });
      label.textContent = `задач: ${edge.tasks}`;
      group.append(label);
    }
    if (edge.link) {
      // Широкая прозрачная линия поверх: по связи легко попасть мышью.
      const hit = svg("path", { d: path, class: "edge-hit" });
      const tip = svg("title", {});
      tip.textContent = `${nameOf(edge.from)} может спросить ${nameOf(edge.to)} · щелчок — убрать связь`;
      hit.append(tip);
      hit.addEventListener("click", () => {
        if (window.confirm(`Убрать связь: ${nameOf(edge.from)} больше не сможет спрашивать ${nameOf(edge.to)}?`)) parts.actions.onUnlink(edge.from, edge.to);
      });
      group.append(hit);
    }
    parts.edges.append(group);
  }
}

const nameOf = (id) => team?.nodes.find((node) => node.id === id)?.name ?? id;

// Точка на краю карточки: от неё тянут связь к другому саб-агенту.
function port(id, side) {
  const handle = el("button", `node-port port-${side}`);
  handle.type = "button";
  handle.title = "Потяните к другому саб-агенту: этот сможет его спрашивать";
  handle.setAttribute("aria-label", "Связать с другим саб-агентом");
  handle.addEventListener("click", (event) => event.stopPropagation());
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    handle.setPointerCapture(event.pointerId);
    dragging = true;
    const line = svg("path", { class: "edge draft", "marker-end": "url(#team-arrow)" });
    parts.edges.append(line);
    const from = sidePoint(rect(id), side);
    let target = null;
    const move = (moved) => {
      const bounds = parts.root.getBoundingClientRect();
      const point = { x: (moved.clientX - bounds.left - view.x) / view.zoom, y: (moved.clientY - bounds.top - view.y) / view.zoom };
      line.setAttribute("d", `M${from.x} ${from.y}L${point.x} ${point.y}`);
      // Отпустить можно в любом месте карточки: подсвечиваем, кому уйдёт связь.
      const over = document.elementFromPoint(moved.clientX, moved.clientY)?.closest(".team-node");
      const next = over && over.dataset.nodeId !== id && over.dataset.nodeId !== "main" ? over : null;
      if (next === target) return;
      target?.classList.remove("drop-target");
      next?.classList.add("drop-target");
      target = next;
    };
    const finish = () => {
      handle.removeEventListener("pointermove", move);
      target?.classList.remove("drop-target");
      line.remove();
      dragging = false;
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", () => {
      const chosen = target?.dataset.nodeId;
      finish();
      if (chosen) parts.actions.onLink(id, chosen);
    }, { once: true });
    handle.addEventListener("pointercancel", finish, { once: true });
  });
  return handle;
}

function defs() {
  const node = svg("defs", {});
  node.innerHTML = '<marker id="team-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 1L9 5L0 9z" fill="currentColor"/></marker>';
  return node;
}

// Связь соединяет стороны карточек, обращённые друг к другу: боковые, если карточки стоят рядом,
// верх и низ — если одна над другой.
function route(source, target) {
  const dx = target.x + target.width / 2 - (source.x + source.width / 2);
  const dy = target.y + target.height / 2 - (source.y + source.height / 2);
  // Сравниваем зазоры между карточками, а не расстояние между центрами: карточки бывают высокими.
  const gapX = Math.abs(dx) - (source.width + target.width) / 2;
  const gapY = Math.abs(dy) - (source.height + target.height) / 2;
  const [startSide, endSide] = gapX >= gapY
    ? (dx >= 0 ? ["right", "left"] : ["left", "right"])
    : (dy >= 0 ? ["bottom", "top"] : ["top", "bottom"]);
  return { start: sidePoint(source, startSide), startSide, end: sidePoint(target, endSide), endSide };
}

function sidePoint(box, side) {
  if (side === "top" || side === "bottom") return { x: box.x + box.width / 2, y: side === "top" ? box.y : box.y + box.height };
  return { x: side === "left" ? box.x : box.x + box.width, y: box.y + box.height / 2 };
}

// Плавная кривая, которая выходит из стороны и входит в сторону перпендикулярно, как провод между блоками.
function bezier(start, startSide, end, endSide) {
  const reach = Math.min(160, Math.max(40, Math.hypot(end.x - start.x, end.y - start.y) / 2));
  const out = SIDES[startSide];
  const into = SIDES[endSide];
  return `M${start.x} ${start.y}C${start.x + out.x * reach} ${start.y + out.y * reach} `
    + `${end.x + into.x * reach} ${end.y + into.y * reach} ${end.x} ${end.y}`;
}

function rect(id) {
  const item = parts.cards.querySelector(`[data-node-id="${CSS.escape(id)}"]`);
  return item && { x: item.offsetLeft, y: item.offsetTop, width: item.offsetWidth, height: item.offsetHeight };
}

// Холст строится один раз: пустое место тянется мышью, колесо двигает холст, Ctrl + колесо меняет масштаб у курсора.
function buildCanvas(root) {
  root.classList.add("team-canvas");
  const stage = el("div", "team-stage");
  const edges = svg("svg", { class: "team-edges", "aria-hidden": "true" });
  const cards = el("div", "team-cards");
  stage.append(edges, cards);
  const controls = el("div", "zoom-controls");
  const zoomOut = control("−", "Отдалить", () => zoomAtCenter(view.zoom - ZOOM_STEP));
  const zoomReset = control("100%", "Масштаб 100%", () => {
    Object.assign(view, { x: 40, y: 40, zoom: 1 });
    applyView();
  });
  const zoomIn = control("+", "Приблизить", () => zoomAtCenter(view.zoom + ZOOM_STEP));
  const arrange = control("Упорядочить", "Вернуть карточки на места по умолчанию", () => {
    layout = {};
    saveLayout();
    drawn.signature = "";
    if (team) renderTeam(root, team, shownCurrent, parts.actions);
  });
  controls.append(zoomOut, zoomReset, zoomIn, arrange);
  root.replaceChildren(stage, controls);

  root.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest(".team-node, .zoom-controls")) return;
    root.setPointerCapture(event.pointerId);
    root.classList.add("panning");
    const start = { x: event.clientX - view.x, y: event.clientY - view.y };
    const move = (moved) => {
      view.x = moved.clientX - start.x;
      view.y = moved.clientY - start.y;
      applyView();
    };
    const stop = () => {
      root.classList.remove("panning");
      root.removeEventListener("pointermove", move);
    };
    root.addEventListener("pointermove", move);
    root.addEventListener("pointerup", stop, { once: true });
    root.addEventListener("pointercancel", stop, { once: true });
  });
  root.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) zoomAt(event.clientX, event.clientY, view.zoom * (event.deltaY < 0 ? 1.1 : 1 / 1.1));
    else {
      view.x -= event.deltaX;
      view.y -= event.deltaY;
      applyView();
    }
  }, { passive: false });
  return { root, stage, edges, cards, zoomReset, actions: {} };
}

function control(text, title, onClick) {
  const button = el("button", "", text);
  button.type = "button";
  button.title = title;
  button.addEventListener("click", onClick);
  return button;
}

function zoomAtCenter(zoom) {
  const bounds = parts.root.getBoundingClientRect();
  zoomAt(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2, zoom);
}

function zoomAt(clientX, clientY, zoom) {
  const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(zoom * 100) / 100));
  const bounds = parts.root.getBoundingClientRect();
  const pointX = clientX - bounds.left;
  const pointY = clientY - bounds.top;
  // Точка под курсором остаётся на месте, меняется всё вокруг неё.
  view.x = pointX - (pointX - view.x) * next / view.zoom;
  view.y = pointY - (pointY - view.y) * next / view.zoom;
  view.zoom = next;
  applyView();
}

function applyView() {
  if (!parts) return;
  parts.stage.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`;
  parts.root.style.backgroundPosition = `${view.x}px ${view.y}px`;
  parts.root.style.backgroundSize = `${GRID * view.zoom}px ${GRID * view.zoom}px`;
  parts.zoomReset.textContent = `${Math.round(view.zoom * 100)}%`;
}

function loadLayout(owner) {
  try {
    return JSON.parse(localStorage.getItem(`team-layout:${owner}`)) ?? {};
  } catch {
    return {};
  }
}

function saveLayout() {
  try {
    localStorage.setItem(`team-layout:${drawn.owner}`, JSON.stringify(layout));
  } catch {
    // Без хранилища раскладка живёт до перезагрузки страницы.
  }
}

function svg(name, attributes) {
  const item = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, value);
  return item;
}
