// Справочник «Команда»: кто в команде и кто к кому может обращаться. Основной в центре, саб-агенты вокруг;
// линии — направленные связи: основной поручает любому, саб-агенты по умолчанию обращаются ко всем
// (взаимная связь — линия со стрелками с двух сторон), ограниченный в своём файле — только к выбранным.
// Щелчок по карточке открывает настройки агента; карточки можно двигать, холст — тянуть и масштабировать,
// раскладка запоминается.
import { el } from "./chat.js";

const STATUS = { idle: "свободен", queued: "в очереди", running: "работает", waiting: "ждёт вашего решения" };
const ACTIVE = new Set(["running", "queued", "waiting"]);
const SVG = "http://www.w3.org/2000/svg";
const CARD_WIDTH = 240;
const CARD_HEIGHT = 86;
const GRID = 20;
const ZOOM_MIN = 0.4;
const ZOOM_MAX = 1.6;
const ZOOM_STEP = 0.1;
const LAYOUT_KEY = "team-layout:reference";
// Наружная нормаль каждой стороны: в эту сторону провод выходит из карточки.
const SIDES = { top: { x: 0, y: -1 }, right: { x: 1, y: 0 }, bottom: { x: 0, y: 1 }, left: { x: -1, y: 0 } };

const view = { x: 40, y: 40, zoom: 1 };
// Что сейчас нарисовано: перерисовка не трогает холст, пока данные те же или карточку тянут.
let signature = "";
let layout = loadLayout();
let team = null;
let dragging = false;
let parts = null;

// data — { nodes, edges } из /api/agents/team, у узлов ещё status; actions.onOpen(node) — настройки агента.
export function renderTeam(root, data, actions) {
  if (!data) return;
  parts ??= buildCanvas(root);
  parts.actions = actions;
  const next = JSON.stringify(data);
  if (dragging || next === signature) return;
  signature = next;
  team = data;
  placeNew(data.nodes);
  parts.cards.replaceChildren(...data.nodes.map(card));
  drawEdges();
}

// Основной — сверху, саб-агенты — дугой под ним: так линии между любыми двумя агентами не прячутся за карточками.
// Сдвинутые карточки остаются, где их оставили.
function placeNew(nodes) {
  const [main, ...agents] = nodes;
  const width = Math.max(300, agents.length * 140);
  const depth = 220;
  layout[main.id] ??= { x: width, y: 0 };
  const top = { x: layout[main.id].x + CARD_WIDTH / 2, y: layout[main.id].y + CARD_HEIGHT / 2 };
  agents.forEach((node, index) => {
    if (layout[node.id]) return;
    const angle = Math.PI - (Math.PI * (index + 1)) / (agents.length + 1);
    layout[node.id] = {
      x: Math.round(top.x + width * Math.cos(angle) - CARD_WIDTH / 2),
      y: Math.round(top.y + depth * Math.sin(angle) + CARD_HEIGHT / 2),
    };
  });
}

function card(node) {
  const position = layout[node.id];
  const item = el("div", `team-node${node.builtin ? " main" : ""}`);
  item.dataset.nodeId = node.id;
  item.style.left = `${position.x}px`;
  item.style.top = `${position.y}px`;
  item.tabIndex = 0;
  item.setAttribute("role", "button");
  const head = el("span", "node-head");
  head.append(el("span", `status-dot ${node.status ?? "idle"}`), node.name);
  const reach = node.builtin ? "поручает задачи любому агенту"
    : node.restricted ? `обращается только к: ${targetsOf(node.id).map(nameOf).join(", ") || "никому"}`
    : "обращается ко всем агентам";
  item.append(head, el("span", "node-state", `${STATUS[node.status] ?? "свободен"} · ${reach}`), el("span", "node-task", node.description || ""));
  item.title = `${node.builtin ? "Дополнительные инструкции основного агента" : `Настройки агента «${node.name}»`} · карточку можно перетащить`;
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
  return item;
}

const targetsOf = (id) => (team?.edges ?? []).filter((edge) => edge.from === id).map((edge) => edge.to);

function makeDraggable(item, id) {
  item.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
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

// Пара агентов — одна линия: стрелка у того, к кому можно обратиться; взаимная связь — стрелки с двух сторон.
// Линии основного агента — акцентные: он руководитель и поручает задачи любому.
function drawEdges() {
  parts.edges.replaceChildren(defs());
  const pairs = new Map();
  for (const edge of team?.edges ?? []) {
    const key = [edge.from, edge.to].sort().join("\u0000");
    const pair = pairs.get(key) ?? { a: edge.from, b: edge.to, forward: false, back: false };
    if (edge.from === pair.a) pair.forward = true;
    else pair.back = true;
    pairs.set(key, pair);
  }
  for (const pair of pairs.values()) {
    const source = rect(pair.a);
    const target = rect(pair.b);
    if (!source || !target) continue;
    const { start, startSide, end, endSide } = route(source, target);
    const lead = pair.a === "main" || pair.b === "main";
    const busy = [pair.a, pair.b].some((id) => ACTIVE.has(team.nodes.find((node) => node.id === id)?.status));
    const group = svg("g", { class: `edge-group${lead ? " lead" : ""}${pair.forward && pair.back ? " mutual" : ""}${busy ? " active" : ""}` });
    const line = svg("path", { d: bezier(start, startSide, end, endSide), class: "edge" });
    if (pair.forward) line.setAttribute("marker-end", "url(#team-arrow)");
    if (pair.back) line.setAttribute("marker-start", "url(#team-arrow)");
    const tip = svg("title", {});
    tip.textContent = lead ? `${nameOf("main")} поручает задачи агенту ${nameOf(pair.a === "main" ? pair.b : pair.a)}`
      : pair.forward && pair.back ? `${nameOf(pair.a)} и ${nameOf(pair.b)} могут обращаться друг к другу`
      : pair.forward ? `${nameOf(pair.a)} может обращаться к ${nameOf(pair.b)}` : `${nameOf(pair.b)} может обращаться к ${nameOf(pair.a)}`;
    line.append(tip);
    group.append(line);
    parts.edges.append(group);
  }
}

const nameOf = (id) => team?.nodes.find((node) => node.id === id)?.name ?? id;

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
    signature = "";
    if (team) renderTeam(root, team, parts.actions);
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

function loadLayout() {
  try {
    return JSON.parse(localStorage.getItem(LAYOUT_KEY)) ?? {};
  } catch {
    return {};
  }
}

function saveLayout() {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  } catch {
    // Без хранилища раскладка живёт до перезагрузки страницы.
  }
}

function svg(name, attributes) {
  const item = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, value);
  return item;
}
