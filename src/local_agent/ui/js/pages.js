// Страницы вне чата: вики, возможности агента, долговременная память.
// Списки — деревья в стиле терминала (tree.js), возможности разложены по вкладкам.
import { api } from "./api.js";
import { el, renderMarkdown } from "./chat.js";
import { guide, rowAction, stem, treeRow } from "./tree.js";

const $ = (selector) => document.querySelector(selector);
const ORIGIN = { builtin: "Встроенные", agent: "Созданы агентом", extra: "Дополнения" };
const KIND = { tool: "инструмент", skill: "скилл", agent: "саб-агент" };
const ARCHIVE_GROUP = { tool: "Инструменты", skill: "Скиллы", agent: "Саб-агенты" };

// Строки детей одной группы: ветки «├─», у последней — «└─».
function branch(items, lines, build) {
  return items.flatMap((item, index) => build(item, lines, index === items.length - 1));
}

// ---------- вики ----------

let wikiCurrent = null;
const wikiClosed = new Set();
// Последний показанный список: после открытия страницы он перерисовывается без нового запроса.
let wikiShown = { items: [], query: "" };

export async function showWiki(toast) {
  try {
    const query = $("#wiki-search").value.trim();
    const items = query
      ? (await api.wikiSearch(query)).map((item) => ({ path: item.path, title: item.title, note: item.text }))
      : (await api.wiki()).map((page) => ({ path: page.path, title: page.title }));
    const unique = [...new Map(items.map((item) => [item.path, item])).values()];
    renderWikiList(unique, query, toast);
    if (wikiCurrent && !query) await openWiki(wikiCurrent, toast, false);
  } catch (error) {
    toast(error);
  }
}

// Без поиска — дерево папок data/wiki, с поиском — найденные страницы одной группой с фрагментом текста.
function renderWikiList(items, query, toast) {
  wikiShown = { items, query };
  const root = $("#wiki-list");
  if (!items.length) {
    root.replaceChildren(el("p", "hint", query ? "Ничего не нашлось." : "Заметок пока нет."));
    return;
  }
  const open = (path) => () => openWiki(path, toast);
  if (query) {
    root.replaceChildren(treeRow({ label: `найдено: ${items.length}`, group: true }), ...branch(items, [], (item, lines, last) => treeRow({
      prefix: guide(lines, last), label: item.title, title: item.path, current: item.path === wikiCurrent, onClick: open(item.path),
      detail: item.note, detailPrefix: stem(lines, last),
    })));
    return;
  }
  const tree = { folders: new Map(), pages: [] };
  for (const item of items) {
    const parts = item.path.split("/");
    let node = tree;
    for (const folder of parts.slice(0, -1)) {
      if (!node.folders.has(folder)) node.folders.set(folder, { folders: new Map(), pages: [] });
      node = node.folders.get(folder);
    }
    node.pages.push(item);
  }
  const rows = (node, path, lines) => branch([...node.folders.entries(), ...node.pages], lines, (entry, ancestors, last) => {
    if (!Array.isArray(entry)) {
      return [treeRow({ prefix: guide(ancestors, last), label: entry.title, title: entry.path, current: entry.path === wikiCurrent, onClick: open(entry.path) })];
    }
    const [name, child] = entry;
    const folder = path ? `${path}/${name}` : name;
    const opened = !wikiClosed.has(folder);
    const head = treeRow({
      prefix: guide(ancestors, last), label: name, group: true, open: opened,
      onClick: () => {
        if (!wikiClosed.delete(folder)) wikiClosed.add(folder);
        renderWikiList(items, query, toast);
      },
    });
    return opened ? [head, ...rows(child, folder, [...ancestors, !last])] : [head];
  });
  root.replaceChildren(treeRow({ label: "data/wiki", group: true }), ...rows(tree, "", []));
}

async function openWiki(path, toast, refreshList = true) {
  try {
    const page = await api.wikiPage(path);
    wikiCurrent = path;
    const reader = $("#wiki-page");
    reader.replaceChildren(el("p", "hint", `data/wiki/${path}`));
    renderMarkdown(reader, page.content);
    if (refreshList) renderWikiList(wikiShown.items, wikiShown.query, toast);
  } catch (error) {
    toast(error);
  }
}

// ---------- возможности ----------

const CAP_TABS = [["tools", "Инструменты"], ["skills", "Скиллы"], ["agents", "Саб-агенты"], ["mcp", "MCP"], ["archive", "Архив"]];
let capTab = recall("capabilities-tab") ?? "tools";
let capData = null;

export async function showCapabilities(toast) {
  try {
    capData = await api.capabilities();
    renderCapabilities(toast);
  } catch (error) {
    toast(error);
  }
}

function renderCapabilities(toast) {
  const data = capData;
  const reload = () => showCapabilities(toast);
  const counts = {
    tools: data.tools.filter((tool) => tool.origin !== "mcp").length, skills: data.skills.length,
    agents: data.agents.length, mcp: data.mcp.length, archive: data.archive.length,
  };
  $("#cap-tabs").replaceChildren(...CAP_TABS.map(([id, label]) => {
    const tab = el("button", "", label);
    tab.type = "button";
    tab.setAttribute("aria-pressed", String(id === capTab));
    tab.append(el("span", "count", String(counts[id])));
    tab.addEventListener("click", () => {
      capTab = id;
      remember("capabilities-tab", id);
      renderCapabilities(toast);
    });
    return tab;
  }));
  const views = {
    tools: () => groups(Object.entries(ORIGIN).map(([origin, title]) => [title, data.tools.filter((tool) => tool.origin === origin)]),
      (tool, lines, last) => capRow(tool.id, tool.description, [tool.requires_approval ? "с подтверждением" : "без подтверждения"], [
        tool.origin === "agent" && link("код", () => showSource("tool", tool.id, toast)),
        ["agent", "extra"].includes(tool.origin) && link("в архив", () => archive("tool", tool.id, reload, toast)),
      ], lines, last)),
    skills: () => groups([["data/skills", data.skills]], (skill, lines, last) => capRow(skill.id, skill.description, [], [
      link("текст", () => showSource("skill", skill.id, toast)),
      link("в архив", () => archive("skill", skill.id, reload, toast)),
    ], lines, last), "Скиллов пока нет — агент предложит их, когда задачи начнут повторяться."),
    agents: () => groups([["data/agents", data.agents]], (agent, lines, last) => capRow(agent.name, agent.description, [
      agent.tools.length ? agent.tools.join(", ") : "без инструментов",
    ], [
      link("промпт", () => showSource("agent", agent.id, toast)),
      link("в архив", () => archive("agent", agent.id, reload, toast)),
    ], lines, last), "Саб-агентов пока нет — агент предложит их для повторяющихся направлений."),
    mcp: () => mcpRows(data),
    archive: () => groups(Object.entries(ARCHIVE_GROUP).map(([kind, title]) => [title, data.archive.filter((item) => item.kind === kind)]),
      (item, lines, last) => capRow(item.id, item.name === item.id ? "" : item.name, [], [
        link("вернуть", async () => {
          try {
            await api.restore(item.kind, item.archive_id);
            await reload();
          } catch (error) {
            toast(error);
          }
        }),
      ], lines, last), "Архив пуст."),
  };
  $("#capabilities").replaceChildren(...(views[capTab] ?? views.tools)());
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

function mcpRows(data) {
  if (!data.mcp.length) return [el("p", "hint", "MCP выключен (MCP_ENABLED=false) или серверов нет в data/mcp.json.")];
  const described = new Map(data.tools.map((tool) => [tool.id, tool.description]));
  return data.mcp.flatMap((server) => {
    const head = treeRow({ label: server.name, group: true, note: server.status, title: server.description });
    head.classList.toggle("failed", server.status !== "работает");
    const tools = server.tools ?? [];
    return [
      head,
      ...(tools.length ? branch(tools, [], (id, lines, last) => capRow(id, described.get(id) ?? "", [], [], lines, last))
        : [treeRow({ prefix: guide([], true), label: server.description })]),
    ];
  });
}

function capRow(name, description, badges, actions, lines, last) {
  return treeRow({
    prefix: guide(lines, last), label: name, note: badges.filter(Boolean).join(" · "), title: description,
    extra: actions.filter(Boolean), detail: description, detailPrefix: stem(lines, last),
  });
}

function link(text, handler) {
  const button = rowAction(text, text, handler);
  button.classList.add("shown");
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
  if (!window.confirm(`Убрать ${KIND[kind]} ${id} в архив? Файлы останутся, вернуть можно во вкладке «Архив».`)) return;
  try {
    await api.archive(kind, id);
    await reload();
  } catch (error) {
    toast(error);
  }
}

async function showSource(kind, id, toast) {
  try {
    const source = await api.capabilitySource(kind, id);
    $("#source-title").textContent = `${KIND[kind]} ${id}`;
    $("#source-path").textContent = `${source.path} — правьте файлы в редакторе, затем «Перечитать с диска».`;
    $("#source-files").replaceChildren(...Object.entries(source.files).flatMap(([name, text]) => {
      const pre = el("pre");
      const code = el("code");
      code.textContent = text;
      pre.append(code);
      return [el("h4", "", name), pre];
    }));
    $("#source-dialog").showModal();
  } catch (error) {
    toast(error);
  }
}

function remember(key, value) {
  try { localStorage.setItem(key, value); } catch { /* без хранилища просто не запомним */ }
}

function recall(key) {
  try { return localStorage.getItem(key); } catch { return null; }
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
