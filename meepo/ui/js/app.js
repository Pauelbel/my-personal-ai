// Точка входа интерфейса: проекты и сессии слева, чат с идущим ходом, карточки подтверждения, «Стоп».
import { api } from "./api.js";
import { renderCard } from "./cards.js";
import { compact, renderMessages, scrollDown } from "./chat.js";
import { Live } from "./live.js";
import { initProjects, openProject } from "./projects.js";
import { initMemory, showMemory } from "./memory.js";
import { initWikiPage, showWiki } from "./wiki.js";
import { refreshWikiChat } from "./wikichat.js";
import { showSettings } from "./settings.js";
import { showSkills } from "./skills.js";
import { renderSidebar as renderSidebarTree } from "./sidebar.js";
import { el, fitInput, recall, remember } from "./util.js";

const $ = (selector) => document.querySelector(selector);

const state = {
  projects: [],
  sessions: [],
  current: null,
  messages: [],
  // Идущий ход открытой сессии: строка хода и чем оборвать чтение потока (сам ход на сервере идёт дальше).
  live: null,
  controller: null,
  card: null,
  collapsed: new Set(JSON.parse(recall("collapsed-projects") || "[]")),
};

function toast(error) {
  const node = $("#toast");
  node.textContent = error?.message || String(error);
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 6000);
}

const current = () => state.sessions.find((session) => session.id === state.current) ?? null;
const projectOf = (session) => state.projects.find((project) => project.id === session?.project_id) ?? null;

// ---------- проекты и сессии ----------

async function loadSessions() {
  [state.sessions, state.projects] = await Promise.all([api.sessions(), api.projects()]);
}

async function selectSession(id) {
  detach();
  showView("chat");
  state.current = id;
  state.card = current()?.pending ?? null;
  remember("session", id);
  if (window.matchMedia("(max-width: 900px)").matches) $("#sidebar").hidden = true;
  state.messages = await api.messages(id);
  const { running } = await api.turn(id);
  render({ running });
  if (running) void follow(id);
}

async function newSession(projectId) {
  try {
    const session = await api.createSession(projectId);
    state.collapsed.delete(projectId);
    await loadSessions();
    await selectSession(session.id);
    $("#input").focus();
  } catch (error) {
    toast(error);
  }
}

// Что делает панель проектов по щелчкам (sidebar.js).
const sidebarActions = {
  select: (id) => selectSession(id),
  create: (projectId) => newSession(projectId),
  configure: (project) => openProject(project),
  remove: (session) => deleteSession(session),
  rename: async (session, title) => {
    try {
      await api.renameSession(session.id, title);
      await loadSessions();
    } catch (error) {
      toast(error);
    }
    render();
  },
};

function renderSidebar() {
  renderSidebarTree(state, sidebarActions);
}

async function deleteSession(session) {
  if (!window.confirm(`Удалить сессию «${session.title}» вместе с историей?`)) return;
  try {
    await api.deleteSession(session.id);
    await loadSessions();
    if (session.id === state.current) await openFirst();
    else render();
  } catch (error) {
    toast(error);
  }
}

// Открыть сессию: запомненную, иначе самую свежую; проектов нет — приветствие с выбором папки.
async function openFirst() {
  const saved = recall("session");
  const target = state.sessions.find((session) => session.id === saved) ?? state.sessions[0];
  if (target) return selectSession(target.id);
  if (state.projects.length) return newSession(state.projects[0].id);
  state.current = null;
  const empty = el("div", "empty");
  const mascot = el("img");
  Object.assign(mascot, { src: "/ui/assets/meepo-mascot.png", alt: "" });
  const choose = el("button", "primary", "Выбрать папку проекта");
  choose.type = "button";
  choose.addEventListener("click", () => openProject());
  empty.append(mascot, el("strong", "", "Привет! Я Мипо."), "Выберите папку проекта: в ней я буду читать и править файлы.", choose);
  $("#messages").replaceChildren(empty);
  renderChrome();
  renderSidebar();
}

// ---------- ход ----------

async function send(event) {
  event?.preventDefault();
  const session = current();
  const input = $("#input");
  const content = input.value.trim();
  if (state.live) return stop();
  if (!session || !content) return;
  input.value = "";
  fitInput(input);
  state.messages.push({ id: "local", role: "user", content, created_at: new Date().toISOString() });
  render({ running: true });
  await run(session.id, (signal, onEvent) => api.send(session.id, content, signal, onEvent));
}

// Подключиться к ходу, который уже идёт на сервере (после перезагрузки страницы или смены сессии).
async function follow(sessionId) {
  await run(sessionId, (signal, onEvent) => api.follow(sessionId, 0, signal, onEvent));
}

async function run(sessionId, open) {
  detach();
  const controller = new AbortController();
  state.controller = controller;
  state.live = new Live($("#messages"));
  $("#composer-error").hidden = true;
  renderChrome();
  renderSidebar();
  scrollDown($("#messages"));
  try {
    await open(controller.signal, (event) => {
      if (state.current !== sessionId) return;
      if (event.type === "approval") {
        state.card = event.card;
        renderCards();
      } else if (["tool_result", "done", "error"].includes(event.type) && state.card) {
        state.card = null;
        renderCards();
      }
      if (event.type === "error") showError(event.message);
      else if (event.type !== "done") {
        const near = isNearBottom();
        state.live?.handle(event);
        if (near) scrollDown($("#messages"));
      }
    });
  } catch (error) {
    if (controller.signal.aborted) return;
    showError(error.message);
  }
  if (state.controller !== controller) return;
  detach();
  await loadSessions();
  if (state.current === sessionId) {
    state.messages = await api.messages(sessionId);
    render();
    scrollDown($("#messages"));
  }
}

function isNearBottom() {
  const box = $("#messages");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
}

// Перестать читать поток в этой вкладке; ход на сервере это не останавливает.
function detach() {
  state.controller?.abort();
  state.controller = null;
  state.live?.finish();
  state.live = null;
}

async function stop() {
  const session = current();
  if (!session) return;
  try {
    await api.stop(session.id);
  } catch (error) {
    toast(error);
  }
}

async function decide(decision) {
  try {
    await api.decide(state.current, decision);
    state.card = null;
    renderCards();
  } catch (error) {
    toast(error);
    state.messages = await api.messages(state.current);
    await loadSessions();
    state.card = current()?.pending ?? null;
    renderCards();
  }
}

function showError(message) {
  const node = $("#composer-error");
  node.textContent = `Ошибка: ${message}`;
  node.hidden = false;
}

// ---------- отрисовка ----------

function render({ running = Boolean(state.live) } = {}) {
  const box = $("#messages");
  const live = state.live?.node;
  renderMessages(box, state.messages, { running });
  if (live) box.append(live);
  renderChrome();
  renderSidebar();
  renderCards();
}

function renderCards() {
  renderCard($("#cards"), state.card, decide);
}

function renderChrome() {
  const session = current();
  const project = projectOf(session);
  $("#project-chip").textContent = project?.name ?? "";
  $("#project-chip").hidden = !project;
  $("#session-title").textContent = session?.title ?? "—";
  const busy = Boolean(state.live);
  const send = $("#send");
  send.textContent = busy ? "■" : "↵";
  send.title = busy ? "Остановить (Esc)" : "Отправить";
  send.disabled = !session;
  $("#stop-hint").hidden = !busy;
  renderUsage(session);
  $("#input").disabled = !session;
}

// Заполненность окна модели по последнему запросу: «контекст ≈3,2k из 8k».
function renderUsage(session) {
  const node = $("#usage");
  const used = session?.context_tokens;
  const size = session?.context_window;
  if (!used || !size) {
    node.replaceChildren();
    return;
  }
  const share = used / size;
  const text = el("span", `meter-text${share > 0.9 ? " full" : share > 0.7 ? " warn" : ""}`, `≈${compact(used)} из ${compact(size)}`);
  node.replaceChildren(el("span", "usage-label", "контекст"), text);
  node.title = `Последний запрос занял ≈${used} токенов из окна модели ${size}. Не влезает — старые результаты `
    + "инструментов заменяются пометкой, затем выпадают старые сообщения.";
}

async function showModel() {
  try {
    const settings = await api.settings();
    $("#model-name").textContent = settings.active_model || "модель не выбрана";
  } catch (error) {
    toast(error);
  }
}

function showView(view) {
  for (const button of document.querySelectorAll(".tabs button")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  for (const node of document.querySelectorAll("main.view")) node.hidden = node.id !== `view-${view}`;
  remember("view", view);
  if (view === "wiki") {
    void showWiki(toast);
    void refreshWikiChat();
  }
  if (view === "skills") void showSkills(toast);
  if (view === "memory") void showMemory(toast);
  if (view === "settings") void showSettings(toast, (saved) => { $("#model-name").textContent = saved.active_model || "модель не выбрана"; });
}

function showTheme() {
  $("#theme-toggle").textContent = document.documentElement.dataset.theme === "dark" ? "☀" : "☾";
}

function bind() {
  $("#composer").addEventListener("submit", send);
  $("#input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!state.live) void send();
    }
  });
  $("#input").addEventListener("input", () => fitInput($("#input")));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.live && !$("#view-chat").hidden && !document.querySelector("dialog[open]")) void stop();
  });
  for (const button of document.querySelectorAll(".tabs button")) button.addEventListener("click", () => showView(button.dataset.view));
  initMemory(toast, current);
  initWikiPage(toast, () => showView("wiki"));
  $("#new-project").addEventListener("click", () => openProject());
  $("#project-chip").addEventListener("click", () => openProject(projectOf(current())));
  initProjects({
    toast,
    saved: async (project, created) => {
      if (created) return newSession(project.id);
      await loadSessions();
      render();
    },
    deleted: async () => {
      await loadSessions();
      if (!current()) await openFirst();
      else render();
    },
  });
  $("#sessions-toggle").addEventListener("click", () => { $("#sidebar").hidden = !$("#sidebar").hidden; });
  $("#theme-toggle").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    remember("app-theme", theme);
    showTheme();
  });
}

async function start() {
  bind();
  showTheme();
  void showModel();
  try {
    // После перезагрузки страница открывается на том же разделе: запоминаем его до того, как откроется сессия.
    const view = recall("view");
    await loadSessions();
    await openFirst();
    if (view && view !== "chat" && document.querySelector(`#view-${view}`)) showView(view);
  } catch (error) {
    toast(error);
  }
}

void start();
