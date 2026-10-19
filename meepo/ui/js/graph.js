// Граф вики как в Obsidian: точка — заметка, линия — ссылка между заметками.
// Размер точки — по числу связей, цвет — по кластеру (его считает сервер). Подписи видны при приближении,
// а при отдалении — только у точки под курсором и её соседей. Щелчок по точке открывает заметку.
// Рисуется в SVG, а не на canvas: цвета берутся из CSS-токенов темы, и тёмный режим браузера
// (автотёмная тема Chrome, Dark Reader) перекрашивает граф вместе со всей страницей.
import { el } from "./util.js";

const SVG = "http://www.w3.org/2000/svg";
const CLUSTER_COLORS = 8;
const LABELS_FROM = 0.9;    // масштаб, с которого подписи проявляются
const LABELS_FULL = 1.3;    // и с которого видны полностью
const LABEL_PX = 12;
const MIN_SCALE = 0.15;
const MAX_SCALE = 4;
const LINK_LENGTH = 70;
const CLICK_PX = 4;
// Отталкивание точек и притяжение к центру: несвязанные группы держатся рядом, а не разлетаются.
const REPULSION = 700;
const GRAVITY = 0.035;

// onOpen(path) — открыть заметку. update(data, current) можно звать сколько угодно: точки, которые уже были,
// остаются на местах, новые встают рядом с соседями, и раскладка дорабатывается.
export function createGraph(root, { onOpen }) {
  const svg = shape("svg", "graph-svg");
  const scene = shape("g");
  const edgeLayer = shape("g");
  const nodeLayer = shape("g");
  const labelLayer = shape("g", "graph-labels");
  const ring = shape("circle", "graph-ring");
  scene.append(edgeLayer, nodeLayer, ring, labelLayer);
  svg.append(scene);
  const empty = el("p", "hint graph-empty", "Заметок пока нет.");
  // Легенда: сплошная линия — ссылка между заметками, пунктир — сервер нашёл, что они близки по тексту.
  const legend = el("div", "graph-legend");
  legend.append(el("span", "graph-legend-link", "ссылка"), el("span", "graph-legend-similar", "близки по тексту"));
  root.replaceChildren(svg, empty, legend);
  let nodes = [];
  let edges = [];
  let byPath = new Map();
  let neighbours = new Map();
  let current = null;
  let hover = null;
  let view = { x: 0, y: 0, k: 1 };
  let fitted = false;
  let alpha = 0;
  let frame = 0;
  let size = { width: 0, height: 0 };
  let gesture = null;

  function update(data, selected) {
    current = selected;
    const old = byPath;
    byPath = new Map();
    nodes = data.nodes.map((item, index) => {
      const known = old.get(item.path);
      const node = known ? Object.assign(known, item) : { ...item, x: 0, y: 0, vx: 0, vy: 0, fresh: true, seed: index };
      node.r = 5 + 2.5 * Math.sqrt(node.degree);
      byPath.set(node.path, node);
      return node;
    });
    edges = data.edges
      .map((edge) => ({ a: byPath.get(edge.from), b: byPath.get(edge.to), kind: edge.kind ?? "link", score: edge.score }))
      .filter(({ a, b }) => a && b);
    neighbours = new Map(nodes.map((node) => [node, new Set()]));
    for (const { a, b } of edges) {
      neighbours.get(a).add(b);
      neighbours.get(b).add(a);
    }
    let placed = false;
    for (const node of nodes) {
      if (!node.fresh) continue;
      // Новая точка — рядом с уже стоящим соседом, иначе по спирали вокруг центра.
      const anchor = [...neighbours.get(node)].find((other) => !other.fresh);
      const angle = node.seed * 2.4;
      const radius = anchor ? LINK_LENGTH : 40 * Math.sqrt(node.seed + 1);
      node.x = (anchor?.x ?? 0) + radius * Math.cos(angle);
      node.y = (anchor?.y ?? 0) + radius * Math.sin(angle);
      node.fresh = false;
      placed = true;
    }
    if (hover && !byPath.has(hover.path)) hover = null;
    build();
    empty.hidden = nodes.length > 0;
    if (placed || !fitted) heat(1);
    draw();
  }

  function setCurrent(path) {
    current = path;
    mark();
    draw();
  }

  // ---------- элементы ----------

  // Элементы пересоздаются только при обновлении данных; кадры раскладки лишь двигают их.
  function build() {
    edgeLayer.replaceChildren(...edges.map((edge) => {
      edge.line = shape("line", `graph-edge ${edge.kind}`);
      const tip = shape("title");
      tip.textContent = edge.kind === "similar"
        ? `Близки по тексту: ${edge.a.title} — ${edge.b.title} (${Math.round(edge.score * 100)}%)`
        : `Ссылка: ${edge.a.title} — ${edge.b.title}`;
      edge.line.append(tip);
      return edge.line;
    }));
    nodeLayer.replaceChildren(...nodes.map((node) => {
      const kind = node.cluster >= 0 ? `c${(node.cluster % CLUSTER_COLORS) + 1}` : "single";
      node.dot = shape("circle", `graph-node ${kind}`);
      const tip = shape("title");
      tip.textContent = `${node.title}\n${node.path}`;
      node.dot.append(tip);
      return node.dot;
    }));
    labelLayer.replaceChildren(...nodes.map((node) => {
      node.label = shape("text", "graph-label");
      node.label.textContent = shorten(node.title);
      return node.label;
    }));
    mark();
  }

  // Подсветка: точка под курсором с соседями и открытая заметка. Меняется только по наведению.
  function mark() {
    const near = hover ? neighbours.get(hover) : null;
    svg.classList.toggle("hovering", Boolean(hover));
    for (const node of nodes) {
      const lit = node === hover || Boolean(near?.has(node));
      node.dot.classList.toggle("lit", lit);
      node.label.classList.toggle("lit", lit || node.path === current);
    }
    for (const edge of edges) edge.line.classList.toggle("lit", Boolean(hover) && (edge.a === hover || edge.b === hover));
  }

  // ---------- раскладка ----------

  function heat(value) {
    alpha = Math.max(alpha, value);
    if (!frame) frame = requestAnimationFrame(tick);
  }

  function tick() {
    frame = 0;
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let distance2 = dx * dx + dy * dy;
        if (distance2 < 0.01) {
          dx = Math.random() - 0.5;
          dy = Math.random() - 0.5;
          distance2 = 0.5;
        }
        if (distance2 > 160_000) continue;
        const force = (REPULSION / distance2) * alpha;
        a.vx -= dx * force;
        a.vy -= dy * force;
        b.vx += dx * force;
        b.vy += dy * force;
      }
    }
    for (const { a, b } of edges) {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.hypot(dx, dy) || 1;
      const force = ((distance - LINK_LENGTH) / distance) * 0.08 * alpha;
      a.vx += dx * force;
      a.vy += dy * force;
      b.vx -= dx * force;
      b.vy -= dy * force;
    }
    for (const node of nodes) {
      node.vx -= node.x * GRAVITY * alpha;
      node.vy -= node.y * GRAVITY * alpha;
      if (gesture?.node === node) {
        node.vx = node.vy = 0;
        continue;
      }
      node.vx *= 0.6;
      node.vy *= 0.6;
      node.x += node.vx;
      node.y += node.vy;
    }
    alpha *= 0.985;
    if (!fitted && alpha < 0.3) fit();
    draw();
    if (alpha > 0.02 || gesture?.node) frame = requestAnimationFrame(tick);
  }

  // Весь граф в окне: после первой раскладки и по двойному щелчку по пустому месту.
  function fit() {
    if (!nodes.length || !size.width) return;
    fitted = true;
    const xs = nodes.map((node) => node.x);
    const ys = nodes.map((node) => node.y);
    const width = Math.max(...xs) - Math.min(...xs) + 120;
    const height = Math.max(...ys) - Math.min(...ys) + 120;
    const k = clamp(Math.min(size.width / width, size.height / height), MIN_SCALE, 1.6);
    view = { k, x: -k * (Math.max(...xs) + Math.min(...xs)) / 2, y: -k * (Math.max(...ys) + Math.min(...ys)) / 2 };
  }

  // ---------- рисование ----------

  function draw() {
    scene.setAttribute("transform", `translate(${size.width / 2 + view.x} ${size.height / 2 + view.y}) scale(${view.k})`);
    // Подписи и их обводка — постоянного экранного размера; при отдалении видны только у выделенных точек.
    labelLayer.style.fontSize = `${LABEL_PX / view.k}px`;
    labelLayer.style.strokeWidth = `${3 / view.k}px`;
    svg.style.setProperty("--label-opacity", String(clamp((view.k - LABELS_FROM) / (LABELS_FULL - LABELS_FROM), 0, 1)));
    for (const { a, b, line } of edges) {
      line.setAttribute("x1", a.x);
      line.setAttribute("y1", a.y);
      line.setAttribute("x2", b.x);
      line.setAttribute("y2", b.y);
    }
    for (const node of nodes) {
      node.dot.setAttribute("cx", node.x);
      node.dot.setAttribute("cy", node.y);
      node.dot.setAttribute("r", node.r);
      node.label.setAttribute("x", node.x);
      node.label.setAttribute("y", node.y + node.r + 4 / view.k);
    }
    const opened = current ? byPath.get(current) : null;
    ring.classList.toggle("shown", Boolean(opened));
    if (opened) {
      ring.setAttribute("cx", opened.x);
      ring.setAttribute("cy", opened.y);
      ring.setAttribute("r", opened.r + 3 / view.k);
    }
  }

  // ---------- мышь ----------

  function toWorld(event) {
    const box = svg.getBoundingClientRect();
    return {
      x: (event.clientX - box.left - size.width / 2 - view.x) / view.k,
      y: (event.clientY - box.top - size.height / 2 - view.y) / view.k,
    };
  }

  function nodeAt(event) {
    const point = toWorld(event);
    let best = null;
    let bestDistance = Infinity;
    for (const node of nodes) {
      const distance = Math.hypot(node.x - point.x, node.y - point.y);
      if (distance <= node.r + 4 / view.k && distance < bestDistance) {
        best = node;
        bestDistance = distance;
      }
    }
    return best;
  }

  svg.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    svg.setPointerCapture(event.pointerId);
    const node = nodeAt(event);
    gesture = { node, startX: event.clientX, startY: event.clientY, viewX: view.x, viewY: view.y, moved: false };
    svg.classList.toggle("panning", !node);
  });

  svg.addEventListener("pointermove", (event) => {
    if (!gesture) {
      const found = nodeAt(event);
      if (found !== hover) {
        hover = found;
        svg.classList.toggle("pointing", Boolean(found));
        mark();
      }
      return;
    }
    const dx = event.clientX - gesture.startX;
    const dy = event.clientY - gesture.startY;
    if (!gesture.moved && Math.hypot(dx, dy) < CLICK_PX) return;
    gesture.moved = true;
    if (gesture.node) {
      const point = toWorld(event);
      gesture.node.x = point.x;
      gesture.node.y = point.y;
      heat(0.3);
    } else {
      view.x = gesture.viewX + dx;
      view.y = gesture.viewY + dy;
      draw();
    }
  });

  const release = () => {
    const done = gesture;
    gesture = null;
    svg.classList.remove("panning");
    if (done?.node && !done.moved) onOpen(done.node.path);
  };
  svg.addEventListener("pointerup", release);
  svg.addEventListener("pointercancel", () => {
    gesture = null;
    svg.classList.remove("panning");
  });
  svg.addEventListener("pointerleave", () => {
    if (gesture || !hover) return;
    hover = null;
    svg.classList.remove("pointing");
    mark();
  });

  svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    // Масштаб вокруг курсора: точка под ним остаётся на месте.
    const box = svg.getBoundingClientRect();
    const mx = event.clientX - box.left - size.width / 2;
    const my = event.clientY - box.top - size.height / 2;
    const k = clamp(view.k * Math.exp(-event.deltaY * 0.0015), MIN_SCALE, MAX_SCALE);
    view = { k, x: mx - ((mx - view.x) * k) / view.k, y: my - ((my - view.y) * k) / view.k };
    draw();
  }, { passive: false });

  svg.addEventListener("dblclick", (event) => {
    if (nodeAt(event)) return;
    fit();
    draw();
  });

  new ResizeObserver(() => {
    const box = root.getBoundingClientRect();
    size = { width: box.width, height: box.height };
    if (!fitted && alpha < 0.3) fit();
    draw();
  }).observe(root);

  return { update, setCurrent, redraw: draw };
}

function shape(tag, className = "") {
  const node = document.createElementNS(SVG, tag);
  if (className) node.setAttribute("class", className);
  return node;
}

function shorten(text) {
  return text.length > 32 ? `${text.slice(0, 31)}…` : text;
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}
