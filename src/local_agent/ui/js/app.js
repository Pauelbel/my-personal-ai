// Точка входа интерфейса: проекты и сессии, чат со стримингом, карточки решений, задачи саб-агентов, разделы.
import { api } from "./api.js";
import { StreamingMessage, el, renderMessages, scrollDown } from "./chat.js";
import { renderCards, renderDock } from "./panel.js";
import { initFiles, openPath, showFiles } from "./files.js";
import { reloadCapabilities, saveMemory, showCapabilities, showMemory, showWiki } from "./pages.js";
import { initWikiChat, LINT_PROMPT, refreshWikiChat, sendWiki } from "./wikichat.js";
import { makeResizable } from "./resize.js";
import { renderAgentBar, renderTeam } from "./team.js";
import { editLabel, guide, rowAction, treeRow } from "./tree.js";

const $ = (selector) => document.querySelector(selector);
const POLL_MS = 30000;
// События приходят пачками (каждое сообщение, каждая запись сессии): перечитываем после короткой паузы.
const EVENT_DELAY_MS = 150;
const SEP = "\u0000";
const RUNNING = new Set(["queued", "running"]);
// Узкий экран: панель проектов выезжает поверх чата и закрывается после выбора сессии.
const narrow = window.matchMedia("(max-width: 900px)");

const state = {
  sessions: [],
  projects: [],
  current: null,
  messages: [],
  view: "chat",
  // Сессия, в которой сейчас идёт поток ответа, и чем его остановить.
  streaming: null,
  controller: null,
  bubble: null,
  // Сессии, по карточкам которых решение уже отправлено и ход продолжается в фоне.
  resuming: new Set(),
  // Команда открытой сессии (основной агент и саб-агенты) и последний неудачный ход для «Повторить».
  team: null,
  failed: null,
  seenUpdate: null,
  folder: null,
  editingProject: null,
  // Очередь сообщений открытой сессии: уходят на сервере после ответа.
  queue: [],
  // Карточки решений и задачи саб-агентов из последнего опроса; offline — сервер не ответил на опрос.
  attention: { cards: [], tasks: [] },
  offline: false,
  // Какую версию каждой сессии пользователь уже видел: более свежие помечаются «новое».
  seen: recallJson("seen", {}),
  collapsed: new Set(recallJson("collapsed-projects", [])),
};

// Части, которые перерисовываются по опросу, обновляются только при изменении данных:
// иначе каждые три секунды пропадали бы фокус, раскрытые блоки и текст, набранный в карточке.
const drawn = new Map();

function changed(part, data) {
  const signature = JSON.stringify(data);
  if (drawn.get(part) === signature) return false;
  drawn.set(part, signature);
  return true;
}

// ---------- общее ----------

function toast(error) {
  const node = $("#toast");
  node.textContent = error?.message || String(error);
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 6000);
}

const current = () => state.sessions.find((session) => session.id === state.current) || null;
const projectOf = (session) => state.projects.find((project) => project.id === session?.project_id) ?? null;

function remember(key, value) {
  try { localStorage.setItem(key, value); } catch { /* без хранилища просто не запомним */ }
}

function recall(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function recallJson(key, fallback) {
  try { return JSON.parse(recall(key)) ?? fallback; } catch { return fallback; }
}

// ---------- сессии и проекты ----------

async function loadSessions() {
  [state.sessions, state.projects] = await Promise.all([api.sessions(), api.projects()]);
  state.sessions.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  markSeen();
}

// Сессию, которую видим впервые, считаем прочитанной: «новое» — только про ответы, пришедшие после этого.
function markSeen() {
  const seen = {};
  for (const session of state.sessions.filter((item) => !item.hidden)) {
    seen[session.id] = session.id === state.current || !state.seen[session.id] ? session.updated_at : state.seen[session.id];
  }
  if (JSON.stringify(seen) === JSON.stringify(state.seen)) return;
  state.seen = seen;
  remember("seen", JSON.stringify(seen));
}

async function selectSession(id) {
  if (state.streaming && state.streaming !== id) detach();
  state.current = id;
  state.queue = [];
  renderQueue();
  remember("session", id);
  state.messages = [];
  state.seenUpdate = current()?.updated_at ?? null;
  markSeen();
  closeOverlay();
  showView("chat");
  render();
  await loadMessages();
  void syncTurn();
  void refreshPanel();
  void refreshTeam();
}

async function loadMessages() {
  const id = state.current;
  if (!id) return;
  try {
    const messages = await api.messages(id);
    if (state.current !== id) return;
    state.messages = messages;
    renderChat();
  } catch (error) {
    toast(error);
  }
}

// Сессия живёт в проекте: по умолчанию — в проекте открытой сессии или в последнем изменённом,
// а пока проектов нет — вместо сессии открывается выбор папки проекта. Возвращает сессию или null.
async function newSession(projectId = current()?.project_id ?? latestProject()?.id) {
  if (!projectId) {
    showWelcome();
    openProject();
    return null;
  }
  try {
    const session = await api.createSession(projectId);
    await loadSessions();
    await selectSession(session.id);
    $("#input").focus();
    return session;
  } catch (error) {
    toast(error);
    return null;
  }
}

function latestProject() {
  return [...state.projects].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0] ?? null;
}

// Первое открытие: проектов нет — приветствие с выбором папки вместо пустого чата.
function showWelcome() {
  state.current = null;
  state.messages = [];
  state.team = null;
  $("#agent-bar").replaceChildren();
  showView("chat");
  const empty = el("div", "empty");
  const mascot = el("img");
  mascot.src = "/ui/assets/meepo-mascot.png";
  mascot.alt = "";
  const choose = el("button", "primary", "Выбрать папку проекта");
  choose.type = "button";
  choose.addEventListener("click", () => openProject());
  empty.append(mascot, el("strong", "", "Привет! Я Meepo."),
    "Выберите папку проекта: в ней я буду читать и править файлы. Разговоры хранятся по проектам.", choose);
  $("#messages").replaceChildren(empty);
  render();
}

// Lint вики — сообщение в чат вики: он живёт вне проектов, ход и карточка правок видны на вкладке «Вики».
function lintWiki() {
  showView("wiki");
  void sendWiki(LINT_PROMPT);
}

// Ход в сессии ждёт решения по карточке, идёт сейчас или в ней работают саб-агенты.
function sessionStatus(session) {
  const { cards, tasks } = state.attention;
  if (session.pending || cards.some((card) => card.owner_id === session.id)) return "waiting";
  if (state.streaming === session.id || state.resuming.has(session.id) || state.attention.running?.includes(session.id)
    || tasks.some((task) => task.owner_id === session.id && RUNNING.has(task.status))) return "running";
  return "idle";
}

// Дерево «проект → сессии». Разговоры саб-агентов открываются с холста и из полосы агентов, здесь — только основные.
function renderSidebar() {
  const sessions = state.sessions.filter((session) => !session.hidden);
  // Открыт разговор с саб-агентом: в дереве отмечена сессия, которая ему поручила.
  const marked = current()?.parent_id ?? state.current;
  const tree = state.projects.map((project) => ({
    id: project.id,
    name: project.name,
    workspace: project.workspace,
    open: !state.collapsed.has(project.id),
    sessions: sessions.filter((session) => session.project_id === project.id).map((session) => ({
      id: session.id,
      title: session.title,
      status: sessionStatus(session),
      fresh: session.id !== state.current && session.updated_at > (state.seen[session.id] ?? session.updated_at),
      current: session.id === marked,
    })),
  }));
  // Пока название правят, дерево не перерисовывается: поле ввода пропало бы.
  if ($("#session-list .trow-input")) {
    drawn.delete("sidebar");
    return;
  }
  if (!changed("sidebar", tree)) return;
  $("#session-list").replaceChildren(...(tree.length
    ? tree.flatMap(projectNode)
    : [treeRow({ label: "проектов пока нет", mark: "" })]));
}

// Проект — «◆ имя», его сессии — ветки «├─ ○ название». ● — идёт ход или работают саб-агенты, «ждёт» — нужна карточка.
function projectNode(item) {
  const head = treeRow({
    label: item.name, group: true, open: item.open, title: `${item.workspace}\nДвойной щелчок — переименовать`,
    // Только число сессий у свёрнутого проекта: место — под название, папка видна в подсказке.
    note: !item.open && item.sessions.length ? String(item.sessions.length) : "",
    onClick: () => toggleProject(item.id),
    extra: [
      rowAction("＋", "Новая сессия в проекте", () => newSession(item.id)),
      rowAction("⚙", "Настройки проекта", () => openProject(state.projects.find((project) => project.id === item.id))),
    ],
  });
  head.dataset.project = item.id;
  head.classList.toggle("current", item.sessions.some((session) => session.current));
  if (!item.open) {
    const busy = ["waiting", "running"].find((status) => item.sessions.some((session) => session.status === status));
    if (busy) head.querySelector(".trow-mark").classList.add(busy);
    return [head];
  }
  if (!item.sessions.length) return [head, treeRow({ prefix: guide([], true), label: "сессий пока нет", mark: "" })];
  return [head, ...item.sessions.map((session, index) => sessionNode(session, index === item.sessions.length - 1))];
}

function sessionNode(item, last) {
  const active = item.status !== "idle";
  const row = treeRow({
    prefix: guide([], last), label: item.title, title: `${item.title}\nДвойной щелчок — переименовать`, current: item.current,
    mark: active ? "●" : "○", state: active ? item.status : "",
    note: item.status === "waiting" ? "ждёт" : item.fresh ? "новое" : "", noteClass: item.status === "waiting" ? "waiting" : "",
    onClick: () => selectSession(item.id),
    extra: [
      rowAction("✎", "Переименовать", () => renameSession(row, item)),
      rowAction("×", "Удалить сессию", () => {
        const session = state.sessions.find((candidate) => candidate.id === item.id);
        if (session) void deleteSession(session);
      }),
    ],
  });
  row.dataset.session = item.id;
  return row;
}

function renameSession(row, item) {
  editLabel(row, item.title, async (title) => {
    try {
      const updated = await api.renameSession(item.id, title);
      state.sessions = state.sessions.map((session) => session.id === updated.id ? updated : session);
      // Переименование — не новый ответ: метку «новое» оно не ставит.
      state.seen[updated.id] = updated.updated_at;
      remember("seen", JSON.stringify(state.seen));
    } catch (error) {
      toast(error);
    }
    render();
  });
}

function renameProject(row, id) {
  const project = state.projects.find((item) => item.id === id);
  if (!project) return;
  editLabel(row, project.name, async (name) => {
    try {
      const updated = await api.updateProject(project.id, name, project.workspace, project.mode);
      state.projects = state.projects.map((item) => item.id === updated.id ? updated : item);
    } catch (error) {
      toast(error);
    }
    render();
  });
}

function toggleProject(id) {
  if (state.collapsed.has(id)) state.collapsed.delete(id);
  else state.collapsed.add(id);
  remember("collapsed-projects", JSON.stringify([...state.collapsed]));
  renderSidebar();
}

async function deleteSession(session) {
  if (!window.confirm(`Удалить сессию «${session.title}» вместе с историей?`)) return;
  try {
    await api.deleteSession(session.id);
    await loadSessions();
    if (session.id === state.current) {
      const next = state.sessions.find((item) => !item.hidden);
      if (next) await selectSession(next.id);
      else await newSession();
    }
    render();
  } catch (error) {
    toast(error);
  }
}

// На широком экране панель открыта, пока её не скроют (запоминается); на узком — выезжает по кнопке.
function setSidebar(open) {
  $("#sidebar").hidden = !open;
  $("#sessions-toggle").setAttribute("aria-expanded", String(open));
  if (!narrow.matches) remember("sidebar", open ? "open" : "closed");
}

function closeOverlay() {
  if (narrow.matches) setSidebar(false);
}

// ---------- проект и выбор папки ----------

function openProject(project = null) {
  state.editingProject = project;
  // Название, совпадающее с именем папки, — название по умолчанию: при смене папки оно меняется вслед за ней.
  state.autoName = project && project.name === baseName(project.workspace) ? project.name : null;
  $("#project-dialog-title").textContent = project ? "Настройки проекта" : "Новый проект";
  $("#project-name").value = project?.name ?? "";
  $("#project-path").value = project?.workspace ?? "";
  $("#project-mode").value = project?.mode ?? "full";
  $("#project-error").textContent = "";
  $("#project-delete").hidden = !project;
  closeOverlay();
  $("#project-dialog").showModal();
  void browse(project?.workspace ?? null);
}

async function browse(path) {
  try {
    const listing = await api.folders(path);
    state.folder = listing;
    $("#folder-current").textContent = listing.path ?? "Корень";
    $("#folder-up").disabled = !listing.path;
    $("#folder-list").replaceChildren(...listing.folders.map((folder) => {
      const item = el("li");
      const button = el("button", "", `📁 ${folder.name}`);
      button.type = "button";
      button.addEventListener("click", () => {
        $("#project-path").value = folder.path;
        // Название по умолчанию — имя папки; своё, введённое вручную, не трогаем.
        const input = $("#project-name");
        if (!input.value.trim() || input.value === state.autoName) input.value = state.autoName = baseName(folder.path);
        void browse(folder.path);
      });
      item.append(button);
      return item;
    }));
  } catch (error) {
    $("#project-error").textContent = error.message;
  }
}

const baseName = (path) => path.split(/[\\/]/).filter(Boolean).pop() ?? path;

async function saveProject(event) {
  event.preventDefault();
  const name = $("#project-name").value.trim();
  const workspace = $("#project-path").value.trim();
  try {
    if (state.editingProject) {
      await api.updateProject(state.editingProject.id, name, workspace, $("#project-mode").value);
      $("#project-dialog").close();
      await loadSessions();
    } else {
      const project = await api.createProject(name, workspace);
      if ($("#project-mode").value !== "full") await api.updateProject(project.id, name, workspace, $("#project-mode").value);
      $("#project-dialog").close();
      await newSession(project.id);
    }
    render();
  } catch (error) {
    $("#project-error").textContent = error.message;
  }
}

async function deleteProject() {
  const project = state.editingProject;
  if (!project || !window.confirm(`Удалить проект «${project.name}» вместе с его сессиями и их историей? Папка на диске останется.`)) return;
  try {
    await api.deleteProject(project.id);
    $("#project-dialog").close();
    await loadSessions();
    if (!current()) {
      const next = state.sessions.find((item) => !item.hidden);
      if (next) await selectSession(next.id);
      else await newSession();
    }
    render();
  } catch (error) {
    $("#project-error").textContent = error.message;
  }
}

// ---------- модель ----------

async function loadModels() {
  const select = $("#model-select");
  const options = [];
  try {
    for (const provider of await api.providers()) {
      try {
        const list = await api.models(provider.id);
        state.defaultModel ??= list.default_model || list.models[0];
        for (const model of list.models) options.push([`${provider.id}${SEP}${model}`, model, provider.name]);
      } catch {
        // Сервер модели выключен: остальные провайдеры всё равно доступны.
      }
    }
  } catch (error) {
    toast(error);
  }
  select.replaceChildren(...options.map(([value, model, provider]) => {
    const option = new Option(model, value);
    option.title = provider;
    return option;
  }));
  // Сервер модели недоступен: не знаем, какие модели есть, и не пугаем предупреждением о модели сессии.
  state.modelsUnknown = !options.length;
  if (!options.length) select.append(new Option("Сервер модели недоступен", ""));
}

function showModel() {
  const session = current();
  const select = $("#model-select");
  if (!session) return;
  const value = `${session.provider}${SEP}${session.model}`;
  if (session.model && ![...select.options].some((option) => option.value === value)) {
    // Модели сессии нет на сервере (например, DEFAULT_MODEL от другого сервера): показываем это, а не молчим.
    select.append(new Option(`${session.model} — нет на сервере`, value));
  }
  if (session.model) select.value = value;
  else {
    // У сессии модели ещё нет: предлагаем модель по умолчанию, она сохранится при первой отправке.
    const fallback = [...select.options].find((option) => option.value.endsWith(`${SEP}${state.defaultModel}`));
    if (fallback) select.value = fallback.value;
  }
}

async function changeModel() {
  const session = current();
  const [provider, model] = $("#model-select").value.split(SEP);
  if (!session || !model) return;
  try {
    const updated = await api.configure(session.id, provider, model);
    state.sessions = state.sessions.map((item) => item.id === updated.id ? updated : item);
    render();
  } catch (error) {
    toast(error);
  }
}

// ---------- ход ----------

// Enter: без хода — отправить, во время хода — поставить сообщение в очередь сессии.
async function send() {
  const input = $("#input");
  const content = input.value.trim();
  if (content === "/lint") {
    // Команда, а не сообщение: проверка вики идёт отдельно от разговора, на вкладке «Вики».
    input.value = "";
    autosize();
    lintWiki();
    return;
  }
  const session = current();
  if (!content || !session) return;
  if (state.streaming === session.id) {
    try {
      await api.enqueue(session.id, content);
      input.value = "";
      autosize();
      await syncTurn();
    } catch (error) {
      toast(error);
    }
    return;
  }
  if (!session.model) {
    const [provider, model] = $("#model-select").value.split(SEP);
    if (!provider || !model) return toast(new Error("Выберите модель: сервер модели должен быть запущен"));
    await changeModel();
  }
  state.failed = null;
  await run(session.id, (signal, onEvent) => api.send(session.id, content, signal, onEvent), () => { input.value = ""; autosize(); }, content);
}

// ■ и Esc: ход останавливает сервер, поток сам закончится событием stopped.
async function stop() {
  if (!state.streaming) return;
  try {
    await api.stop(state.streaming);
  } catch (error) {
    toast(error);
  }
}

async function decide(card, decision) {
  if (card.session_id === state.current && !state.streaming) {
    await run(card.session_id, (signal, onEvent) => api.resume(card.session_id, decision, signal, onEvent));
    return;
  }
  // Карточка саб-агента или другой сессии: ход продолжается в фоне, итог придёт в панель и чат.
  state.resuming.add(card.session_id);
  void refreshPanel();
  try {
    await api.resume(card.session_id, decision, undefined, () => {});
  } catch (error) {
    toast(error);
  } finally {
    state.resuming.delete(card.session_id);
    await refreshAll();
  }
}

async function retry() {
  const failed = state.failed;
  if (!failed || state.streaming) return;
  state.failed = null;
  await run(failed.sessionId, (signal, onEvent) => api.send(failed.sessionId, failed.content, signal, onEvent, true), () => {}, failed.content);
}

// Ход открытой сессии идёт на сервере (из очереди, из другой вкладки, до перезагрузки): подключаемся к нему с начала.
async function syncTurn() {
  const id = state.current;
  if (!id) return;
  try {
    const turn = await api.turn(id);
    if (state.current !== id) return;
    state.queue = turn.queue;
    renderQueue();
    if (turn.running && !state.streaming) void run(id, (signal, onEvent) => api.follow(id, 0, signal, onEvent));
  } catch {
    // Сервер перезапускается: следующее событие или опрос подключат снова.
  }
}

// Поток открытой сессии перестаём читать, когда открывают другую: сам ход на сервере идёт дальше.
function detach() {
  const controller = state.controller;
  state.bubble?.finish();
  Object.assign(state, { bubble: null, streaming: null, controller: null });
  controller?.abort();
}

// content — текст хода: если ход сорвётся, его можно повторить кнопкой.
async function run(sessionId, start, onSaved = () => {}, content = null) {
  const controller = new AbortController();
  state.streaming = sessionId;
  state.controller = controller;
  state.bubble?.finish();
  state.bubble = new StreamingMessage($("#messages"));
  render();
  // При подключении к идущему ходу события повторяются с начала: уже загруженные сообщения не дублируем.
  const push = (message) => {
    if (state.current !== sessionId || state.messages.some((item) => item.id === message.id)) return;
    state.messages = [...state.messages, message];
  };
  try {
    await start(controller.signal, (event) => {
      const bubble = state.bubble;
      if (event.type === "user_message") {
        onSaved();
        push(event.message);
        redrawKeepingBubble();
      } else if (event.type === "delta") bubble.append(event.text);
      else if (event.type === "reasoning") bubble.addReasoning(event.text);
      else if (event.type === "log") bubble.addLog(event.text);
      else if (event.type === "tool_calls") {
        push(event.message);
        bubble.reset("Выполняется инструмент");
        redrawKeepingBubble();
      } else if (event.type === "tool_result") {
        push(event.message);
        bubble.setStatus("Модель думает");
        redrawKeepingBubble();
      } else if (event.type === "done") push(event.message);
      else if (event.type === "interrupt") void refreshPanel();
      else if (event.type === "error") throw new Error(event.message);
    });
  } catch (error) {
    if (error.name !== "AbortError") {
      toast(error);
      if (content) state.failed = { sessionId, content, reason: error.message };
    }
  } finally {
    // Пока этот поток заканчивался, могли подключиться к другому: его состояние не трогаем.
    if (state.controller === controller) {
      state.bubble?.finish();
      state.bubble = null;
      state.streaming = null;
      state.controller = null;
    }
    await refreshAll();
    if (state.current === sessionId && !state.streaming) {
      await loadMessages();
      await syncTurn();
    }
  }
}

// Очередь открытой сессии над полем ввода. Пока ход не идёт (остановлен, ждёт карточку, сорвался),
// очередь сама не уходит: первое сообщение можно отправить кнопкой.
function renderQueue() {
  const root = $("#queue");
  const items = state.queue ?? [];
  root.hidden = !items.length;
  root.replaceChildren(...items.map((item, index) => {
    const row = el("div", "queue-item");
    row.append(el("span", "queue-mark", "⋯"), el("span", "queue-text", item.content));
    if (index === 0 && !state.streaming) {
      const now = el("button", "queue-action", "отправить");
      now.type = "button";
      now.title = "Отправить сейчас";
      now.addEventListener("click", async () => {
        try {
          await api.dequeue(state.current, item.id);
          $("#input").value = item.content;
          await send();
        } catch (error) {
          toast(error);
        }
      });
      row.append(now);
    }
    const remove = el("button", "queue-action", "×");
    remove.type = "button";
    remove.title = "Убрать из очереди";
    remove.addEventListener("click", async () => {
      try {
        await api.dequeue(state.current, item.id);
        await syncTurn();
      } catch (error) {
        toast(error);
      }
    });
    row.append(remove);
    return row;
  }));
}

function redrawKeepingBubble() {
  renderChat();
  state.bubble?.attach();
  scrollDown($("#messages"));
}

// ---------- отрисовка ----------

function render() {
  const session = current();
  const project = projectOf(session);
  const agent = session?.parent_id ? state.team?.nodes.find((node) => node.session_id === session.id) : null;
  $("#session-title").textContent = session ? (session.parent_id ? `${agent?.name ?? "Саб-агент"} · ${session.title}` : session.title) : "—";
  $("#project-chip").textContent = project?.name ?? "—";
  $("#project-chip").title = project ? `Настройки проекта · ${project.workspace}` : "";
  $("#offline").hidden = !state.offline;
  const failed = state.failed && state.failed.sessionId === state.current && !state.streaming;
  $("#composer-retry").hidden = !failed;
  $("#retry-reason").textContent = failed ? `Ответ не получен: ${state.failed.reason}` : "";
  const streaming = Boolean(state.streaming);
  $("#send").textContent = streaming ? "■" : "↵";
  $("#send").title = streaming ? "Остановить ответ (Esc)" : "Отправить (Enter)";
  $("#stop-hint").hidden = !streaming;
  $("#input").placeholder = session || streaming ? placeholder(streaming) : "Сначала выберите папку проекта";
  $("#input").disabled = !session && !streaming;
  $("#send").disabled = !session && !streaming;
  renderQueue();
  const waiting = Boolean(session?.pending) && !streaming;
  const missing = Boolean(session?.model) && !state.modelsUnknown
    && ![...$("#model-select").options].some((option) => option.value === `${session.provider}${SEP}${session.model}` && !option.text.endsWith("нет на сервере"));
  const note = $("#composer-note");
  note.hidden = !waiting && !missing;
  note.textContent = waiting ? "Ход ждёт решения в панели «Требует внимания». Сообщение отсюда станет ответом на карточку."
    : missing ? `Модели «${session.model}» нет на сервере — выберите модель под полем ввода.` : "";
  const mode = $("#mode-badge");
  mode.hidden = !project;
  mode.textContent = project?.mode === "read" ? "только чтение" : "полные права";
  mode.classList.toggle("read", project?.mode === "read");
  renderUsage(session);
  showModel();
  renderSidebar();
}

// Заполненность окна модели последним запросом и скорость генерации.
function renderUsage(session) {
  const usage = $("#usage");
  if (!session?.context_tokens || !session.context_window) {
    usage.replaceChildren();
    usage.title = "";
    return;
  }
  const share = Math.min(session.context_tokens / session.context_window, 1);
  // Шкала из шести делений, как в терминале: хотя бы одно закрашено, пока контекст не пуст.
  const cells = Math.max(Math.round(share * 6), 1);
  const meter = el("span", `meter-text${share >= 0.85 ? " full" : share >= 0.6 ? " warn" : ""}`, "▰".repeat(cells) + "▱".repeat(6 - cells));
  usage.title = `Последний запрос занял ${session.context_tokens} из ${session.context_window} токенов окна модели`;
  usage.replaceChildren(el("span", "", "контекст"), meter, el("span", "", `${Math.round(share * 100)}%`));
  if (session.tokens_per_second) usage.append(el("span", "speed", `· ${Math.round(session.tokens_per_second)} ток/с`));
}

function renderChat() {
  const container = $("#messages");
  renderMessages(container, state.messages);
  scrollDown(container);
}

async function refreshPanel() {
  let data;
  try {
    data = await api.attention(state.current);
  } catch {
    // Сервер перезапускается: панель обновится на следующем опросе.
    return;
  }
  data.cards = data.cards.filter((card) => !state.resuming.has(card.session_id));
  state.attention = data;
  renderAttention();
  renderSidebar();
}

// Панель внимания видна, только когда есть карточки; задачи саб-агентов открытой сессии — над полем ввода.
function renderAttention() {
  const { cards, tasks } = state.attention;
  const busy = Boolean(state.streaming);
  $("#side").hidden = !cards.length;
  $("#cards-count").textContent = cards.length ? `· ${cards.length}` : "";
  if (changed("cards", [cards, busy])) {
    renderCards($("#cards"), cards, { onDecision: decide, onOpen: (id) => selectSession(id), readFile: readCardFile, busy });
  }
  const owner = current()?.parent_id ?? state.current;
  const own = tasks.filter((task) => task.owner_id === owner);
  $("#dock").hidden = !own.length;
  if (own.length && changed("dock", own)) {
    renderDock({ count: $("#dock-count"), summary: $("#dock-summary"), list: $("#tasks") }, own, {
      onOpen: (id) => selectSession(id),
      onMore: () => showView("team"),
    });
  }
}

// Текущий текст файла в проекте сессии карточки; null — файла нет или он прочитан не целиком.
async function readCardFile(card, path) {
  const session = state.sessions.find((item) => item.id === card.session_id);
  if (!session?.project_id) return null;
  try {
    const file = await api.file(session.project_id, path);
    return file.truncated ? null : file.content;
  } catch {
    return null;
  }
}

function setDock(collapsed) {
  $("#dock").classList.toggle("collapsed", collapsed);
  $("#dock-toggle").setAttribute("aria-expanded", String(!collapsed));
  remember("dock", collapsed ? "collapsed" : "open");
}

async function refreshAll() {
  try {
    await loadSessions();
  } catch (error) {
    toast(error);
  }
  render();
  await Promise.all([refreshPanel(), refreshTeam()]);
}

// Сервер сообщает об изменениях потоком событий; на каждое (пачкой) перечитываем сессии, карточки и команду.
// Редкий опрос — на случай, если поток событий оборвался и ещё не переподключился.
function listen() {
  const source = new EventSource("/api/events");
  let timer = null;
  source.onopen = () => {
    if (state.offline) {
      state.offline = false;
      void refreshFromServer();
    }
  };
  source.onerror = () => {
    state.offline = true;
    render();
  };
  source.onmessage = () => {
    clearTimeout(timer);
    timer = setTimeout(() => void refreshFromServer(), EVENT_DELAY_MS);
  };
}

async function refreshFromServer() {
  try {
    await loadSessions();
    state.offline = false;
    const session = current();
    if (session && session.updated_at !== state.seenUpdate && !state.streaming) {
      state.seenUpdate = session.updated_at;
      if (state.view === "chat") await loadMessages();
    }
  } catch {
    // Сервер недоступен: показываем это в шапке, поток событий переподключится сам.
    state.offline = true;
  }
  render();
  await Promise.all([refreshPanel(), refreshTeam(), state.streaming ? null : syncTurn(), refreshWikiChat()]);
}

async function poll() {
  await refreshFromServer();
  setTimeout(poll, POLL_MS);
}

// ---------- команда ----------

async function refreshTeam() {
  if (!state.current) return;
  try {
    state.team = await api.team(state.current);
  } catch {
    return;
  }
  renderAgentBar($("#agent-bar"), state.team, state.current, openAgent);
  if (state.view === "team") renderTeam($("#team"), state.team, state.current, { onOpen: openAgent, onLink: link, onUnlink: unlink });
  render();
}

async function link(from, to) {
  try {
    await api.link(from, to);
    await refreshTeam();
  } catch (error) {
    toast(error);
  }
}

async function unlink(from, to) {
  try {
    await api.unlink(from, to);
    await refreshTeam();
  } catch (error) {
    toast(error);
  }
}

async function openAgent(node) {
  try {
    let sessionId = node.session_id;
    if (!sessionId) {
      sessionId = (await api.teamChat(state.current, node.id)).id;
      await loadSessions();
    }
    await selectSession(sessionId);
  } catch (error) {
    toast(error);
  }
}

// ---------- разделы ----------

const VIEWS = ["chat", "team", "files", "wiki", "capabilities", "memory"];

function showView(view) {
  state.view = view;
  // После перезагрузки страница откроется на этой же вкладке.
  remember("view", view);
  for (const button of document.querySelectorAll(".tabs button")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  for (const name of VIEWS) $(`#view-${name}`).hidden = name !== view;
  if (view === "team") void refreshTeam();
  if (view === "files") void showFiles(projectOf(current()));
  if (view === "wiki") {
    void showWiki(toast);
    void refreshWikiChat(true);
  }
  if (view === "capabilities") void showCapabilities(toast);
  if (view === "memory") void showMemory(toast);
  if (view === "chat") renderChat();
}

// Путь из ответа: относительный к папке проекта или абсолютный внутри неё.
async function openInFiles(path, line) {
  const project = projectOf(current());
  if (!project) return;
  let relative = path.replace(/^\.\//, "");
  if (relative.startsWith(`${project.workspace}/`)) relative = relative.slice(project.workspace.length + 1);
  showView("files");
  await openPath(relative, line);
}

function autosize() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * 0.4)}px`;
}

// ---------- запуск ----------

function bind() {
  $("#sessions-toggle").addEventListener("click", () => setSidebar($("#sidebar").hidden));
  $("#new-session").addEventListener("click", () => newSession());
  $("#session-list").addEventListener("dblclick", (event) => {
    const row = event.target.closest(".trow");
    if (!row || event.target.closest(".trow-action, .trow-input")) return;
    if (row.dataset.project) renameProject(row, row.dataset.project);
    else if (row.dataset.session) renameSession(row, { id: row.dataset.session, title: row.querySelector(".trow-label")?.textContent ?? "" });
  });
  $("#new-project").addEventListener("click", () => openProject());
  $("#project-chip").addEventListener("click", () => openProject(projectOf(current())));
  $("#mode-badge").addEventListener("click", () => openProject(projectOf(current())));
  $("#project-form").addEventListener("submit", saveProject);
  // Путь, введённый руками, тоже даёт название по умолчанию.
  $("#project-path").addEventListener("input", () => {
    const input = $("#project-name");
    const path = $("#project-path").value.trim();
    if (path && (!input.value.trim() || input.value === state.autoName)) input.value = state.autoName = baseName(path);
  });
  $("#project-cancel").addEventListener("click", () => $("#project-dialog").close());
  $("#project-delete").addEventListener("click", deleteProject);
  $("#folder-up").addEventListener("click", () => browse(state.folder?.parent ?? null));
  $("#source-close").addEventListener("click", () => $("#source-dialog").close());
  $("#model-select").addEventListener("change", changeModel);
  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    void (state.streaming ? stop() : send());
  });
  $("#input").addEventListener("input", autosize);
  $("#input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void send();
    }
  });
  $("#messages").addEventListener("click", (event) => {
    const link = event.target.closest(".path-link");
    if (!link) return;
    // Путь в заголовке шага не должен заодно раскрывать сам шаг.
    event.preventDefault();
    void openInFiles(link.dataset.path, Number(link.dataset.line) || null);
  });
  $("#dock-toggle").addEventListener("click", () => setDock(!$("#dock").classList.contains("collapsed")));
  for (const button of document.querySelectorAll(".tabs button")) button.addEventListener("click", () => showView(button.dataset.view));
  let searchTimer = null;
  $("#wiki-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => showWiki(toast), 250);
  });
  $("#wiki-lint").addEventListener("click", lintWiki);
  initWikiChat({ toast, model: () => $("#model-select").value.split(SEP) });
  makeResizable($("#wiki-chat-resizer"), { grid: $("#view-wiki"), variables: ["--wiki-chat-w"], min: 280 });
  makeResizable($("#wiki-list-resizer"), {
    grid: $("#wiki-split"),
    variables: ["--wiki-list-w", "--wiki-graph-w"],
    pick: () => ($("#wiki-split").classList.contains("graph-mode") ? "--wiki-graph-w" : "--wiki-list-w"),
    min: 160,
  });
  $("#capabilities-reload").addEventListener("click", () => reloadCapabilities(toast));
  $("#retry").addEventListener("click", retry);
  initFiles(toast);
  $("#memory-save").addEventListener("click", () => saveMemory(toast));
  $("#memory-update").addEventListener("click", async () => {
    if (!state.current) return;
    try {
      const result = await api.updateMemory(state.current);
      $("#memory-status").textContent = `Разобрано сообщений: ${result.processed_messages}, изменений: ${result.applied_operations}`;
      await showMemory(toast);
    } catch (error) {
      toast(error);
    }
  });
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    remember("app-theme", next);
    showTheme();
  });
  // Esc останавливает ответ, а на узком экране сначала закрывает выехавшую панель.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
    if (narrow.matches && !$("#sidebar").hidden) setSidebar(false);
    else if (state.streaming) void stop();
  });
  // Путь события берём на момент щелчка: кнопка внутри панели к этому времени могла уйти из DOM при перерисовке дерева.
  document.addEventListener("click", (event) => {
    const inside = event.composedPath().some((node) => node instanceof Element && node.matches("#sidebar, #sessions-toggle, dialog"));
    if (narrow.matches && !$("#sidebar").hidden && !inside) setSidebar(false);
  });
  narrow.addEventListener("change", fitScreen);
}

// Иконка темы показывает, во что переключит кнопка.
function showTheme() {
  $("#theme-toggle").textContent = document.documentElement.dataset.theme === "dark" ? "☀" : "☾";
}

function fitScreen() {
  setSidebar(!narrow.matches && recall("sidebar") !== "closed");
  $("#input").placeholder = placeholder(Boolean(state.streaming));
}

function placeholder(streaming) {
  if (streaming) return narrow.matches ? "В очередь" : "Ход идёт — Enter поставит сообщение в очередь";
  return "Сообщение агенту";
}

async function start() {
  bind();
  fitScreen();
  showTheme();
  setDock(recall("dock") === "collapsed");
  try {
    await Promise.all([loadSessions(), loadModels()]);
  } catch (error) {
    toast(error);
  }
  // Вкладку читаем до выбора сессии: выбор сессии сам переключает на чат.
  const savedView = recall("view");
  const saved = recall("session");
  const target = state.sessions.find((session) => session.id === saved) ?? state.sessions.find((session) => !session.hidden);
  if (target) {
    await selectSession(target.id);
    if (VIEWS.includes(savedView) && savedView !== "chat") showView(savedView);
  } else await newSession();
  listen();
  setTimeout(poll, POLL_MS);
}

void start();
