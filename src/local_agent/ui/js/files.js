// Файлы проекта: дерево (папки подгружаются по щелчку), просмотр с подсветкой и номерами строк или markdown,
// переход к строке, правка, перенос и удаление. Пути из чата открываются здесь же (openPath).
import { api } from "./api.js";
import { el, renderMarkdown } from "./chat.js";
import { highlightInto } from "./highlight.js";
import { guide, rowAction, treeRow } from "./tree.js";

const $ = (selector) => document.querySelector(selector);
// Дерево и открытый файл относятся к одному проекту: при смене проекта всё сбрасывается.
const view = {
  projectId: null, children: new Map(), open: new Set(), loading: new Set(), message: "",
  path: null, file: null, editing: false,
  // source — Markdown исходником, а не отрисованным; line — подсвеченная строка исходника.
  source: false, line: null,
};
let toast = () => {};
// Открытый файл каждого проекта: после перезагрузки страницы он открывается снова.
const OPENED_KEY = "files-opened";
// Путь перетаскиваемого элемента дерева.
let dragged = null;

export function initFiles(showError) {
  toast = showError;
  acceptDrops($("#files-tree"), "");
  $("#files-edit").addEventListener("click", () => setEditing(!view.editing));
  $("#files-save").addEventListener("click", () => void saveFile());
  $("#files-source").addEventListener("click", () => {
    view.source = !view.source;
    syncActions();
    renderBody();
  });
  $("#files-body").addEventListener("keydown", (event) => {
    if (event.key === "s" && (event.ctrlKey || event.metaKey) && view.editing) {
      event.preventDefault();
      void saveFile();
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (isDirty()) event.preventDefault();
  });
}

export async function showFiles(project) {
  $("#files-root").textContent = project?.workspace ?? "";
  if (project?.id !== view.projectId) {
    if (!confirmDiscard()) return;
    Object.assign(view, { projectId: project?.id ?? null, children: new Map(), open: new Set(), message: "" });
    closeFile();
  }
  if (!view.projectId) return renderTree();
  await refreshTree();
  const saved = opened()[view.projectId];
  if (saved && !view.path) await openPath(saved, null, true);
}

function opened() {
  try {
    return JSON.parse(localStorage.getItem(OPENED_KEY)) ?? {};
  } catch {
    return {};
  }
}

function rememberOpened(path) {
  const all = opened();
  if (path) all[view.projectId] = path;
  else delete all[view.projectId];
  try {
    localStorage.setItem(OPENED_KEY, JSON.stringify(all));
  } catch {
    // Без хранилища просто не запомним.
  }
}

// ---------- дерево ----------

function renderTree() {
  const tree = $("#files-tree");
  const root = view.children.get("");
  if (!view.projectId) tree.replaceChildren(note("Проект сессии не найден: файлов нет."));
  else if (!root) tree.replaceChildren(note(view.message || "Загрузка…"));
  else if (!root.length) tree.replaceChildren(note("Папка пуста."));
  else {
    tree.replaceChildren();
    appendEntries(tree, "", null);
  }
}

// lines — для каждого уровня предков, есть ли после него соседи; у корня (null) ветки не рисуются.
function appendEntries(parent, folder, lines) {
  const entries = view.children.get(folder) ?? [];
  entries.forEach((entry, index) => {
    const last = index === entries.length - 1;
    const open = view.open.has(entry.path);
    const row = treeRow({
      prefix: guide(lines, last), label: entry.name, title: entry.path, group: entry.is_dir, open,
      current: entry.path === view.path,
      onClick: () => (entry.is_dir ? toggleFolder(entry.path) : void openFile(entry.path)),
      extra: [rowAction("✎", "Переименовать", () => void renameEntry(entry)), rowAction("✕", "Удалить", () => void deleteEntry(entry))],
    });
    row.draggable = true;
    row.addEventListener("dragstart", (event) => {
      dragged = entry.path;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", entry.path);
    });
    row.addEventListener("dragend", () => { dragged = null; });
    if (entry.is_dir) acceptDrops(row, entry.path);
    parent.append(row);

    if (entry.is_dir && open) {
      const nested = lines === null ? [] : [...lines, !last];
      if (view.children.has(entry.path)) appendEntries(parent, entry.path, nested);
      else if (view.loading.has(entry.path)) parent.append(note("Загрузка…"));
    }
  });
}

function toggleFolder(path) {
  if (view.open.has(path)) view.open.delete(path);
  else {
    view.open.add(path);
    if (!view.children.has(path)) void loadFolder(path);
  }
  renderTree();
}

// Подгружает раскрытую папку; корень читает refreshTree.
async function loadFolder(path) {
  const id = view.projectId;
  view.loading.add(path);
  try {
    const entries = await api.files(id, path);
    if (id === view.projectId) view.children.set(path, entries);
  } catch (error) {
    if (id !== view.projectId) return;
    view.open.delete(path);
    toast(error);
  } finally {
    view.loading.delete(path);
  }
  if (id === view.projectId) renderTree();
}

// Перечитываем корень и все раскрытые папки: после переноса, удаления и при каждом заходе на вкладку.
async function refreshTree() {
  const id = view.projectId;
  const folders = ["", ...view.open];
  const loaded = new Map();
  view.message = "";
  await Promise.all(folders.map(async (path) => {
    try {
      loaded.set(path, await api.files(id, path));
    } catch (error) {
      if (!path) view.message = error.message;
    }
  }));
  if (id !== view.projectId) return;
  // Папки, раскрытые, пока шёл этот запрос (например, переходом по пути из чата), не теряем:
  // их содержимое прочитал другой запрос.
  for (const [path, entries] of view.children) {
    if (!folders.includes(path) && view.open.has(path)) loaded.set(path, entries);
  }
  view.children = loaded;
  view.open = new Set([...view.open].filter((path) => loaded.has(path) || !folders.includes(path)));
  renderTree();
}

// ---------- перенос, переименование, удаление ----------

const parentOf = (path) => path.split("/").slice(0, -1).join("/");
const baseName = (path) => path.split("/").pop();
const joinPath = (folder, name) => (folder ? `${folder}/${name}` : name);
const within = (path, from) => path === from || path.startsWith(`${from}/`);
// Путь после переноса: сам перенесённый элемент и всё, что лежит внутри него.
const remap = (path, from, to) => (within(path, from) ? to + path.slice(from.length) : path);

// Папка (или корень дерева) принимает перетаскиваемый файл или папку.
function acceptDrops(element, folder) {
  element.addEventListener("dragover", (event) => {
    if (dragged === null) return;
    event.preventDefault();
    event.stopPropagation();
    element.classList.add("drop");
  });
  element.addEventListener("dragleave", () => element.classList.remove("drop"));
  element.addEventListener("drop", (event) => {
    event.preventDefault();
    event.stopPropagation();
    element.classList.remove("drop");
    const source = dragged;
    dragged = null;
    if (source === null || parentOf(source) === folder || within(folder, source)) return;
    void moveEntry(source, joinPath(folder, baseName(source)));
  });
}

async function renameEntry(entry) {
  const name = window.prompt("Новое имя", entry.name)?.trim();
  if (!name || name === entry.name) return;
  await moveEntry(entry.path, joinPath(parentOf(entry.path), name));
}

async function moveEntry(from, to) {
  if (view.path && within(view.path, from) && !confirmDiscard()) return;
  try {
    await api.moveFile(view.projectId, from, to);
  } catch (error) {
    toast(error);
    return;
  }
  view.open = new Set([...view.open].map((path) => remap(path, from, to)));
  if (view.path && within(view.path, from)) {
    view.path = remap(view.path, from, to);
    showPath();
  }
  await refreshTree();
}

async function deleteEntry(entry) {
  const what = entry.is_dir ? "папку со всем содержимым" : "файл";
  if (!window.confirm(`Удалить ${what} «${entry.path}»? Это нельзя отменить.`)) return;
  try {
    await api.deleteFile(view.projectId, entry.path);
  } catch (error) {
    toast(error);
    return;
  }
  view.open = new Set([...view.open].filter((path) => !within(path, entry.path)));
  if (view.path && within(view.path, entry.path)) closeFile();
  await refreshTree();
}

// ---------- открытый файл ----------

// Путь из чата: относительный к папке проекта или абсолютный внутри неё. Папка раскрывается в дереве,
// файл открывается; line — строка, к которой перейти. quiet — восстановление после перезагрузки:
// файла уже нет — молча забываем его, без ошибки.
export async function openPath(path, line = null, quiet = false) {
  if (!view.projectId) return;
  const id = view.projectId;
  const folders = path.split("/").slice(0, -1).map((_, index, parts) => parts.slice(0, index + 1).join("/"));
  let folder = false;
  try {
    await api.file(id, path);
  } catch {
    try {
      await api.files(id, path);
      folder = true;
    } catch {
      if (quiet) rememberOpened(null);
      else toast(new Error(`В проекте нет «${path}»`));
      return;
    }
  }
  if (id !== view.projectId) return;
  for (const item of folder ? [...folders, path] : folders) view.open.add(item);
  await refreshTree();
  if (folder) return;
  await openFile(path);
  if (line) goToLine(line);
}

async function openFile(path) {
  if (path === view.path || !confirmDiscard()) return;
  const id = view.projectId;
  Object.assign(view, { path, file: null, editing: false, source: false, line: null });
  syncActions();
  showPath();
  $("#files-body").replaceChildren(note("Загрузка…"));
  renderTree();
  try {
    const file = await api.file(id, path);
    if (id !== view.projectId || path !== view.path) return;
    view.file = file;
    rememberOpened(path);
    syncActions();
    renderBody();
  } catch (error) {
    if (id === view.projectId && path === view.path) $("#files-body").replaceChildren(note(error.message));
  }
}

function closeFile() {
  if (view.path && view.projectId) rememberOpened(null);
  Object.assign(view, { path: null, file: null, editing: false });
  syncActions();
  showPath();
  $("#files-body").replaceChildren(note("Выберите файл слева."));
}

// Путь показываем крошками: последняя часть — имя файла, полный путь — в подсказке.
function showPath() {
  const target = $("#files-path");
  target.title = view.path ?? "";
  if (!view.path) return target.replaceChildren();
  const parts = view.path.split("/");
  const name = el("strong", "", parts.pop());
  target.replaceChildren(...(parts.length ? [el("span", "files-dir", `${parts.join(" / ")} /`)] : []), name);
}

function renderBody() {
  const body = $("#files-body");
  if (view.editing) {
    const editor = el("textarea", "files-editor");
    editor.spellcheck = false;
    editor.setAttribute("aria-label", "Содержимое файла");
    editor.value = view.file.content;
    editor.addEventListener("input", () => {
      $("#files-status").textContent = isDirty() ? "Есть несохранённые изменения" : "";
    });
    body.replaceChildren(editor);
    body.classList.add("editing");
    editor.setSelectionRange(0, 0);
    editor.focus();
    editor.scrollTop = 0;
    return;
  }
  body.classList.remove("editing");
  let content;
  if (isMarkdown() && !view.source) {
    content = markdownView(view.file.content);
  } else {
    content = codeView(view.file.content, view.path);
  }
  body.replaceChildren(content);
  if (view.file.truncated) body.prepend(note("Файл большой: показано только начало, править его нельзя."));
  body.scrollTop = 0;
}

const isMarkdown = () => /\.(md|markdown)$/i.test(view.path ?? "");

// Код с номерами строк: номера в отдельной колонке, чтобы при копировании текста они не попадали в буфер.
function codeView(text, path) {
  const lines = text.split("\n").length;
  const wrap = el("div", "code-lines");
  const gutter = el("pre", "code-gutter", Array.from({ length: lines }, (_, index) => index + 1).join("\n"));
  gutter.setAttribute("aria-hidden", "true");
  const code = el("pre", "file-view");
  highlightInto(code, text, path);
  wrap.append(gutter, code, el("div", "code-mark"));
  return wrap;
}

// Отрисованный Markdown с номерами строк исходника: у каждого блока — номер его первой строки.
// Колонка номеров отдельная и ставится по фактическому положению блоков, поэтому переносы,
// таблицы и картинки её не сбивают, а при копировании текста номера в буфер не попадают.
function markdownView(text) {
  const wrap = el("div", "md-lines");
  const gutter = el("div", "md-gutter");
  gutter.setAttribute("aria-hidden", "true");
  const doc = el("article", "reader files-markdown");
  renderMarkdown(doc, text);
  wrap.append(gutter, doc);
  const place = () => {
    const origin = wrap.getBoundingClientRect().top;
    gutter.replaceChildren(...[...doc.querySelectorAll("[data-line]")].map((block) => {
      // У строки таблицы и пункта списка число встаёт напротив первой строки текста.
      const target = block.tagName === "TR" ? block.cells[0] ?? block : block;
      const style = getComputedStyle(target);
      const lineHeight = Number.parseFloat(style.lineHeight) || Number.parseFloat(style.fontSize) * 1.5;
      const number = el("span", "", block.dataset.line);
      number.style.top = `${target.getBoundingClientRect().top - origin + Number.parseFloat(style.paddingTop) + (lineHeight - 20) / 2}px`;
      return number;
    }));
  };
  new ResizeObserver(place).observe(doc);
  return wrap;
}

// Переход к строке по ссылке «файл:42» из чата: в отрисованном Markdown подсвечивается блок,
// в который попадает строка, в остальных файлах — сама строка.
function goToLine(line) {
  if (!view.file || view.editing || !Number.isInteger(line) || line < 1) return;
  const body = $("#files-body");
  if (isMarkdown() && !view.source) {
    const blocks = [...body.querySelectorAll(".files-markdown [data-line]")];
    const block = blocks.filter((item) => Number(item.dataset.line) <= line).pop() ?? blocks[0];
    if (!block) return;
    for (const item of body.querySelectorAll(".line-mark")) item.classList.remove("line-mark");
    block.classList.add("line-mark");
    body.scrollTop = Math.max(0, block.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop - body.clientHeight / 3);
    return;
  }
  const wrap = body.querySelector(".code-lines");
  if (!wrap) return;
  const total = view.file.content.split("\n").length;
  view.line = Math.min(line, total);
  const gutter = wrap.querySelector(".code-gutter");
  const style = getComputedStyle(gutter);
  const height = Number.parseFloat(style.lineHeight);
  const mark = wrap.querySelector(".code-mark");
  mark.style.top = `${gutter.offsetTop + Number.parseFloat(style.paddingTop) + (view.line - 1) * height}px`;
  mark.style.height = `${height}px`;
  body.scrollTop = Math.max(0, mark.offsetTop - body.clientHeight / 3);
}

function syncActions() {
  $("#files-source").hidden = !view.file || view.editing || !isMarkdown();
  $("#files-source").textContent = view.source ? "👁 Вид" : "‹› Исходник";
  $("#files-edit").hidden = !view.file || view.file.truncated;
  $("#files-edit").textContent = view.editing ? "👁 Просмотр" : "✎ Править";
  $("#files-save").hidden = !view.editing;
  $("#files-status").textContent = "";
}

// Текст из редактора; textarea отдаёт только \n, поэтому файл с CRLF сохраняем с CRLF.
function editorText() {
  const text = $("#files-body .files-editor").value;
  return view.file.content.includes("\r\n") ? text.replace(/\n/g, "\r\n") : text;
}

const isDirty = () => view.editing && Boolean(view.file) && editorText() !== view.file.content;
const confirmDiscard = () => !isDirty() || window.confirm("Есть несохранённые изменения. Отбросить их?");

function setEditing(value) {
  if (!view.file || value === view.editing || !confirmDiscard()) return;
  view.editing = value;
  syncActions();
  renderBody();
}

async function saveFile() {
  if (!view.editing) return;
  const { projectId: id, path } = view;
  $("#files-save").disabled = true;
  try {
    const saved = await api.saveFile(id, path, editorText());
    if (id !== view.projectId || path !== view.path) return;
    view.file = saved;
    $("#files-status").textContent = "Сохранено";
  } catch (error) {
    if (id === view.projectId && path === view.path) $("#files-status").textContent = error.message;
  } finally {
    $("#files-save").disabled = false;
  }
}

function note(text) {
  return el("p", "hint files-note", text);
}
