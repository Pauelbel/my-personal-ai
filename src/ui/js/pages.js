// Страницы вне чата: вики, возможности агента, долговременная память.
// Списки — деревья в стиле терминала (tree.js), возможности разложены по вкладкам.
import { api } from "./api.js";
import { el, renderMarkdown } from "./chat.js";
import { createGraph } from "./graph.js";
import { stepList } from "./steps.js";
import { guide, rowAction, stem, treeRow } from "./tree.js";
import { recall, remember } from "./util.js";

const $ = (selector) => document.querySelector(selector);
// Три группы инструментов; третья — серверы MCP — рисуется отдельно, с переключателями.
const ORIGIN = { builtin: "Встроенные", connector: "Коннекторы — скрипты: рабочие системы и свои инструменты" };
const KIND = { tool: "инструмент", agent: "агент" };

// Строки детей одной группы: ветки «├─», у последней — «└─».
function branch(items, lines, build) {
  return items.flatMap((item, index) => build(item, lines, index === items.length - 1));
}

// ---------- вики ----------

const INDEX_PAGE = "index.md";
// Сколько заметок в «Недавних» и сколько тегов видно над списком, пока не нажали «ещё».
const RECENT_SHOWN = 5;
const TAGS_SHOWN = 10;
const UNTAGGED = "без тегов";
// Открытая заметка запоминается: после перезагрузки страницы она открывается снова.
let wikiCurrent = recall("wiki-page");
const wikiClosed = new Set();
// Все заметки с описаниями и тегами: по ним список фильтруется сразу, без запроса к серверу.
let wikiPages = [];
// Выбранный тег сужает список до его заметок; null — все заметки.
let wikiTag = null;
let wikiAllTags = false;
// Последний показанный список: после открытия заметки он перерисовывается без нового запроса
// и без перестановки «Недавних» под курсором.
let wikiShown = { query: "", textHits: [], recent: [] };
// «Список» — заметки по темам, «Граф» — точки и ссылки между ними; читалка справа в обоих видах.
let wikiMode = recall("wiki-mode") === "graph" ? "graph" : "list";
let wikiGraph = null;

export async function showWiki(toast) {
  try {
    const query = $("#wiki-search").value.trim();
    const graphShown = wikiMode === "graph" && !query && !wikiTag;
    renderWikiMode(toast, graphShown);
    wikiPages = await api.wiki();
    renderWikiTags(toast);
    if (graphShown) {
      wikiGraph ??= createGraph($("#wiki-graph"), { onOpen: (path) => openWiki(path, toast) });
      wikiGraph.update(await api.wikiGraph(), wikiCurrent);
    } else {
      const textHits = query ? await api.wikiSearch(query) : [];
      renderWikiList({ query, textHits, recent: recentPages() }, toast);
    }
    if (wikiCurrent && !query) await openWiki(wikiCurrent, toast, false);
  } catch (error) {
    toast(error);
  }
}

// Стрелки ходят по списку, Enter в поиске открывает первую найденную заметку, Esc очищает поиск.
export function wikiKeys(event, toast) {
  const search = $("#wiki-search");
  const rows = [...$("#wiki-list").querySelectorAll("button.trow-main")];
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const next = rows.indexOf(document.activeElement) + (event.key === "ArrowDown" ? 1 : -1);
    (next < 0 ? search : rows[Math.min(next, rows.length - 1)])?.focus();
  } else if (event.key === "Enter" && event.target === search) {
    event.preventDefault();
    rows.find((row) => !row.closest(".group"))?.click();
  } else if (event.key === "Escape" && (search.value || wikiTag)) {
    // Esc здесь только очищает поиск: ответ в основном чате он не останавливает.
    event.stopPropagation();
    search.value = "";
    wikiTag = null;
    search.focus();
    void showWiki(toast);
  }
}

// Поиск и выбранный тег всегда показывают список: граф по ним не фильтруется.
function renderWikiMode(toast, graphShown) {
  $("#wiki-split").classList.toggle("graph-mode", graphShown);
  $("#wiki-list").hidden = graphShown;
  $("#wiki-graph").hidden = !graphShown;
  for (const tab of $("#wiki-tabs").querySelectorAll("button")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.mode === wikiMode));
    tab.onclick = () => {
      wikiMode = tab.dataset.mode;
      remember("wiki-mode", wikiMode);
      $("#wiki-search").value = "";
      wikiTag = null;
      void showWiki(toast);
    };
  }
}

// Теги над списком — самые частые первыми, с числом заметок.
function renderWikiTags(toast) {
  const tags = tagCounts();
  const shown = wikiAllTags ? tags : tags.slice(0, TAGS_SHOWN);
  if (wikiTag && !shown.some(([tag]) => tag === wikiTag)) shown.push([wikiTag, 0]);
  const chips = shown.map(([tag, count]) => tagChip(tag, toast, count));
  if (tags.length > TAGS_SHOWN) {
    const more = el("button", "chip", wikiAllTags ? "свернуть" : `ещё ${tags.length - TAGS_SHOWN}`);
    more.type = "button";
    more.addEventListener("click", () => {
      wikiAllTags = !wikiAllTags;
      renderWikiTags(toast);
    });
    chips.push(more);
  }
  $("#wiki-tags").replaceChildren(...chips);
}

function tagCounts() {
  const counts = new Map();
  for (const page of wikiPages) for (const tag of page.tags) counts.set(tag, (counts.get(tag) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ru"));
}

// Тег над списком и под заметкой: щелчок оставляет в списке только его заметки, повторный — снимает фильтр.
function tagChip(tag, toast, count = 0) {
  const chip = el("button", `chip${tag === wikiTag ? " on" : ""}`, `#${tag}`);
  chip.type = "button";
  chip.title = tag === wikiTag ? "Показать все заметки" : `Только заметки с тегом «${tag}»`;
  if (count) chip.append(el("span", "chip-count", ` ${count}`));
  chip.addEventListener("click", () => {
    wikiTag = tag === wikiTag ? null : tag;
    void showWiki(toast);
  });
  return chip;
}

// Недавние: заметки по последнему касанию — открыли здесь или изменили (вы или агент), что позже.
// Только что созданная заметка поэтому сразу наверху, а не за открытыми раньше.
function recentPages() {
  const opened = openedAt();
  const touched = (page) => Math.max(opened[page.path] ?? 0, Date.parse(page.updated_at) || 0);
  return [...wikiPages].sort((a, b) => touched(b) - touched(a)).slice(0, RECENT_SHOWN);
}

// Когда заметки открывали здесь: { путь: время }. Старая запись — просто список путей, без времени.
function openedAt() {
  try {
    const stored = JSON.parse(recall("wiki-opened"));
    if (stored && typeof stored === "object" && !Array.isArray(stored)) return stored;
  } catch {
    // Испорченная запись — просто без открытых.
  }
  return {};
}

// Без поиска — «Недавние» и заметки по темам (главный тег — самый частый в вики из тегов заметки);
// с поиском — совпадения в названии, описании и тегах (сразу, без сервера), ниже — найденное в тексте.
function renderWikiList(shown, toast) {
  wikiShown = shown;
  const { query, textHits, recent } = shown;
  const root = $("#wiki-list");
  if (!wikiPages.length) {
    root.replaceChildren(el("p", "hint", "Заметок пока нет."));
    return;
  }
  const pages = wikiTag ? wikiPages.filter((page) => page.tags.includes(wikiTag)) : wikiPages;
  // Длинное название в узкой колонке обрезано: целиком — с описанием — во всплывающей подсказке.
  const item = (page, detail = "") => ({ path: page.path, title: page.title, hint: [page.title, page.description].join("\n"), detail });
  let sections;
  if (query) {
    const words = query.toLowerCase().split(/\s+/);
    const named = pages.filter((page) => {
      const text = [page.title, page.description, ...page.tags].join(" ").toLowerCase();
      return words.every((word) => text.includes(word));
    });
    const skip = new Set(named.map((page) => page.path));
    const allowed = new Set(pages.map((page) => page.path));
    const inText = [...new Map(textHits.map((hit) => [hit.path, hit])).values()]
      .filter((hit) => allowed.has(hit.path) && !skip.has(hit.path));
    sections = [
      ["В названии и тегах", named.map((page) => item(page, page.description))],
      ["В тексте", inText.map((hit) => ({ path: hit.path, title: hit.title, hint: hit.path, detail: hit.text }))],
    ];
  } else if (wikiTag) {
    sections = [[`#${wikiTag}`, byTitle(pages).map((page) => item(page))]];
  } else {
    const counts = new Map(tagCounts());
    const topics = new Map();
    for (const page of pages) {
      const topic = page.tags.reduce((best, tag) => (counts.get(tag) > (counts.get(best) ?? 0) ? tag : best), null);
      const name = topic ? `#${topic}` : UNTAGGED;
      topics.set(name, [...(topics.get(name) ?? []), page]);
    }
    const ordered = [...topics].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0], "ru"));
    sections = [
      ["Недавние", recent.map((page) => item(page))],
      ...ordered.map(([name, group]) => [name, byTitle(group).map((page) => item(page))]),
    ];
  }
  const rows = sections.filter(([, items]) => items.length).flatMap(([name, items]) => {
    // Найденное не сворачивается: свёрнутая группа спрятала бы результат поиска.
    const opened = Boolean(query) || !wikiClosed.has(name);
    const head = treeRow({
      label: `${name} · ${items.length}`, group: true, open: opened,
      onClick: query ? undefined : () => {
        if (!wikiClosed.delete(name)) wikiClosed.add(name);
        renderWikiList(wikiShown, toast);
      },
    });
    return opened ? [head, ...branch(items, [], (entry, lines, last) => treeRow({
      prefix: guide(lines, last), label: entry.title, title: entry.hint, current: entry.path === wikiCurrent,
      onClick: () => openWiki(entry.path, toast), detail: entry.detail, detailPrefix: stem(lines, last),
    }))] : [head];
  });
  if (!query && !wikiTag) {
    rows.push(treeRow({
      label: INDEX_PAGE, note: "индекс", current: wikiCurrent === INDEX_PAGE, onClick: () => openWiki(INDEX_PAGE, toast),
      title: "Индекс: строка на каждую заметку, агент обновляет его сам",
    }));
  }
  root.replaceChildren(...(rows.length ? rows : [el("p", "hint", "Ничего не нашлось.")]));
}

function byTitle(pages) {
  return [...pages].sort((a, b) => a.title.localeCompare(b.title, "ru"));
}

// Заметка по пути — из чата вики: пути в ответах агента открывают её здесь.
export function openNote(path, toast) {
  void openWiki(path, toast);
}

async function openWiki(path, toast, refreshList = true) {
  try {
    const page = await api.wikiPage(path);
    wikiCurrent = path;
    remember("wiki-page", path);
    if (path !== INDEX_PAGE) {
      const opened = Object.entries({ ...openedAt(), [path]: Date.now() }).sort((a, b) => b[1] - a[1]);
      remember("wiki-opened", JSON.stringify(Object.fromEntries(opened.slice(0, RECENT_SHOWN))));
    }
    const reader = $("#wiki-page");
    reader.replaceChildren(el("p", "hint", `data/wiki/${path}`));
    // Теги — как в списке: варианты одного слова там уже сведены к одному тегу.
    const tags = wikiPages.find((item) => item.path === path)?.tags ?? page.meta?.tags.map((tag) => tag.toLowerCase().trim());
    if (page.meta) reader.append(noteMeta(page.meta, tags, toast));
    // Картинки заметки лежат в вики рядом с ней: адрес — от папки заметки.
    renderMarkdown(reader, page.body, true, (src) => {
      const file = wikiPath(path, src);
      return file ? `/api/wiki/file?path=${encodeURIComponent(file)}` : null;
    });
    // Ссылки на другие заметки открываются здесь же, а не переходом браузера.
    for (const link of reader.querySelectorAll("a[data-href]")) {
      const target = wikiTarget(path, link.dataset.href);
      if (!target) continue;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        void openWiki(target, toast);
      });
    }
    wikiGraph?.setCurrent(path);
    if (refreshList && !$("#wiki-list").hidden) renderWikiList(wikiShown, toast);
  } catch (error) {
    if (refreshList) {
      toast(error);
      return;
    }
    // Запомненной заметки больше нет (удалили или слили): забываем её без ошибки.
    wikiCurrent = null;
    remember("wiki-page", "");
    $("#wiki-page").replaceChildren(el("p", "hint", "Выберите страницу."));
  }
}

// Поля OKF над текстом: тип, теги (щелчок — заметки с этим тегом) и описание.
function noteMeta(meta, tags, toast) {
  const box = el("div", "note-meta");
  const chips = el("div", "note-chips");
  if (meta.type) chips.append(el("span", "chip type", meta.type));
  for (const tag of tags) chips.append(tagChip(tag, toast));
  if (chips.childElementCount) box.append(chips);
  if (meta.description) box.append(el("p", "note-description", meta.description));
  return box;
}

// Путь заметки по относительной ссылке из заметки from; внешние ссылки и не .md — null.
function wikiTarget(from, href) {
  const path = wikiPath(from, href);
  return path?.endsWith(".md") ? path : null;
}

// Путь файла вики по относительной ссылке из заметки from; внешние ссылки — null.
function wikiPath(from, href) {
  if (!href || /^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("/") || href.startsWith("#")) return null;
  const clean = decodeURIComponent(href.split("#")[0]);
  const parts = from.split("/").slice(0, -1);
  for (const part of clean.split("/")) {
    if (part === "..") parts.pop();
    else if (part && part !== ".") parts.push(part);
  }
  return parts.join("/");
}

// ---------- возможности ----------

// Два вида возможностей — по вкладке на вид. В каждой: что это и чем отличается от соседнего, ваши, заготовки Мипо
// (идут вместе с приложением, включаются кнопкой) и архив этого вида.
const CAP_KINDS = {
  agents: {
    kind: "agent", icon: "◆", label: "Агенты", short: "кто выполняет работу", folder: "data/agents", add: "＋ Новый агент",
    about: "Специализированный агент выполняет одну законченную QA-задачу в отдельном контексте. В AGENT.md лежат "
      + "его роль, разрешённые инструменты и делегирование; steps.md и scripts/ задают воспроизводимый процесс.",
    empty: "Агентов пока нет. Создайте агента или включите заготовку.",
  },
  tools: {
    kind: "tool", icon: "⚙\uFE0E", label: "Инструменты", short: "что достать или сделать", folder: "data/tools",
    about: "Что Мипо умеет достать или сделать одним вызовом: читать и писать файлы, открывать сайты, ходить в Jira. "
      + "Коннектор — папка из двух файлов: tool.md (описание, параметры, подсказка модели, доступы) и script.py. "
      + "Встроенные идут с Мипо, серверы MCP — внешние программы, их можно выключить. Всё, что меняет файлы или "
      + "систему, — с вашим подтверждением.",
    empty: "",
  },
};
let capTab = recall("capabilities-tab");
capTab = CAP_KINDS[capTab] ? capTab : "agents";
let capData = null;
let capPoll = null;

export async function showCapabilities(toast) {
  try {
    capData = await api.capabilities();
    renderCapabilities(toast);
  } catch (error) {
    toast(error);
    return;
  }
  // Сервер MCP запускается — состояние перечитывается раз в секунду, пока он не заработает или не упадёт.
  clearTimeout(capPoll);
  if (capData.mcp.servers.some((server) => server.status === "запускается")) {
    capPoll = setTimeout(() => {
      if (!$("#view-capabilities").hidden) void showCapabilities(toast);
    }, 1000);
  }
}

function renderCapabilities(toast) {
  const data = capData;
  const reload = () => showCapabilities(toast);
  const counts = { agents: data.agents.length, tools: data.tools.filter((tool) => tool.origin !== "mcp").length };
  $("#cap-tabs").replaceChildren(...Object.entries(CAP_KINDS).map(([id, info]) => {
    const tab = el("button", "kind-tab");
    tab.type = "button";
    tab.setAttribute("aria-pressed", String(id === capTab));
    const head = el("span", "kind-tab-head");
    head.append(el("span", `kind-tab-icon kind-${info.kind}`, info.icon), el("span", "kind-tab-label", info.label),
      el("span", "count", String(counts[id])));
    tab.append(head, el("span", "kind-tab-short", info.short));
    tab.addEventListener("click", () => {
      capTab = id;
      remember("capabilities-tab", id);
      renderCapabilities(toast);
    });
    return tab;
  }));
  const info = CAP_KINDS[capTab];
  const own = {
    agents: () => grid(data.agents.map((agent) => workflowCard(agent, reload, toast, { kind: "agent" })), info.empty),
    tools: () => toolRows(data, reload, toast),
  }[capTab];
  $("#capabilities").replaceChildren(
    intro(info, data, reload, toast),
    ...own(),
    ...librarySection(info, data, reload, toast),
    ...archiveSection(data, reload, toast),
  );
}

// Шапка вида: что это — двумя-тремя фразами, где лежат файлы, и «＋ Новый».
function intro(info, data, reload, toast) {
  const box = el("div", "cap-intro");
  const text = el("div", "cap-intro-text");
  text.append(el("p", "", info.about));
  const folder = el("p", "cap-intro-folder");
  folder.append("Файлы — ", el("code", "", info.folder), ": их можно править и руками, затем «↻ Перечитать с диска».");
  text.append(folder);
  box.append(el("span", `cap-intro-icon kind-${info.kind}`, info.icon), text);
  if (info.add) {
    const add = el("button", "primary", info.add);
    add.type = "button";
    add.addEventListener("click", () => openNew(info.kind, data.templates?.[info.kind] ?? "", reload, toast));
    box.append(add);
  }
  return box;
}

function grid(cards, empty) {
  if (!cards.length) return [el("p", "hint cap-empty", empty)];
  const box = el("div", "cap-grid");
  box.append(...cards);
  return [box];
}

function sectionHead(title, note = "") {
  const head = el("div", "cap-section");
  head.append(el("h3", "", title));
  if (note) head.append(el("p", "hint", note));
  return head;
}

// Карточка возможности: заголовок с id, описание, метки и действия внизу.
function capCard({ title, id = "", description = "", className = "", body = [], actions = [] }) {
  const card = el("article", `cap-card ${className}`.trim());
  const head = el("header", "cap-card-head");
  head.append(el("h4", "", title));
  if (id && id !== title) head.append(el("code", "cap-card-id", id));
  card.append(head);
  if (description) card.append(el("p", "cap-card-about", description));
  card.append(...body.filter(Boolean));
  const row = el("div", "cap-card-actions");
  row.append(...actions.filter(Boolean));
  if (row.childElementCount) card.append(row);
  return card;
}

function action(label, handler, className = "") {
  const button = el("button", `small ${className}`.trim(), label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

// Карточка агента: описание и файлы папки, а если есть шаги — что они делают, что им нужно и сами шаги.
function workflowCard(workflow, reload, toast, { ready = false, kind = "agent" } = {}) {
  const plan = workflow.steps;
  const body = [filesNote(workflow.files ?? [])];
  if (plan) {
    if (plan.about) body.push(el("p", "cap-card-note", plan.about));
    if (plan.inputs.length) {
      const needs = el("div", "cap-needs");
      needs.append(el("span", "cap-needs-label", "нужно"));
      for (const input of plan.inputs) {
        const chip = el("span", `cap-need${input.optional ? " optional" : ""}`, input.name);
        chip.title = input.hint + (input.optional ? " (необязательно)" : "");
        needs.append(chip);
      }
      body.push(needs);
    }
    body.push(stepList(plan));
    if (workflow.problems?.length) {
      const list = el("ul", "steps-problems");
      for (const problem of workflow.problems) list.append(el("li", "", problem));
      body.push(list);
    }
  }
  if (workflow.uses?.length) body.push(usesNote(workflow.uses, toast));
  const actions = ready
    ? [action("＋ Включить", () => enable(kind, workflow.id, reload, toast), "primary")]
    : [
      action("✎ Править", () => showSource(kind, workflow.id, reload, toast)),
      action("В архив", () => archive(kind, workflow.id, reload, toast), "danger"),
    ];
  return capCard({
    title: workflow.name ?? workflow.id, id: workflow.id, description: workflow.description, body, actions,
    className: `${plan ? "steps-card" : ""}${ready ? " ready" : ""}`.trim(),
  });
}

// Какие инструменты зовут шаги агента и готовы ли они; по клику — карточка инструмента на соседней вкладке.
const USE_STATUS = {
  ready: ["✓", "готов"], configure: ["⚠", "не заданы доступы — откройте «настроить» на карточке"],
  missing: ["✗", "инструмента нет: включите его во вкладке «Инструменты»"],
};

function usesNote(uses, toast) {
  const box = el("div", "cap-needs");
  box.append(el("span", "cap-needs-label", "использует"));
  for (const use of uses) {
    const [mark, title] = USE_STATUS[use.status] ?? ["", ""];
    const chip = el("button", `cap-need cap-use ${use.status}`, `${use.id} ${mark}`);
    chip.type = "button";
    chip.title = title;
    chip.addEventListener("click", () => showTool(use.id, toast));
    box.append(chip);
  }
  return box;
}

function showTool(id, toast) {
  capTab = "tools";
  remember("capabilities-tab", capTab);
  renderCapabilities(toast);
  const card = document.getElementById(`tool-card-${id}`);
  if (!card) return;
  card.scrollIntoView({ block: "center" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1500);
}

// Карточка коннектора: всё про инструмент в одном месте — доступ, кто его зовёт, подсказка модели, код и проба.
function connectorCard(tool, reload, toast) {
  const body = [];
  if (tool.fields?.length) {
    const access = el("div", "cap-needs");
    access.append(el("span", "cap-needs-label", "доступ"));
    for (const field of tool.fields) {
      const chip = el("span", `cap-need${field.optional ? " optional" : ""}`,
        `${field.name} ${field.set ? "✓" : field.optional ? "—" : "⚠"}${field.value && field.name.endsWith("_URL") ? ` ${field.value}` : ""}`);
      chip.title = field.set ? "задан" : field.optional ? "необязательный, не задан" : "не задан";
      access.append(chip);
    }
    body.push(access);
  }
  if (tool.used_by?.length) {
    const users = el("div", "cap-needs");
    users.append(el("span", "cap-needs-label", "используют"), ...tool.used_by.map((name) => el("span", "cap-need optional", name)));
    body.push(users);
  }
  body.push(connectorTabs(tool, toast));
  const actions = [
    tool.missing?.length && installLink(tool, reload, toast),
    tool.fields?.length && action("настроить", () => openConnector(tool, reload, toast), tool.configured ? "" : "primary"),
    action("✎ Править", () => showSource("tool", tool.id, reload, toast)),
    action("В архив", () => archive("tool", tool.id, reload, toast), "danger"),
  ];
  const card = capCard({ title: tool.name ?? tool.id, id: tool.id, description: tool.description, body, actions });
  card.id = `tool-card-${tool.id}`;
  return card;
}

// Вкладки карточки: «Подсказка» — что модель получает вместе со схемой, «Код» — script.py, «Попробовать» — вызов
// с вашими значениями и ответ ровно в том виде, в каком его увидит модель.
function connectorTabs(tool, toast) {
  const box = el("div", "cap-tabs");
  const bar = el("div", "cap-tab-bar");
  const panel = el("div", "cap-tab-panel");
  panel.hidden = true;
  const tabs = {
    hint: ["Подсказка", () => hintPanel(tool)],
    code: ["Код", () => codePanel(tool, toast)],
    ...(tool.tryable ? { try: ["Попробовать", () => tryPanel(tool)] } : {}),
  };
  let current = null;
  for (const [key, [label, build]] of Object.entries(tabs)) {
    const button = el("button", "small", label);
    button.type = "button";
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      const open = current !== key;
      current = open ? key : null;
      for (const other of bar.children) other.setAttribute("aria-pressed", String(other === button && open));
      panel.hidden = !open;
      panel.replaceChildren(...(open ? [build()] : []));
    });
    bar.append(button);
  }
  box.append(bar, panel);
  return box;
}

function hintPanel(tool) {
  if (!tool.hint) return el("p", "hint", "Подсказки нет: модель видит только описание. Добавьте раздел «## Подсказка модели» в tool.md.");
  const text = el("div", "text cap-hint");
  renderMarkdown(text, tool.hint);
  return text;
}

function codePanel(tool, toast) {
  const pre = el("pre", "cap-code", "Загружаю…");
  api.capabilitySource("tool", tool.id)
    .then((source) => { pre.textContent = source.files["script.py"] ?? "script.py нет"; })
    .catch((error) => { pre.textContent = ""; toast(error); });
  return pre;
}

function tryPanel(tool) {
  const form = el("form", "cap-try");
  const inputs = {};
  for (const parameter of tool.parameters ?? []) {
    const label = el("label", "cap-try-field");
    const input = el("input");
    input.name = parameter.name;
    input.type = parameter.number ? "number" : "text";
    input.placeholder = parameter.hint + (parameter.optional ? " (необязательно)" : "");
    input.required = !parameter.optional;
    inputs[parameter.name] = input;
    label.append(el("span", "", parameter.name), input);
    form.append(label);
  }
  const run = el("button", "primary small", "▶ Выполнить");
  run.type = "submit";
  const status = el("p", "hint", "");
  const output = el("pre", "cap-code");
  output.hidden = true;
  form.append(run, status, output);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    run.disabled = true;
    status.textContent = "Выполняю…";
    output.hidden = true;
    try {
      const values = Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name, input.value]));
      const result = await api.tryConnector(tool.id, values);
      status.textContent = `${result.is_error ? "✗ ошибка" : "✓ готово"} · ${result.seconds} с`;
      output.textContent = result.content;
      output.classList.toggle("error", result.is_error);
      output.hidden = false;
    } catch (error) {
      status.textContent = `✗ ${error.message}`;
    } finally {
      run.disabled = false;
    }
  });
  return form;
}

// Из чего состоит папка агента: WORKFLOW.md, steps.md, scripts/… — одной строкой.
function filesNote(files) {
  const box = el("div", "cap-needs");
  box.append(el("span", "cap-needs-label", "файлы"));
  for (const file of files) box.append(el("span", "cap-need optional", file));
  return box;
}

// Инструменты — списком: их много, и у каждого одна строка. Встроенные и коннекторы — группами, серверы MCP —
// с переключателями, пакеты Python — разделом здесь же.
function toolRows(data, reload, toast) {
  const connectors = data.tools.filter((tool) => tool.origin === "connector");
  const rows = groups([[ORIGIN.builtin, data.tools.filter((tool) => tool.origin === "builtin")]],
    (tool, lines, last) => capRow(tool.id, tool.description, [
      tool.missing?.length ? "нужны пакеты"
        : tool.fields?.length ? (tool.configured ? "настроен" : "нужно настроить")
          : tool.requires_approval ? "с подтверждением" : "без подтверждения",
    ], [
      tool.missing?.length && installLink(tool, reload, toast),
      tool.fields?.length && link("настроить", () => openConnector(tool, reload, toast)),
      tool.origin === "connector" && link("файлы", () => showSource("tool", tool.id, reload, toast)),
      tool.origin === "connector" && link("в архив", () => archive("tool", tool.id, reload, toast)),
    ], lines, last));
  const list = el("div", "tree-list");
  list.append(...rows);
  const mcp = el("div", "tree-list");
  mcp.append(...mcpRows(data, reload, toast));
  const python = el("div", "tree-list");
  python.append(...groups([[data.python.path ?? "песочница", data.python.packages]], (item, lines, last) => capRow(
    `${item.name} ${item.version}`, "", [item.tools.length ? `нужен: ${item.tools.join(", ")}` : "не используется"], [
      link("удалить", () => uninstall(item, reload, toast)),
    ], lines, last),
  "Пакетов нет: скрипты видят только стандартную библиотеку Python. Агент попросит установить нужное — с вашего согласия."));
  return [
    sectionHead("Коннекторы", "Скрипты на Python: подключение к рабочим системам и инструменты, которые агент написал сам."),
    ...grid(connectors.map((tool) => connectorCard(tool, reload, toast)),
      "Коннекторов нет. Включите заготовку ниже или попросите агента: «сделай инструмент для …»."),
    sectionHead("Встроенные"),
    list,
    sectionHead("Серверы MCP", "Браузер и поиск в интернете — внешние программы. Выключенный сервер не запускается, "
      + "а выключенного инструмента нет ни у агента, ни у шагов процессов агентов. Переключатели сохраняются в data/mcp.json."),
    mcp,
    sectionHead("Пакеты Python", "Окружение песочницы: в нём работают скрипты коннекторов и агентов."),
    python,
  ];
}

// Заготовки этого вида, которые ещё не включены. Когда всё включено, раздела нет совсем.
function librarySection(info, data, reload, toast) {
  const items = data.library.filter((item) => item.kind === info.kind);
  if (!items.length) return [];
  const note = "Идут вместе с Мипо. «Включить» кладёт копию в " + info.folder + " — дальше это ваша возможность: её можно править и убрать в архив.";
  if (info.kind === "agent") {
    return [sectionHead("Заготовки Мипо", note), ...grid(items.map((item) => workflowCard(item, reload, toast, { ready: true, kind: info.kind })), "")];
  }
  const list = el("div", "tree-list");
  list.append(...branch(items, [], (item, lines, last) => capRow(item.id, item.description, [], [
    link("включить", () => enable(item.kind, item.id, reload, toast)),
  ], lines, last)));
  return [sectionHead("Заготовки Мипо", note), list];
}

// Архив — общий для агентов и инструментов; вернуть или удалить навсегда.
function archiveSection(data, reload, toast) {
  const items = data.archive;
  if (!items.length) return [];
  const box = el("details", "cap-archive");
  box.append(el("summary", "", `Архив · ${items.length}`));
  const list = el("div", "tree-list");
  list.append(...branch(items, [], (item, lines, last) => capRow(item.id, item.name === item.id ? "" : item.name, [KIND[item.kind]], [
    link("вернуть", async () => {
      try {
        await api.restore(item.kind, item.archive_id);
        await reload();
      } catch (error) {
        toast(error);
      }
    }),
    link("удалить", async () => {
      if (!window.confirm(`Удалить ${KIND[item.kind]} ${item.id} из архива навсегда? Вернуть его будет нельзя.`)) return;
      try {
        await api.purge(item.kind, item.archive_id);
        await reload();
      } catch (error) {
        toast(error);
      }
    }),
  ], lines, last)));
  box.append(list);
  return [box];
}

async function enable(kind, id, reload, toast) {
  try {
    await api.enable(kind, id);
    await reload();
  } catch (error) {
    toast(error);
  }
}

// Непустые группы: заголовок «◆ группа», под ним ветки.
function groups(list, build, empty = "Пусто.") {
  const filled = list.filter(([, items]) => items.length);
  if (!filled.length) return [el("p", "hint", empty)];
  return filled.flatMap(([title, items]) => [
    treeRow({ label: title, group: true, note: String(items.length) }),
    ...branch(items, [], build),
  ]);
}

// Серверы MCP: переключатель у сервера и у каждого инструмента работающего сервера.
function mcpRows(data, reload, toast) {
  if (!data.mcp.started) return [el("p", "hint", "MCP выключен в .env (MCP_ENABLED=false): серверы не запускаются.")];
  if (!data.mcp.servers.length) return [el("p", "hint", "Серверов нет в data/mcp.json.")];
  return data.mcp.servers.flatMap((server) => {
    const head = treeRow({
      label: server.name, group: true, note: server.status, title: server.description,
      extra: [toggle(server.enabled, `Сервер ${server.name}`, (on) => api.switchServer(server.name, on), reload, toast)],
    });
    head.classList.toggle("failed", server.enabled && !["работает", "запускается"].includes(server.status));
    head.classList.toggle("off", !server.enabled);
    if (!server.tools.length) {
      return [head, treeRow({ prefix: guide([], true), label: server.enabled ? server.description : "выключен: его инструментов нет у агента" })];
    }
    return [head, ...branch(server.tools, [], (tool, lines, last) => {
      const row = capRow(tool.id, tool.description, [], [
        toggle(tool.enabled, tool.id, (on) => api.switchTool(server.name, tool.id, on), reload, toast),
      ], lines, last);
      (row.matches(".trow") ? row : row.querySelector(".trow")).classList.toggle("off", !tool.enabled);
      return row;
    })];
  });
}

// Переключатель вкл/выкл в строке дерева: новое состояние сохраняется сразу.
function toggle(on, label, change, reload, toast) {
  const button = el("button", `switch${on ? " on" : ""}`);
  button.type = "button";
  button.setAttribute("role", "switch");
  button.setAttribute("aria-checked", String(on));
  button.setAttribute("aria-label", label);
  button.title = on ? "Включён — выключить" : "Выключен — включить";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await change(!on);
      await reload();
    } catch (error) {
      toast(error);
      button.disabled = false;
    }
  });
  return button;
}

function capRow(name, description, badges, actions, lines, last) {
  return treeRow({
    prefix: guide(lines, last), label: name, note: badges.filter(Boolean).join(" · "), title: description,
    extra: actions.filter(Boolean), detail: description, detailPrefix: stem(lines, last),
  });
}

// Кнопка действия, видная всегда; «в архив» при наведении красная — убирает возможность.
function link(text, handler) {
  const button = rowAction(text, text, handler);
  button.classList.add("shown");
  button.classList.toggle("danger", ["в архив", "удалить"].includes(text));
  return button;
}

export async function reloadCapabilities(toast) {
  try {
    await api.reloadCapabilities();
    await showCapabilities(toast);
  } catch (error) {
    toast(error);
  }
}

async function archive(kind, id, reload, toast) {
  if (!window.confirm(`Убрать ${KIND[kind]} ${id} в архив? Файлы останутся, вернуть можно в разделе «Архив» внизу вкладки.`)) return;
  try {
    await api.archive(kind, id);
    await reload();
  } catch (error) {
    toast(error);
  }
}

// ---------- настройка коннектора ----------

// Поле формы по имени доступа: JIRA_URL → «Адрес», JIRA_TOKEN → «Токен» и т. д. Имя видно под полем мелко:
// по нему скрипт читает значение (os.environ).
function fieldMeta(name) {
  if (name.endsWith("_URL")) return { label: "Адрес", type: "url", placeholder: "https://jira.company.ru" };
  if (/_(EMAIL|USER|LOGIN)$/.test(name)) return { label: "Логин или почта", type: "text", placeholder: "me@company.ru" };
  if (name.endsWith("_PASSWORD")) return { label: "Пароль", type: "password" };
  if (/_(TOKEN|KEY)$/.test(name)) return { label: "Токен", type: "password" };
  if (name.endsWith("_VERIFY_SSL")) return { label: "Проверять сертификат HTTPS", type: "checkbox" };
  return { label: name, type: "password" };
}

// Подсказки для известных коннекторов: где взять значение.
const FIELD_HINTS = {
  JIRA_URL: "Адрес, по которому вы открываете Jira в браузере, без /browse/…",
  JIRA_TOKEN: "Jira Server / Data Center: аватар → Профиль → Personal Access Tokens → Создать. "
    + "Jira Cloud (*.atlassian.net): id.atlassian.com → Security → API tokens.",
  JIRA_EMAIL: "Только для Jira Cloud — почта вашей учётной записи. Для корпоративной Jira оставьте пустым.",
  JIRA_VERIFY_SSL: "Снимите, если Jira на корпоративном (самоподписанном) сертификате и проверка пишет про CERTIFICATE.",
};

let connectorDialog = null;

function openConnector(tool, reload, toast) {
  connectorDialog?.remove();
  const dialog = connectorDialog = el("dialog", "connector-dialog");
  const form = el("form", "connector");
  form.method = "dialog";
  const about = el("div", "connector-about");
  about.append(
    el("p", "", tool.full_description ?? tool.description),
    el("p", "hint", "Заполните доступы — и агент сам будет вызывать этот инструмент, когда вы попросите "
      + "(например: «покажи мои задачи в Jira»). Значения хранятся только у вас (data/secrets.json); "
      + "агент и чат их не видят. Токен и пароль после сохранения не показываются — только «задан»."),
  );
  form.append(el("h3", "", `Настройка: ${tool.name}`), about);
  // Порядок как в обычной форме входа: адрес, логин, токен или пароль, галочки — в конце.
  const order = { url: 0, text: 1, password: 2, checkbox: 3 };
  const fields = [...tool.fields].sort((a, b) => order[fieldMeta(a.name).type] - order[fieldMeta(b.name).type]);
  const inputs = fields.map((field) => {
    const meta = fieldMeta(field.name);
    const label = el("label", meta.type === "checkbox" ? "check" : "");
    const input = el("input");
    input.type = meta.type;
    input.name = field.name;
    if (meta.type === "checkbox") {
      input.checked = (field.value ?? "true").toLowerCase() !== "false";
      label.append(input, el("span", "", meta.label));
    } else {
      input.autocomplete = meta.type === "password" ? "new-password" : "off";
      input.spellcheck = false;
      input.value = meta.type === "password" ? "" : field.value ?? "";
      input.placeholder = meta.type === "password" && field.set ? "задан — оставьте пустым, чтобы не менять" : meta.placeholder ?? "";
      const title = el("span", "", `${meta.label}${field.optional ? " (необязательно)" : ""}`);
      if (field.set && meta.type === "password") title.append(el("span", "connector-set", " · задан"));
      label.append(title, input);
    }
    const hint = FIELD_HINTS[field.name];
    label.append(el("span", "connector-name", `${hint ? `${hint} · ` : ""}${field.name}`));
    form.append(label);
    return { field, meta, input };
  });
  const status = el("span", "connector-status");
  status.setAttribute("role", "status");
  const actions = el("div", "dialog-actions");
  const check = el("button", "", "Проверить подключение");
  check.type = "button";
  check.hidden = !tool.checkable;
  const close = el("button", "", "Закрыть");
  close.type = "button";
  const save = el("button", "primary", "Сохранить");
  save.type = "submit";
  actions.append(status, check, close, save);
  form.append(actions);
  dialog.append(form);
  document.body.append(dialog);

  const show = (text, ok) => {
    status.textContent = text;
    status.classList.toggle("ok", ok === true);
    status.classList.toggle("failed", ok === false);
  };
  // Запоминаем, что уже записано: повторное «Проверить» не пишет то же самое и не теряет снятую галочку.
  const put = async (field, value) => {
    await api.setSecret(field.name, value);
    Object.assign(field, { set: true, value });
  };
  const drop = async (field) => {
    await api.deleteSecret(field.name);
    Object.assign(field, { set: false, value: undefined });
  };
  const store = async () => {
    for (const { field, meta, input } of inputs) {
      if (meta.type === "checkbox") {
        // Проверка включена по умолчанию: храним только «выключено».
        if (!input.checked && field.value !== "false") await put(field, "false");
        else if (input.checked && field.set) await drop(field);
        continue;
      }
      const value = input.value.trim();
      if (meta.type === "password") {
        if (value) {
          await put(field, value);
          input.value = "";
          input.placeholder = "задан — оставьте пустым, чтобы не менять";
        }
      } else if (value !== (field.value ?? "")) {
        if (value) await put(field, value);
        else if (field.set) await drop(field);
      }
    }
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await store();
      dialog.close();
      await reload();
    } catch (error) {
      show(error.message, false);
    }
  });
  check.addEventListener("click", async () => {
    show("Сохраняю и проверяю…");
    try {
      await store();
      show((await api.checkConnector(tool.id)).message, true);
      void reload();
    } catch (error) {
      show(error.message, false);
    }
  });
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  dialog.showModal();
}

// Коннектору не хватает пакетов (requires в tool.md): ставит их пользователь — нажатие и есть согласие.
function installLink(tool, reload, toast) {
  const button = link("установить пакеты", async () => {
    button.disabled = true;
    button.textContent = "ставлю…";
    try {
      await api.installPackages(tool.missing);
      await reload();
    } catch (error) {
      toast(error);
      button.disabled = false;
      button.textContent = "установить пакеты";
    }
  });
  button.title = `Поставить в песочницу: ${tool.missing.join(", ")}`;
  return button;
}

async function uninstall(item, reload, toast) {
  const users = item.tools.length ? ` Он нужен инструментам: ${item.tools.join(", ")} — они перестанут работать.` : "";
  if (!window.confirm(`Удалить пакет ${item.name} из песочницы?${users}`)) return;
  try {
    await api.uninstallPackage(item.name);
    await reload();
  } catch (error) {
    toast(error);
  }
}

// ---------- окно папки ----------

// Возможность открывается папкой: слева дерево файлов, справа — открытый файл. У агента файлы можно добавить
// (steps.md из шаблона, скрипт) и удалить, у коннектора — только править. «Сохранить» отправляет все правки разом:
// сервер проверяет папку копией и записывает её, только если она рабочая.
let folder = null;
const SCRIPT_NAME = /^[A-Za-z0-9_][A-Za-z0-9_.-]*\.py$/;
const SCRIPT_TEMPLATE = `"""Что делает скрипт. Аргументы — JSON в stdin, итог — в stdout, ход работы — строками в stderr."""

import json
import sys

args = json.load(sys.stdin)
print(args)
`;

async function showSource(kind, id, reload, toast) {
  try {
    const source = await api.capabilitySource(kind, id);
    openFolder({ kind, id, path: source.path, files: source.files, reload });
  } catch (error) {
    toast(error);
  }
}

function openFolder({ kind, id, path, files, reload, create = false }) {
  folder = {
    kind, id, reload, create, deleted: new Set(), current: Object.keys(files)[0],
    files: new Map(Object.entries(files).map(([name, text]) => [name, { text, original: create ? null : text }])),
  };
  $("#source-title").textContent = create ? "Новый агент" : `${KIND[kind][0].toUpperCase()}${KIND[kind].slice(1)} ${id}`;
  $("#source-path").textContent = path;
  $("#source-id-label").hidden = !create;
  $("#source-id").value = "";
  $("#source-status").textContent = "";
  renderFolder();
  $("#source-dialog").showModal();
  if (create) $("#source-id").focus();
}

// Правки окна: изменённые и новые файлы — текстом, удалённые — null.
function changes() {
  const result = {};
  for (const [name, file] of folder.files) if (file.text !== file.original) result[name] = file.text;
  for (const name of folder.deleted) result[name] = null;
  return result;
}

function fileState(name) {
  const file = folder.files.get(name);
  return file.original === null ? "новый" : file.text !== file.original ? "изменён" : "";
}

// AGENT.md первым, затем steps.md, затем папка scripts.
function fileOrder(a, b) {
  const rank = (name) => (name === "AGENT.md" ? 0 : name === "steps.md" ? 1 : name.includes("/") ? 3 : 2);
  return rank(a) - rank(b) || a.localeCompare(b);
}

function renderFolder() {
  // У агента — AGENT.md, steps.md, scripts/…; у коннектора — в порядке сервера.
  const names = folder.kind === "agent" ? [...folder.files.keys()].sort(fileOrder) : [...folder.files.keys()];
  const row = (name, label, prefix = "") => treeRow({
    prefix, label, note: fileState(name), current: name === folder.current, onClick: () => {
      folder.current = name;
      renderFolder();
    },
  });
  const top = names.filter((name) => !name.includes("/"));
  const nested = names.filter((name) => name.includes("/"));
  const folders = [...new Set(nested.map((name) => name.split("/")[0]))];
  $("#source-tree").replaceChildren(
    ...top.map((name) => row(name, name)),
    ...folders.flatMap((group) => {
      const items = nested.filter((name) => name.startsWith(`${group}/`));
      return [treeRow({ label: `${group}/`, group: true }),
        ...items.map((name, index) => row(name, name.slice(group.length + 1), guide([], index === items.length - 1)))];
    }),
  );
  const file = folder.files.get(folder.current);
  const editor = el("textarea", "source-editor");
  Object.assign(editor, { value: file.text, spellcheck: false });
  editor.setAttribute("aria-label", folder.current);
  editor.addEventListener("input", () => {
    file.text = editor.value;
    $("#source-status").textContent = "";
    for (const note of $("#source-tree").querySelectorAll(".trow.current .trow-note")) note.textContent = fileState(folder.current);
    sync();
  });
  $("#source-files").replaceChildren(
    el("div", "source-file-name", folder.current),
    ...(folder.current === "steps.md" ? [stepsHelp()] : []),
    editor,
  );
  const agent = folder.kind === "agent" && !folder.create;
  $("#source-add-steps").hidden = !agent || folder.files.has("steps.md");
  $("#source-add-script").hidden = !agent;
  $("#source-delete").hidden = !agent || folder.current === "AGENT.md";
  sync();
}

function sync() {
  $("#source-save").disabled = !folder.create && !Object.keys(changes()).length;
}

function addFile(name, text) {
  if (!folder.files.has(name)) {
    folder.files.set(name, { text, original: null });
    folder.deleted.delete(name);
  }
  folder.current = name;
  renderFolder();
}

export function initFolder() {
  $("#source-add-steps").addEventListener("click", () => addFile("steps.md", capData?.templates?.steps ?? ""));
  $("#source-add-script").addEventListener("click", () => {
    const name = window.prompt("Имя скрипта — файл .py в папке scripts, например report.py", "script.py")?.trim();
    if (!name) return;
    if (!SCRIPT_NAME.test(name)) {
      $("#source-status").textContent = "Имя скрипта — латиница, цифры, _ . -, на конце .py";
      return;
    }
    addFile(`scripts/${name}`, SCRIPT_TEMPLATE);
  });
  $("#source-delete").addEventListener("click", () => {
    const name = folder.current;
    if (name === "AGENT.md" || !window.confirm(`Удалить ${name}? Файл исчезнет после «Сохранить».`)) return;
    if (folder.files.get(name).original !== null) folder.deleted.add(name);
    folder.files.delete(name);
    folder.current = "AGENT.md";
    renderFolder();
  });
  $("#source-close").addEventListener("click", () => {
    if (Object.keys(changes()).length && !folder.create && !window.confirm("Закрыть без сохранения? Правки пропадут.")) return;
    $("#source-dialog").close();
  });
  $("#source-save").addEventListener("click", saveFolder);
}

async function saveFolder() {
  const save = $("#source-save");
  save.disabled = true;
  $("#source-status").textContent = "Сохраняю…";
  try {
    if (folder.create) {
      const main = "AGENT.md";
      await api.createCapability(folder.kind, $("#source-id").value.trim(), folder.files.get(main).text);
      $("#source-dialog").close();
    } else {
      await api.saveSource(folder.kind, folder.id, changes());
      for (const file of folder.files.values()) file.original = file.text;
      folder.deleted.clear();
      $("#source-status").textContent = "Сохранено";
      renderFolder();
    }
    await folder.reload();
  } catch (error) {
    $("#source-status").textContent = error.message;
    sync();
  }
}

// Шпаргалка по формату steps.md — над редактором, свёрнута: её раскрывают, когда пишут шаги.
const STEPS_HELP = [
  ["## Нужно от вас", "что спросить у пользователя: строки «- имя — подсказка»; «(необязательно)» — поле можно не заполнять"],
  ["## Название шага", "каждый шаг — раздел; в нём одна из строк ниже"],
  ["модель: задание", "один ответ модели, без инструментов"],
  ["агент: задание", "модель с инструментами; ниже — «инструменты: браузер, файлы, чтение, интернет» или их id"],
  ["команда: pytest -q", "команда в папке проекта; длинная — блоком ``` под строкой «команда:»"],
  ["инструмент: id", "один вызов инструмента; ниже — «параметры: {\"path\": \"{папка}\"}»"],
  ["скрипт: имя.py", "скрипт из папки scripts агента в песочнице; ниже — «параметры: {json}», он получит их в stdin"],
  ["файлы: пути", "что шаг прочитает перед работой: файлы и папки проекта"],
  ["сохранить: путь", "куда записать итог шага — так его получит следующий шаг"],
  ["при ошибке: …", "«стоп» (по умолчанию), «дальше» или «Название шага, 3» — вернуться и повторить до 3 раз"],
  ["таймаут: 600 · шагов: 30", "сколько секунд ждать команду или скрипт и сколько действий у агента"],
  ["{имя}", "подставляет значение из «Нужно от вас»"],
];

function stepsHelp() {
  const box = el("details", "source-help");
  box.append(el("summary", "", "Как устроены шаги (steps.md)"));
  const list = el("dl");
  for (const [line, about] of STEPS_HELP) list.append(el("dt", "", line), el("dd", "", about));
  box.append(list);
  return box;
}

// «＋ Новый агент»: то же окно с полем id и шаблоном WORKFLOW.md. Шаги и скрипты добавляются потом — в окне агента.
function openNew(kind, template, reload) {
  const agent = kind === "agent";
  openFolder({ kind, id: "", path: "Папка появится в data/agents/<id>/",
    files: { "AGENT.md": template }, reload, create: true });
}

// ---------- память ----------

let memoryCurrent = null;
let memoryDocuments = [];

export async function showMemory(toast) {
  try {
    memoryDocuments = await api.memory();
    memoryCurrent ??= (memoryDocuments.find((item) => item.name === "profile.md") ?? memoryDocuments[0])?.name ?? null;
    renderMemoryList(toast);
    if (memoryCurrent) await openMemory(memoryCurrent, toast);
  } catch (error) {
    toast(error);
  }
}

function renderMemoryList(toast) {
  $("#memory-list").replaceChildren(treeRow({ label: "data/memory", group: true }), ...branch(memoryDocuments, [], (document, lines, last) => treeRow({
    prefix: guide(lines, last), label: document.title, note: document.name, title: document.name,
    current: document.name === memoryCurrent, onClick: () => openMemory(document.name, toast),
  })));
}

async function openMemory(name, toast) {
  try {
    const document = await api.memoryDocument(name);
    memoryCurrent = name;
    $("#memory-text").value = document.content;
    $("#memory-status").textContent = `data/memory/${name}`;
    renderMemoryList(toast);
  } catch (error) {
    toast(error);
  }
}

export async function saveMemory(toast) {
  if (!memoryCurrent) return;
  try {
    await api.saveMemory(memoryCurrent, $("#memory-text").value);
    $("#memory-status").textContent = "Сохранено";
  } catch (error) {
    toast(error);
  }
}
