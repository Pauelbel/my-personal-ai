// Точка входа интерфейса: проекты и сессии, чат со стримингом, карточки решений, разделы.
import { initial, tone } from "./marks.js";
import { fitInput, plural, recall, remember } from "./util.js";
import { api } from "./api.js";
import { imageInput } from "./attach.js";
import { STEPS_TOOLS, StreamingMessage, el, parse, renderMessages, scrollDown } from "./chat.js";
import { renderCards } from "./panel.js";
import { initFiles, openPath, revealChange, showFiles } from "./files.js";
import { initFolder, reloadCapabilities, saveMemory, showCapabilities, showMemory, showWiki, wikiKeys } from "./pages.js";
import { initWikiChat, LINT_PROMPT, refreshWikiChat, sendWiki } from "./wikichat.js";
import { makeResizable } from "./resize.js";
import { initProviders } from "./providers.js";
import { editLabel, rowAction, treeRow } from "./tree.js";

const $ = (selector) => document.querySelector(selector);
const POLL_MS = 30000;
// События приходят пачками (каждое сообщение, каждая запись сессии): перечитываем после короткой паузы.
const EVENT_DELAY_MS = 150;
const SEP = "\u0000";
// Узкий экран: панель проектов выезжает поверх чата и закрывается после выбора сессии.
const narrow = window.matchMedia("(max-width: 900px)");

// Картинки в поле ввода основного чата (attach.js).
let images = null;

const state = {
  sessions: [],
  projects: [],
  // Сессия, история которой загружена в state.messages: только по ней можно судить, что сессия пуста.
  loaded: null,
  current: null,
  messages: [],
  view: "chat",
  // Сессия, в которой сейчас идёт поток ответа, и чем его остановить.
  streaming: null,
  controller: null,
  bubble: null,
  // Сессии, по карточкам которых решение уже отправлено и ход продолжается в фоне.
  resuming: new Set(),
  // Последний неудачный ход — для «Повторить».
  failed: null,
  seenUpdate: null,
  folder: null,
  editingProject: null,
  // Очередь сообщений открытой сессии: уходят на сервере после ответа.
  queue: [],
  // Карточки решений из последнего опроса; offline — сервер не ответил на опрос.
  attention: { cards: [] },
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
  for (const session of state.sessions) {
    seen[session.id] = session.id === state.current || !state.seen[session.id] ? session.updated_at : state.seen[session.id];
  }
  if (JSON.stringify(seen) === JSON.stringify(state.seen)) return;
  state.seen = seen;
  remember("seen", JSON.stringify(seen));
}

async function selectSession(id) {
  // Из пустой сессии ушли — она не нужна: в списке не копятся «Новые сессии» без единого сообщения.
  const left = current();
  const discard = id !== state.current && isBlank(left) ? left.id : null;
  if (state.streaming && state.streaming !== id) detach();
  state.current = id;
  state.queue = [];
  renderQueue();
  remember("session", id);
  state.messages = [];
  // История новой сессии ещё не пришла: пока она не загружена, пустой эту сессию не считаем.
  state.loaded = null;
  state.seenUpdate = current()?.updated_at ?? null;
  markSeen();
  closeOverlay();
  showView("chat");
  render();
  if (discard) void discardSession(discard);
  await loadMessages();
  void syncTurn();
  void refreshPanel();
}

// Пустая сессия проекта: история загружена и в ней ничего нет, ход не идёт. Пока история не пришла,
// сессию считаем непустой.
function isBlank(session) {
  if (!session) return false;
  if (state.loaded !== session.id || state.messages.length || session.pending) return false;
  return state.streaming !== session.id && !state.resuming.has(session.id);
}

async function discardSession(id) {
  try {
    await api.discardSession(id);
    await loadSessions();
    render();
  } catch {
    // Сервер счёл сессию непустой или не ответил — она остаётся в списке.
  }
}

async function loadMessages() {
  const id = state.current;
  if (!id) return;
  try {
    const messages = await api.messages(id);
    if (state.current !== id) return;
    // Сессию только что открыли — её разговор показываем с конца; иначе позиция чтения сохраняется.
    const opened = state.loaded !== id;
    state.messages = messages;
    state.loaded = id;
    renderChat({ follow: opened });
  } catch (error) {
    toast(error);
  }
}

// Сессия живёт в проекте: по умолчанию — в проекте открытой сессии или в последнем изменённом,
// а пока проектов нет — вместо сессии открывается выбор папки проекта. Возвращает сессию или null.
async function newSession(projectId = current()?.project_id ?? latestProject()?.id) {
  // Открытая сессия этого проекта ещё пуста — она и есть новая.
  if (isBlank(current()) && current().project_id === projectId) {
    $("#input").focus();
    return current();
  }
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

// Ход в сессии ждёт решения по карточке или идёт сейчас.
function sessionStatus(session) {
  if (session.pending || state.attention.cards.some((card) => card.owner_id === session.id)) return "waiting";
  if (state.streaming === session.id || state.resuming.has(session.id) || state.attention.running?.includes(session.id)) return "running";
  return "idle";
}

// Сайдбар: дерево «проект → сессии».
function renderSidebar() {
  const sessions = state.sessions;
  const marked = state.current;
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
      current: session.id === state.current,
      marked: session.id === marked,
    })),
  }));
  // Пока название правят, дерево не перерисовывается: поле ввода пропало бы.
  if ($("#session-list .trow-input")) {
    drawn.delete("sidebar");
    return;
  }
  if (!changed("sidebar", { tree, open: state.current })) return;
  $("#session-list").replaceChildren(...(tree.length
    ? tree.flatMap(projectNode)
    : [treeRow({ label: "проектов пока нет", mark: "" })]));
}

// Проект — цветной значок с буквой и имя, его сессии — строки под ним. Точка у сессии — идёт ход,
// «ждёт» — нужна карточка; у свёрнутого проекта то же показывает обводка значка.
function projectNode(item) {
  const head = treeRow({
    label: item.name, group: true, open: item.open, mark: initial(item.name),
    title: `${item.workspace}\nДвойной щелчок — переименовать`,
    // Только число сессий у свёрнутого проекта: место — под название, папка видна в подсказке.
    note: !item.open && item.sessions.length ? String(item.sessions.length) : "",
    // В проекте без сессий сворачивать нечего: щелчок открывает чат с основным агентом и файлы проекта.
    onClick: () => (item.sessions.length ? toggleProject(item.id) : void openEmptyProject(item.id)),
    extra: [
      rowAction("＋", "Новая сессия в проекте", () => newSession(item.id)),
      rowAction("⚙", "Настройки проекта", () => openProject(state.projects.find((project) => project.id === item.id))),
    ],
  });
  head.dataset.project = item.id;
  head.querySelector(".trow-mark").style.setProperty("--tone", tone(item.id));
  head.classList.toggle("current", item.sessions.some((session) => session.marked));
  if (!item.open) {
    const busy = ["waiting", "running"].find((status) => item.sessions.some((session) => session.status === status));
    if (busy) head.querySelector(".trow-mark").classList.add(busy);
    return [head];
  }
  return [head, ...item.sessions.map(sessionNode)];
}


function sessionNode(item) {
  const active = item.status !== "idle";
  const row = treeRow({
    label: item.title, title: `${item.title}\nДвойной щелчок — переименовать`, current: item.current,
    mark: "", state: active ? item.status : "",
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
  // Открыт разговор агента этой сессии: сама сессия не выделена плашкой, но отмечена.
  row.classList.toggle("marked", item.marked && !item.current);
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

async function openEmptyProject(id) {
  if (state.collapsed.delete(id)) remember("collapsed-projects", JSON.stringify([...state.collapsed]));
  await newSession(id);
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
      const next = state.sessions[0];
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
      const next = state.sessions[0];
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
  select.replaceChildren(new Option("Загрузка моделей…", ""));
  select.disabled = true;
  state.defaultModel = undefined;
  const options = [];
  try {
    const providers = await api.providers();
    const results = await Promise.allSettled(providers.map((p) => api.models(p.id)));
    for (let i = 0; i < providers.length; i++) {
      const result = results[i];
      if (result.status === "rejected") continue;
      const list = result.value;
      state.defaultModel ??= list.default_model || list.models[0];
      for (const model of list.models) options.push([`${providers[i].id}${SEP}${model}`, model, providers[i].name]);
    }
  } catch (error) {
    toast(error);
  }
  select.disabled = false;
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
  if ((!content && !images.count) || !session) return;
  const attachments = await images.names();
  if (!content && !attachments.length) return;
  if (state.streaming === session.id) {
    try {
      await api.enqueue(session.id, content, attachments);
      input.value = "";
      images.clear();
      autosize();
      await syncTurn();
    } catch (error) {
      toast(error);
    }
    return;
  }
  if (!(await ensureModel(session))) return;
  state.failed = null;
  const sent = () => {
    input.value = "";
    images.clear();
    autosize();
  };
  await run(session.id, (signal, onEvent) => api.send(session.id, content, signal, onEvent, false, attachments), sent, content, attachments);
}

// У сессии ещё нет модели: берём выбранную над полем ввода, она сохранится с первым ходом.
async function ensureModel(session) {
  if (session.model) return true;
  const [provider, model] = $("#model-select").value.split(SEP);
  if (!provider || !model) {
    toast(new Error("Выберите модель: сервер модели должен быть запущен"));
    return false;
  }
  await changeModel();
  return true;
}

// «▶ Запустить по шагам» на карточке скилла: шаги идут в папке проекта — в открытой сессии проекта или в новой.
// Сессия, которая ждёт решения по карточке или занята ходом, не подходит: запуск стал бы ответом или встал в очередь.
async function runSkill(id) {
  let session = current();
  const usable = session && !session.pending && state.streaming !== session.id;
  if (!usable) {
    const project = projectOf(session) ?? latestProject();
    if (!project) return toast(new Error("Сначала создайте проект: шаги скилла работают в его папке"));
    await newSession(project.id);
    session = current();
    if (!session) return;
  }
  showView("chat");
  if (!(await ensureModel(session))) return;
  state.failed = null;
  await run(session.id, (signal, onEvent) => api.runSkill(session.id, id, signal, onEvent));
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
  // Карточка другой сессии: ход продолжается в фоне, итог придёт в панель и чат.
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
  await run(
    failed.sessionId, (signal, onEvent) => api.send(failed.sessionId, failed.content, signal, onEvent, true, failed.attachments),
    () => {}, failed.content, failed.attachments,
  );
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
async function run(sessionId, start, onSaved = () => {}, content = null, attachments = []) {
  const controller = new AbortController();
  state.streaming = sessionId;
  state.controller = controller;
  state.bubble?.finish();
  state.bubble = new StreamingMessage($("#messages"));
  // Новая реплика ещё не пришла: строка этого хода не должна забрать себе предыдущий, уже законченный ход.
  state.bubble.fresh = Boolean(content || attachments.length);
  render();
  // Продолжение после карточки или подключение к идущему ходу: шаги, что уже были, сразу уходят в строку хода.
  redrawKeepingBubble();
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
        bubble.fresh = false;
        redrawKeepingBubble(true);
      } else if (event.type === "delta") bubble.append(event.text);
      else if (event.type === "reasoning") bubble.addReasoning(event.text);
      else if (event.type === "log") bubble.addLog(event.text);
      else if (event.type === "steps") bubble.setSteps(event.run);
      // Модель зациклилась или ответила пустотой — сервер повторяет запрос, оборванный текст убираем.
      else if (event.type === "retry") bubble.reset("retry");
      else if (event.type === "tool_calls") {
        push(event.message);
        // Промежуточная реплика без вызовов (модель попросили продолжить) — модель снова думает.
        const names = (event.message.tool_calls ?? []).map((call) => call.name);
        bubble.reset(names.length ? "tool" : "thinking", names);
        redrawKeepingBubble();
      } else if (event.type === "tool_result") {
        push(event.message);
        if (state.current === sessionId) revealToolChange(event.message);
        bubble.setStatus("thinking");
        redrawKeepingBubble();
      } else if (event.type === "done") push(event.message);
      else if (event.type === "interrupt") void refreshPanel();
      else if (event.type === "error") throw new Error(event.message);
    });
  } catch (error) {
    if (error.name !== "AbortError") {
      // Причина уже видна над полем ввода рядом с «Повторить»: всплывающее сообщение её не дублирует.
      if (content || attachments.length) state.failed = { sessionId, content, attachments, reason: error.message };
      else toast(error);
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

// follow — показать конец разговора в любом случае: например, после своей отправленной реплики.
function redrawKeepingBubble(follow = false) {
  const container = $("#messages");
  const stick = follow || container.scrollHeight - container.scrollTop - container.clientHeight < 120;
  renderChat();
  state.bubble?.attach();
  if (stick) scrollDown(container);
}

// После перерисовки: был внизу — снова вниз, читал выше — та же позиция.
function keepScroll(container, { top, stick }) {
  if (stick) scrollDown(container);
  else container.scrollTop = top;
}

// ---------- отрисовка ----------

function render() {
  const session = current();
  const project = projectOf(session);
  $("#session-title").textContent = session?.title ?? "—";
  $("#project-chip").textContent = project?.name ?? "—";
  $("#project-chip").disabled = !project;
  // Пока сессия не выбрана, файлов нет — полоса с кнопкой «Файлы» не нужна.
  $("#chat-bar").hidden = !session;
  syncFilesPanel(project);
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
  // Что ход ждёт решения, видно по самой карточке над полем ввода: отдельная надпись не нужна.
  const missing = Boolean(session?.model) && !state.modelsUnknown
    && ![...$("#model-select").options].some((option) => option.value === `${session.provider}${SEP}${session.model}` && !option.text.endsWith("нет на сервере"));
  const note = $("#composer-note");
  note.hidden = !missing;
  note.textContent = missing ? `Модели «${session.model}» нет на сервере — выберите модель под полем ввода.` : "";
  const mode = $("#mode-badge");
  mode.hidden = !project;
  mode.textContent = project?.mode === "read" ? "только чтение" : "полные права";
  mode.classList.toggle("read", project?.mode === "read");
  renderUsage(session);
  // Этап плана задачи — в строке идущего хода: «этап 2/3: Баг-репорты». План от прошлой задачи не показываем,
  // пока агент в этом ходе его не тронул (новая реплика может быть о другом).
  if (state.bubble && streaming && state.streaming === session?.id) {
    const plan = JSON.stringify(session.plan ?? []);
    state.bubble.planBefore ??= state.bubble.fresh ? plan : "";
    state.bubble.setStage(plan !== state.bubble.planBefore ? stageOf(session.plan) : "");
  }
  showModel();
  renderSidebar();
}

function stageOf(plan) {
  const index = (plan ?? []).findIndex((step) => !step.done);
  return index < 0 ? "" : `этап ${index + 1}/${plan.length}: ${plan[index].text}`;
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
  usage.replaceChildren(el("span", "usage-label", "контекст"), meter, el("span", "", `${Math.round(share * 100)}%`));
  if (session.tokens_per_second) usage.append(el("span", "speed", `· ${Math.round(session.tokens_per_second)} ток/с`));
}

function renderChat({ follow = false } = {}) {
  const container = $("#messages");
  // Перерисовка на каждом шаге агента не дёргает прокрутку: читающего выше оставляем на месте.
  const scroll = { top: container.scrollTop, stick: follow || container.scrollHeight - container.scrollTop - container.clientHeight < 120 };
  const session = current();
  const own = Boolean(session) && state.streaming === session.id && Boolean(state.bubble);
  const fresh = own && state.bubble.fresh;
  renderMessages(container, state.messages, undefined, {
    // Идущий ход этой сессии — одной строкой печатающегося ответа; стоящий на карточке — «ждёт решения».
    // Пока новая реплика не пришла, последний ход в истории — прошлый, законченный: его не трогаем.
    live: own && !fresh ? state.bubble : null,
    waiting: Boolean(session?.pending),
    busy: !fresh && (own || Boolean(session && state.attention.running?.includes(session.id))),
  });
  keepScroll(container, scroll);
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

// Карточки открытой сессии — в чате над полем ввода; про карточки других сессий — одна строка-ссылка.
function renderAttention() {
  const { cards } = state.attention;
  const busy = Boolean(state.streaming);
  const owner = state.current;
  const here = cards.filter((card) => card.owner_id === owner);
  const elsewhere = cards.filter((card) => card.owner_id !== owner);
  $("#cards").hidden = !here.length;
  if (changed("cards", [here, busy])) {
    renderCards($("#cards"), here, { onDecision: decide, onOpen: (id) => selectSession(id), readFile: readCardFile, busy, inChat: true });
  }
  const note = $("#cards-elsewhere");
  note.hidden = !elsewhere.length;
  if (elsewhere.length) {
    const titles = [...new Set(elsewhere.map((card) => `«${card.session_title}»`))];
    note.textContent = `Ещё ${elsewhere.length} ${plural(elsewhere.length, "карточка ждёт", "карточки ждут", "карточек ждут")} решения: ${titles.join(", ")} →`;
    note.onclick = () => selectSession(elsewhere[0].owner_id);
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

async function refreshAll() {
  try {
    await loadSessions();
  } catch (error) {
    toast(error);
  }
  render();
  await refreshPanel();
}

// Сервер сообщает об изменениях потоком событий; на каждое (пачкой) перечитываем сессии и карточки.
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
  await Promise.all([refreshPanel(), state.streaming ? null : syncTurn(), refreshWikiChat()]);
}

async function poll() {
  await refreshFromServer();
  setTimeout(poll, POLL_MS);
}

// ---------- разделы ----------

const VIEWS = ["chat", "wiki", "capabilities", "memory"];

function showView(view) {
  state.view = view;
  // После перезагрузки страница откроется на этой же вкладке.
  remember("view", view);
  for (const button of document.querySelectorAll(".tabs button")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  for (const name of VIEWS) $(`#view-${name}`).hidden = name !== view;
  if (view === "wiki") {
    void showWiki(toast);
    void refreshWikiChat(true);
  }
  if (view === "capabilities") void showCapabilities(toast);
  if (view === "memory") void showMemory(toast);
  if (view === "chat") renderChat();
}

// Путь из ответа: относительный к папке проекта или абсолютный внутри неё. Открывается в панели файлов рядом.
async function openInFiles(path, line) {
  const project = projectOf(current());
  if (!project) return;
  setFilesPanel(true);
  await openPath(projectPath(project, path), line);
}

function projectPath(project, path) {
  const relative = path.replace(/^\.\//, "");
  return relative.startsWith(`${project.workspace}/`) ? relative.slice(project.workspace.length + 1) : relative;
}

// ---------- панель файлов ----------

// Файлы проекта — справа от чата. Скрыть можно кнопкой, это запоминается; у чатов вне проектов файлов нет.
let filesOpen = recall("files-panel") !== "closed";
let filesProject;

function setFilesPanel(open) {
  filesOpen = open;
  remember("files-panel", open ? "open" : "closed");
  syncFilesPanel(projectOf(current()));
}

function syncFilesPanel(project) {
  const shown = Boolean(project) && filesOpen;
  $("#view-chat").classList.toggle("files-hidden", !shown);
  $("#files-open").hidden = !project || filesOpen;
  // Дерево перечитывается, только когда панель открыли или сменился проект.
  const key = shown ? project.id : null;
  if (key === filesProject) return;
  filesProject = key;
  if (shown) void showFiles(project);
}

// Агент записал или поправил файл в открытой сессии — файл открывается в панели с подсвеченными изменениями.
function revealToolChange(message) {
  // Шаги скилла записали файлы — панель файлов показывает последний из них.
  const written = STEPS_TOOLS.has(message.tool_name)
    ? (message.meta?.steps?.steps ?? []).flatMap((step) => step.files ?? []) : [];
  if (written.length && projectOf(current())) {
    setFilesPanel(true);
    void revealChange(projectPath(projectOf(current()), written.at(-1)), null);
    return;
  }
  if (message.is_error || !["write_file", "edit_file"].includes(message.tool_name)) return;
  const project = projectOf(current());
  const call = state.messages.flatMap((item) => item.tool_calls ?? []).find((item) => item.id === message.tool_call_id);
  const args = call ? parse(call.arguments) : {};
  if (!project || typeof args.path !== "string") return;
  setFilesPanel(true);
  void revealChange(projectPath(project, cleanPath(args.path)), message.tool_name === "edit_file" ? args.new_text ?? null : null);
}

// Тот же разбор, что у файловых инструментов: «test_results.md`)» из Markdown задачи — это test_results.md.
function cleanPath(path) {
  let previous;
  while (path !== previous) {
    previous = path;
    path = path.trim().replace(/^[`'"«»“”]+|[`'"«»“”]+$/g, "").replace(/[,;:]+$/, "");
    const count = (char) => path.split(char).length - 1;
    const wrapper = (char) => char !== "" && "`'\"«»“”".includes(char);
    if (path.endsWith(")") && (wrapper(path.slice(-2, -1)) || count(")") > count("("))) path = path.slice(0, -1);
    if (path.startsWith("(") && (wrapper(path.slice(1, 2)) || count("(") > count(")"))) path = path.slice(1);
  }
  return path;
}

function autosize() {
  fitInput($("#input"));
}

// ---------- запуск ----------

function bind() {
  $("#sessions-toggle").addEventListener("click", () => setSidebar($("#sidebar").hidden));
  $("#session-list").addEventListener("dblclick", (event) => {
    const row = event.target.closest(".trow");
    if (!row || event.target.closest(".trow-action, .trow-input")) return;
    if (row.dataset.project) renameProject(row, row.dataset.project);
    else if (row.dataset.session) renameSession(row, { id: row.dataset.session, title: row.querySelector(".trow-label")?.textContent ?? "" });
  });
  $("#new-project").addEventListener("click", () => openProject());
  $("#project-chip").addEventListener("click", () => {
    const project = projectOf(current());
    if (project) openProject(project);
  });
  document.addEventListener("meepo:run-skill", (event) => void runSkill(event.detail.id));
  // Провайдеры поменялись — модели над полем ввода собираются заново.
  initProviders({ onChanged: async () => { await loadModels(); showModel(); } });
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
  initFolder();
  $("#model-select").addEventListener("change", changeModel);
  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    void (state.streaming ? stop() : send());
  });
  $("#input").addEventListener("input", autosize);
  images = imageInput({ box: $("#composer .composer-box"), input: $("#input"), strip: $("#attachments"), button: $("#attach"), toast });
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
  for (const button of document.querySelectorAll(".tabs button")) button.addEventListener("click", () => showView(button.dataset.view));
  let searchTimer = null;
  $("#wiki-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => showWiki(toast), 250);
  });
  $("#wiki-search").addEventListener("keydown", (event) => wikiKeys(event, toast));
  $("#wiki-list").addEventListener("keydown", (event) => wikiKeys(event, toast));
  $("#wiki-lint").addEventListener("click", lintWiki);
  initWikiChat({ toast, model: () => $("#model-select").value.split(SEP) });
  makeResizable($("#sidebar-resizer"), { grid: $(".layout"), variables: ["--sidebar"], min: 200, max: 480 });
  makeResizable($("#files-resizer"), { grid: $("#view-chat"), variables: ["--chat-w"], min: 360 });
  makeResizable($("#files-tree-resizer"), { grid: $("#files-split"), variables: ["--tree-w"], min: 140, max: 480 });
  $("#files-open").addEventListener("click", () => setFilesPanel(true));
  $("#files-close").addEventListener("click", () => setFilesPanel(false));
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
  // Ctrl+K — найти заметку вики из любого раздела. По коду клавиши: в русской раскладке это «Л».
  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.code !== "KeyK" || document.querySelector("dialog[open]")) return;
    event.preventDefault();
    if (state.view !== "wiki") showView("wiki");
    $("#wiki-search").focus();
    $("#wiki-search").select();
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
  if (streaming) return narrow.matches ? "В очередь" : "Ход идёт · Enter — в очередь";
  return "Сообщение агенту";
}

async function start() {
  bind();
  fitScreen();
  showTheme();
  try {
    await Promise.all([loadSessions(), loadModels()]);
  } catch (error) {
    toast(error);
  }
  // Вкладку читаем до выбора сессии: выбор сессии сам переключает на чат.
  const savedView = recall("view");
  const saved = recall("session");
  const target = state.sessions.find((session) => session.id === saved) ?? state.sessions[0];
  if (target) {
    await selectSession(target.id);
    if (VIEWS.includes(savedView) && savedView !== "chat") showView(savedView);
  } else await newSession();
  listen();
  setTimeout(poll, POLL_MS);
}

void start();
