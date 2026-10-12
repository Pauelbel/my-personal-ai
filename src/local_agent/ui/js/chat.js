// Чат: история сессии и печатающийся ответ. Вызовы инструментов — строки в общей рамке,
// под ответом — сводка хода и «Копировать», результаты саб-агентов выделены, код в ответах подсвечен.
import { renderDiagrams } from "./diagrams.js";
import { highlightCode } from "./highlight.js";
import { appendMarkdown } from "./markdown.js";

const FLUSH_MS = 80;
const STEP_ICON = { done: "✓", failed: "✕", pending: "◌" };
// Длинный отчёт саб-агента в основном чате свёрнут; развёрнутые остаются такими после перерисовки.
const REPORT_FOLD_CHARS = 700;
const REPORT_FOLD_LINES = 12;
const unfolded = new Set();
const folded = new Set();

export function renderMessages(container, messages) {
  container.replaceChildren();
  if (!messages.length) {
    const empty = el("div", "empty");
    const mascot = el("img");
    mascot.src = "/ui/assets/meepo-mascot.png";
    mascot.alt = "";
    empty.append(mascot, el("strong", "", "Привет! Я Meepo."), "Расскажите, как вас зовут и чем займёмся.");
    container.append(empty);
    return;
  }
  const results = new Map(messages.filter((item) => item.role === "tool").map((item) => [item.tool_call_id, item]));
  // Вызовы инструментов подряд, даже из разных раундов, собираются в одну рамку.
  let box = null;
  const add = (node) => {
    box = null;
    container.append(node);
  };
  for (const message of messages) {
    if (message.role === "tool") continue;
    if (message.role === "user") {
      add(message.sender ? reportNode(message) : userNode(message));
      continue;
    }
    if (message.content?.trim()) add(assistantNode(message));
    if (!message.tool_calls?.length) continue;
    if (!box) container.append((box = new ToolBox()).node);
    box.add(message, results);
  }
}

// Рамка вызовов инструментов: в заголовке на кромке — сколько вызовов, сколько заняли и за сколько запросов к модели.
// Свёрнутые рамки остаются свёрнутыми после перерисовки.
class ToolBox {
  constructor() {
    this.node = el("details", "tools");
    this.node.open = true;
    this.head = el("summary", "tools-head");
    this.title = el("span", "tools-title");
    this.meta = el("span", "tools-meta");
    this.head.append(this.title, this.meta);
    this.rows = el("div", "tools-rows");
    this.node.append(this.head, this.rows);
    this.key = null;
    this.calls = 0;
    this.requests = 0;
    this.widest = 0;
    this.first = null;
    this.last = null;
    this.node.addEventListener("toggle", () => {
      if (this.node.open) folded.delete(this.key);
      else folded.add(this.key);
    });
  }

  add(message, results) {
    // Шаги одного сообщения идут друг за другом: время шага — от конца предыдущего до его результата.
    let previous = Date.parse(message.created_at);
    this.first ??= previous;
    this.key ??= message.id;
    this.node.open = !folded.has(this.key);
    this.requests += 1;
    for (const call of message.tool_calls) {
      const result = results.get(call.id);
      const finished = result && Date.parse(result.created_at);
      const seconds = finished && previous ? Math.max(finished - previous, 0) / 1000 : null;
      if (finished) previous = this.last = finished;
      this.rows.append(stepNode(call, result, seconds));
      this.calls += 1;
      this.widest = Math.max(this.widest, call.name.length);
    }
    // Колонка имён — по самому длинному имени в рамке: шрифт моноширинный, ширина в символах точная.
    this.node.style.setProperty("--name-ch", String(this.widest));
    this.title.textContent = `◇ инструменты · ${this.calls} `;
    this.title.append(el("span", "tools-caret", "▾"));
    const span = this.first && this.last ? (this.last - this.first) / 1000 : null;
    this.meta.textContent = [span != null && duration(round(span)), plural(this.requests, "запрос", "запроса", "запросов")]
      .filter(Boolean).join(" · ");
  }
}

export class StreamingMessage {
  constructor(container) {
    this.container = container;
    this.node = el("article", "msg assistant streaming");
    this.textNode = el("div", "text");
    this.status = el("div", "status");
    this.trace = el("details", "trace");
    this.trace.append(el("summary", "", "Ход"));
    this.traceText = el("pre");
    this.trace.append(this.traceText);
    this.reasoning = null;
    this.text = "";
    this.timer = null;
    // Сколько секунд идёт ход: рядом со статусом, чтобы долгий ответ не выглядел зависшим.
    this.started = Date.now();
    this.statusText = "Модель думает";
    this.ticker = setInterval(() => this.#showStatus(), 1000);
    this.#showStatus();
    // Статус под текстом: при автопрокрутке он остаётся на виду, даже когда ответ длинный.
    this.node.append(this.textNode, this.status, this.trace);
    container.append(this.node);
    scrollDown(container);
  }

  append(text) {
    this.text += text;
    if (this.statusText !== "Пишет ответ") this.setStatus("Пишет ответ");
    this.timer ??= setTimeout(() => this.#flush(), FLUSH_MS);
  }

  addReasoning(text) {
    if (!this.reasoning) {
      this.reasoning = el("details", "trace");
      this.reasoning.append(el("summary", "", "Рассуждения"), el("pre"));
      this.node.insertBefore(this.reasoning, this.textNode);
      this.setStatus("Рассуждает");
    }
    this.reasoning.querySelector("pre").textContent += text;
  }

  addLog(text) {
    this.traceText.textContent += `${text}\n`;
  }

  // Модель позвала инструмент: напечатанное до вызова уже сохранено в истории.
  reset(status) {
    this.text = "";
    this.textNode.replaceChildren();
    this.reasoning?.remove();
    this.reasoning = null;
    this.setStatus(status);
  }

  setStatus(text) {
    this.statusText = text;
    this.#showStatus();
  }

  attach() {
    if (!this.node.isConnected) this.container.append(this.node);
  }

  finish() {
    clearTimeout(this.timer);
    clearInterval(this.ticker);
    this.node.remove();
  }

  #showStatus() {
    const seconds = Math.floor((Date.now() - this.started) / 1000);
    this.status.textContent = seconds ? `${this.statusText} · ${duration(seconds)}` : this.statusText;
  }

  #flush() {
    this.timer = null;
    const near = this.container.scrollHeight - this.container.scrollTop - this.container.clientHeight < 80;
    this.textNode.replaceChildren();
    renderMarkdown(this.textNode, this.text, false);
    if (near) scrollDown(this.container);
  }
}

// images — адрес картинки по ссылке из текста; по умолчанию показываются только картинки самого приложения.
export function renderMarkdown(target, text, withTools = true, images = (src) => (src.startsWith("/api/") ? src : null)) {
  appendMarkdown(target, text);
  for (const image of target.querySelectorAll("img[data-src]")) {
    const url = images(image.dataset.src);
    if (!url) {
      // Чужие адреса не грузим: картинка из интернета выдала бы, что страницу открыли.
      image.replaceWith(document.createTextNode(`[картинка: ${image.alt || image.dataset.src}]`));
      continue;
    }
    const link = el("a", "md-image");
    Object.assign(link, { href: url, target: "_blank", rel: "noopener" });
    Object.assign(image, { src: url, loading: "lazy" });
    image.replaceWith(link);
    link.append(image);
  }
  for (const code of target.querySelectorAll("pre > code")) {
    highlightCode(code, code.dataset.language);
    if (withTools) code.parentElement.append(copyButton("copy", () => code.textContent));
  }
  if (!withTools) return;
  // Диаграммы — только в готовом тексте: пока ответ печатается, блок ещё не дописан.
  renderDiagrams(target);
  // Пути в `коде` открываются во вкладке «Файлы»: щелчок ловит app.js по классу path-link.
  for (const code of target.querySelectorAll(":not(pre) > code")) markPath(code, code.textContent);
}

// Похоже ли на путь: с «/» — любое, без — имя файла с известным расширением. «:42» в конце — строка.
const FILE_EXTENSIONS = new Set(("md markdown txt py js mjs cjs ts tsx jsx json yaml yml toml ini cfg conf env xml html htm css scss "
  + "sh bash ps1 sql csv log go rs java kt c h cpp hpp cs rb php vue lock").split(" "));

function pathOf(text) {
  const value = text.trim();
  if (!value || value.length > 300 || /[\n<>{}()=,;|*?"'`]|:\/\//.test(value) || value.startsWith("-")) return null;
  const [, raw, line] = /^(.*?)(?::(\d+))?(?::\d+)?$/.exec(value);
  const path = raw.replace(/\/+$/, "");
  if (!path) return null;
  if (!path.includes("/")) {
    const extension = /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase();
    if (/\s/.test(path) || !FILE_EXTENSIONS.has(extension)) return null;
  }
  return { path, line: line ? Number(line) : null };
}

function markPath(node, text) {
  const found = pathOf(text);
  if (!found) return;
  node.classList.add("path-link");
  node.dataset.path = found.path;
  if (found.line) node.dataset.line = String(found.line);
  node.title = `Открыть во вкладке «Файлы»${found.line ? `, строка ${found.line}` : ""}`;
}

export function scrollDown(container) {
  container.scrollTop = container.scrollHeight;
}

// Сводка хода одной строкой: «12,4 с · вход 2,9k · выход 312 · 165 ток/с».
function statsText(stats) {
  return [
    duration(stats.seconds),
    stats.input_tokens != null && `вход ${compact(stats.input_tokens)}`,
    stats.output_tokens != null && `выход ${compact(stats.output_tokens)}`,
    stats.tokens_per_second && `${Math.round(stats.tokens_per_second)} ток/с`,
    stats.rounds > 1 && `запросов: ${stats.rounds}`,
  ].filter(Boolean).join(" · ");
}

function compact(value) {
  const short = (number, unit) => `${number.toFixed(1).replace(/\.0$/, "").replace(".", ",")}${unit}`;
  if (value < 1000) return String(value);
  if (value < 1e6) return value < 1e4 ? short(value / 1e3, "k") : `${Math.round(value / 1e3)}k`;
  return short(value / 1e6, "M");
}

function duration(seconds) {
  if (seconds < 60) return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1).replace(".", ",")} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${Math.round(seconds - minutes * 60)} с`;
}

function round(seconds) {
  return seconds < 10 ? Math.max(Math.round(seconds * 10) / 10, 0.1) : Math.round(seconds);
}

function plural(count, one, few, many) {
  const tens = count % 100;
  const units = count % 10;
  const word = tens >= 11 && tens <= 14 ? many : units === 1 ? one : units >= 2 && units <= 4 ? few : many;
  return `${count} ${word}`;
}

function userNode(message) {
  const node = el("article", "msg user");
  const bubble = el("div", "bubble", message.content);
  if (message.attachments?.length) bubble.prepend(imagesNode(message.attachments));
  node.append(bubble);
  return node;
}

// Картинки реплики: миниатюры, щелчок открывает картинку целиком.
function imagesNode(names) {
  const box = el("div", "msg-images");
  for (const name of names) {
    const link = el("a");
    Object.assign(link, { href: `/api/attachments/${name}`, target: "_blank", rel: "noopener" });
    const image = el("img");
    Object.assign(image, { src: link.href, alt: name, loading: "lazy" });
    link.append(image);
    box.append(link);
  }
  return box;
}

function assistantNode(message) {
  const node = el("article", "msg assistant");
  const body = el("div", "text");
  renderMarkdown(body, message.content);
  node.append(body, footNode(message));
  return node;
}

function reportNode(message) {
  const node = el("article", "msg report");
  const body = el("div", "text");
  renderMarkdown(body, message.content);
  const extra = [];
  if (message.content.length > REPORT_FOLD_CHARS || message.content.split("\n").length > REPORT_FOLD_LINES) {
    const fold = el("button", "msg-action fold");
    fold.type = "button";
    const sync = () => {
      const open = unfolded.has(message.id);
      node.classList.toggle("folded", !open);
      fold.textContent = open ? "Свернуть" : "Показать полностью";
    };
    fold.addEventListener("click", () => {
      if (!unfolded.delete(message.id)) unfolded.add(message.id);
      sync();
    });
    sync();
    extra.push(fold);
  }
  node.append(el("div", "from", `от ${message.sender}`), body, footNode(message, extra));
  return node;
}

// Подвал ответа: сводка хода, если она сохранена, и копирование всего ответа в Markdown.
function footNode(message, extra = []) {
  const foot = el("div", `msg-foot${message.stats || extra.length ? "" : " bare"}`);
  foot.append(...extra);
  if (message.stats) {
    const stats = el("span", "stats", statsText(message.stats));
    stats.title = "Время работы модели и инструментов без ожидания ваших решений. "
      + "Вход — размер последнего запроса к модели, выход — всё, что она написала за ход.";
    foot.append(stats);
  }
  foot.append(copyButton("msg-action push", () => message.content));
  return foot;
}

function stepNode(call, result, seconds) {
  const state = !result ? "pending" : result.is_error ? "failed" : "done";
  const node = el("details", `step ${state}`);
  const args = parse(call.arguments);
  // Браузер: element — что за элемент, text — что ввели, target — ссылка на элемент из снимка.
  const target = args.path || args.query || args.url || args.name || args.agent || (Array.isArray(args.names) ? args.names.join(", ") : "")
    || [args.element, args.text && `«${args.text}»`].filter(Boolean).join(" ← ") || args.target || "";
  const summary = el("summary");
  const icon = el("span", "step-icon", STEP_ICON[state]);
  icon.title = { done: "Готово", failed: "Ошибка", pending: "Выполняется" }[state];
  const label = el("span", "step-target", target ? String(target) : "—");
  if (args.path) markPath(label, String(args.path));
  const meta = state === "failed" ? "ошибка" : state === "pending" ? "…" : seconds != null ? duration(round(seconds)) : "";
  summary.append(icon, el("span", "step-name", call.name), label, el("span", "step-meta", meta));
  const body = el("pre");
  body.textContent = `${JSON.stringify(args, null, 2)}\n\n→ ${result?.content ?? "—"}`;
  node.append(summary, body);
  return node;
}

function copyButton(className, text) {
  const button = el("button", className, "Копировать");
  button.type = "button";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text());
      button.textContent = "Скопировано";
    } catch {
      button.textContent = "Не удалось";
    }
    setTimeout(() => { button.textContent = "Копировать"; }, 1500);
  });
  return button;
}

export function parse(raw) {
  try {
    const value = JSON.parse(raw || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

export function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}
