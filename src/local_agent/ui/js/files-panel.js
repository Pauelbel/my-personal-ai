// Вкладка «Файлы»: дерево файлов проекта (папки подгружаются по щелчку), просмотр и правка выбранного файла.
import { filesApi } from "./api.js";
import { highlightInto } from "./highlight.js";
import { appendMarkdown } from "./markdown.js";
import { elements, selectedProject, showError } from "./state.js";

// Дерево и открытый файл относятся к одному проекту: при смене проекта всё сбрасывается.
let projectId = null;
let children = new Map();
let openFolders = new Set();
let openedPath = null;
let loading = new Set();
let message = "";
// Открытый файл и режим правки.
let file = null;
let editing = false;
// Путь перетаскиваемого элемента дерева.
let dragged = null;

export function initFiles() {
  acceptDrops(elements.filesTree, "");
  elements.filesEdit.addEventListener("click", () => setEditing(!editing));
  elements.filesSave.addEventListener("click", () => { void saveFile(); });
  elements.filesViewerBody.addEventListener("keydown", (event) => {
    if (event.key === "s" && (event.ctrlKey || event.metaKey) && editing) {
      event.preventDefault();
      void saveFile();
    }
  });
}

export function renderFiles() {
  const project = selectedProject();
  if (project?.id !== projectId) {
    projectId = project?.id ?? null;
    children = new Map();
    openFolders = new Set();
    openedPath = null;
    file = null;
    editing = false;
    syncActions();
    elements.filesViewerName.textContent = "";
    elements.filesViewerBody.replaceChildren();
    message = "";
    if (projectId) void loadFolder("");
  }
  const tree = elements.filesTree;
  tree.replaceChildren();
  if (!projectId) {
    tree.append(note("Сессия без проекта: файлов нет. Выберите папку под полем ввода."));
    return;
  }
  const root = children.get("");
  if (!root) tree.append(note(message || "Загрузка…"));
  else if (!root.length) tree.append(note("Папка пуста"));
  else appendEntries(tree, "", 0);
}

function appendEntries(parent, folder, depth) {
  for (const entry of children.get(folder) ?? []) {
    const item = document.createElement("div");
    item.className = "files-item";
    item.draggable = true;
    item.addEventListener("dragstart", (event) => {
      dragged = entry.path;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", entry.path);
    });
    item.addEventListener("dragend", () => { dragged = null; });
    if (entry.is_dir) acceptDrops(item, entry.path);
    const row = document.createElement("button");
    row.type = "button";
    row.className = "files-row";
    row.style.paddingLeft = `${8 + depth * 14}px`;
    const open = openFolders.has(entry.path);
    row.textContent = `${entry.is_dir ? (open ? "▾" : "▸") : "·"} ${entry.name}`;
    row.title = entry.path;
    item.classList.toggle("active", entry.path === openedPath);
    row.addEventListener("click", () => (entry.is_dir ? toggleFolder(entry.path) : void openFile(entry.path)));
    item.append(row, actionButton("✎", "Переименовать", () => void renameEntry(entry)),
      actionButton("✕", "Удалить", () => void deleteEntry(entry)));
    parent.append(item);
    if (entry.is_dir && open) {
      if (children.has(entry.path)) appendEntries(parent, entry.path, depth + 1);
      else if (loading.has(entry.path)) parent.append(note("Загрузка…"));
    }
  }
}

function actionButton(text, title, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "files-action";
  button.textContent = text;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.addEventListener("click", onClick);
  return button;
}

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
    if (source === null || parentOf(source) === folder || folder === source || folder.startsWith(`${source}/`)) return;
    void moveEntry(source, joinPath(folder, baseName(source)));
  });
}

const parentOf = (path) => path.split("/").slice(0, -1).join("/");
const baseName = (path) => path.split("/").pop();
const joinPath = (folder, name) => (folder ? `${folder}/${name}` : name);
// Путь после переноса: сам перенесённый элемент и всё, что лежит внутри него.
const remap = (path, from, to) => (path === from ? to : path.startsWith(`${from}/`) ? to + path.slice(from.length) : path);
const within = (path, from) => path === from || path.startsWith(`${from}/`);

async function renameEntry(entry) {
  const name = window.prompt("Новое имя", entry.name)?.trim();
  if (!name || name === entry.name) return;
  await moveEntry(entry.path, joinPath(parentOf(entry.path), name));
}

async function moveEntry(from, to) {
  if (openedPath && within(openedPath, from) && !confirmDiscard()) return;
  const id = projectId;
  try {
    await filesApi.move(id, from, to);
  } catch (error) {
    showError(error);
    return;
  }
  if (id !== projectId) return;
  openFolders = new Set([...openFolders].map((path) => remap(path, from, to)));
  if (openedPath && within(openedPath, from)) {
    openedPath = remap(openedPath, from, to);
    elements.filesViewerName.textContent = openedPath;
  }
  await refreshTree();
}

async function deleteEntry(entry) {
  const what = entry.is_dir ? "папку со всем содержимым" : "файл";
  if (!window.confirm(`Удалить ${what} «${entry.path}»? Это нельзя отменить.`)) return;
  const id = projectId;
  try {
    await filesApi.remove(id, entry.path);
  } catch (error) {
    showError(error);
    return;
  }
  if (id !== projectId) return;
  openFolders = new Set([...openFolders].filter((path) => !within(path, entry.path)));
  if (openedPath && within(openedPath, entry.path)) {
    openedPath = null;
    file = null;
    editing = false;
    syncActions();
    elements.filesViewerName.textContent = "";
    elements.filesViewerBody.replaceChildren();
  }
  await refreshTree();
}

// После переноса и удаления перечитываем корень и все раскрытые папки.
async function refreshTree() {
  const id = projectId;
  children = new Map();
  message = "";
  await Promise.all(["", ...openFolders].map((path) => loadFolder(path)));
  if (id === projectId) renderFiles();
}

function toggleFolder(path) {
  if (openFolders.has(path)) openFolders.delete(path);
  else {
    openFolders.add(path);
    if (!children.has(path)) void loadFolder(path);
  }
  renderFiles();
}

async function loadFolder(path) {
  const id = projectId;
  loading.add(path);
  try {
    const entries = await filesApi.list(id, path);
    if (id === projectId) children.set(path, entries);
  } catch (error) {
    if (id === projectId) {
      message = error.message;
      children.set(path, []);
    }
  } finally {
    loading.delete(path);
  }
  if (id === projectId) renderFiles();
}

async function openFile(path) {
  if (path === openedPath || !confirmDiscard()) return;
  const id = projectId;
  openedPath = path;
  file = null;
  editing = false;
  syncActions();
  elements.filesViewerName.textContent = path;
  elements.filesViewerBody.replaceChildren(note("Загрузка…"));
  renderFiles();
  try {
    const loaded = await filesApi.read(id, path);
    if (id !== projectId || path !== openedPath) return;
    file = loaded;
    syncActions();
    renderBody();
  } catch (error) {
    if (id !== projectId || path !== openedPath) return;
    elements.filesViewerBody.replaceChildren(note(error.message));
  }
}

function renderBody() {
  const body = elements.filesViewerBody;
  if (editing) {
    const editor = document.createElement("textarea");
    editor.className = "files-editor";
    editor.spellcheck = false;
    editor.setAttribute("aria-label", "Содержимое файла");
    editor.value = file.content;
    editor.addEventListener("input", () => {
      elements.filesStatus.textContent = isDirty() ? "Есть несохранённые изменения" : "";
    });
    body.replaceChildren(editor);
    editor.focus();
    return;
  }
  let view;
  if (/\.(md|markdown)$/i.test(openedPath)) {
    view = document.createElement("div");
    view.className = "message-content";
    appendMarkdown(view, file.content);
  } else {
    view = document.createElement("pre");
    view.className = "files-code";
    highlightInto(view, file.content, openedPath);
  }
  if (file.truncated) view.append(note("Файл большой: показано только начало, править его нельзя."));
  body.replaceChildren(view);
}

function syncActions() {
  elements.filesActions.hidden = !file;
  elements.filesEdit.hidden = !file || file.truncated;
  elements.filesEdit.textContent = editing ? "👁 Просмотр" : "✎ Править";
  elements.filesSave.hidden = !editing;
  elements.filesStatus.textContent = "";
}

// Текст из редактора; textarea отдаёт только \n, поэтому файл с CRLF сохраняем с CRLF.
function editorText() {
  const text = elements.filesViewerBody.querySelector(".files-editor").value;
  return file.content.includes("\r\n") ? text.replace(/\n/g, "\r\n") : text;
}

const isDirty = () => editing && editorText() !== file.content;
const confirmDiscard = () => !isDirty() || window.confirm("Есть несохранённые изменения. Отбросить их?");

function setEditing(value) {
  if (!file || value === editing || !confirmDiscard()) return;
  editing = value;
  syncActions();
  renderBody();
}

async function saveFile() {
  if (!editing) return;
  const id = projectId;
  const path = openedPath;
  elements.filesSave.disabled = true;
  try {
    const saved = await filesApi.write(id, path, editorText());
    if (id !== projectId || path !== openedPath) return;
    file = saved;
    elements.filesStatus.textContent = "Сохранено";
  } catch (error) {
    if (id === projectId && path === openedPath) elements.filesStatus.textContent = error.message;
  } finally {
    elements.filesSave.disabled = false;
  }
}

function note(text) {
  const item = document.createElement("p");
  item.className = "files-note";
  item.textContent = text;
  return item;
}
