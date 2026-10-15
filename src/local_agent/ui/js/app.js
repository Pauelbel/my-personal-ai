// Точка входа интерфейса: агенты, проекты и сессии, чат со стримингом, карточки решений, задачи саб-агентов, разделы.
import { initial, tone } from "./marks.js";
import { api } from "./api.js";
import { imageInput } from "./attach.js";
import { StreamingMessage, el, parse, renderMessages, scrollDown, setAgentNames } from "./chat.js";
import { renderCards, renderDock } from "./panel.js";
import { initFiles, openPath, revealChange, showFiles } from "./files.js";
import { reloadCapabilities, saveMemory, showCapabilities, showMemory, showWiki, wikiKeys } from "./pages.js";
import { initWikiChat, LINT_PROMPT, refreshWikiChat, sendWiki } from "./wikichat.js";
import { makeResizable } from "./resize.js";
import { initAgentForm, openAgentForm, openMainForm } from "./agents.js";
import { initProviders } from "./providers.js";
import { renderTeam } from "./team.js";
import { editLabel, rowAction, treeRow } from "./tree.js";

const $ = (selector) => document.querySelector(selector);
const POLL_MS = 30000;
// События приходят пачками (каждое сообщение, каждая запись сессии): перечитываем после короткой паузы.
const EVENT_DELAY_MS = 150;
const SEP = "\u0000";
const RUNNING = new Set(["queued", "running"]);
// Узкий экран: панель проектов выезжает поверх чата и закрывается после выбора сессии.
const narrow = window.matchMedia("(max-width: 900px)");

// Картинки в поле ввода основного чата (attach.js).
let images = null;

const state = {
  sessions: [],
  projects: [],
  // Саб-агенты для блока «Агенты»: с личным чатом (chat_id) и тем, чем каждый занят (status).
  agents: [],
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
  [state.sessions, state.projects, state.agents] = await Promise.all([api.sessions(), api.projects(), api.agents()]);
  state.sessions.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  setAgentNames(state.agents);
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
  // Из пустой сессии ушли — она не нужна: в списке не копятся «Новые сессии» без единого сообщения.
  const left = current();
  const discard = id !== state.current && isBlank(left) ? left.id : null;
  if (state.streaming && state.streaming !== id) detach();
  state.current = id;
  state.queue = [];
  renderQueue();
  remember("session", id);
  state.messages = [];
  state.teamLog = [];
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
  void refreshTeam();
}

// Пустая сессия проекта: история загружена и в ней ничего нет, ход не идёт, агентов в ней нет.
// Пока что-то из этого неизвестно (история или команда ещё не пришли), сессию считаем непустой.
function isBlank(session) {
  if (!session || session.kind !== "chat" || session.parent_id || session.hidden) return false;
  if (state.loaded !== session.id || state.messages.length || session.pending) return false;
  if (state.streaming === session.id || state.resuming.has(session.id)) return false;
  return state.team?.owner_id === session.id && !state.team.nodes.some((node) => node.id !== "main" && node.member);
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
    state.messages = messages;
    state.loaded = id;
    renderChat();
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
  state.team = null;
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

// Сайдбар: агенты, под ними дерево «проект → сессии». Агенты сессии — кружками над чатом.
function renderSidebar() {
  const sessions = state.sessions.filter((session) => !session.hidden);
  // Открыт разговор с агентом в сессии: в дереве отмечена сессия, которой он принадлежит.
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
      current: session.id === state.current,
      marked: session.id === marked,
    })),
  }));
  const agents = state.agents.map(({ id, name, description, status, builtin }) => ({ id, name, description, status, builtin }));
  // Пока название правят, дерево не перерисовывается: поле ввода пропало бы.
  if ($("#session-list .trow-input")) {
    drawn.delete("sidebar");
    return;
  }
  if (!changed("sidebar", { tree, agents, open: state.current })) return;
  $("#agent-list").replaceChildren(...(agents.length
    ? agents.map(agentNode)
    : [treeRow({ label: "агентов пока нет", mark: "" })]));
  $("#session-list").replaceChildren(...(tree.length
    ? tree.flatMap(projectNode)
    : [treeRow({ label: "проектов пока нет", mark: "" })]));
}

const BUSY = new Set(["running", "queued", "waiting"]);

// Агент в блоке «Агенты»: щелчок — личный чат, ✎ — форма, × — в архив. Обводка значка — агент где-то работает.
// Основной закреплён первым: значок — маскот, щелчок — общий чат вне проектов, ✎ — свои правила, удалить нельзя.
function agentNode(agent) {
  const personal = current()?.kind === "agent" && current().agent_id === agent.id;
  const row = treeRow({
    label: agent.name, title: agent.description, current: personal,
    mark: agent.builtin ? "" : initial(agent.name), state: BUSY.has(agent.status) ? agent.status : "",
    onClick: () => openPersonal(agent.id),
    extra: agent.builtin
      ? [rowAction("✎", "Дополнительные инструкции основного агента", () => editAgent(agent.id))]
      : [
        rowAction("✎", "Изменить агента", () => editAgent(agent.id)),
        rowAction("×", "Убрать агента в архив", () => archiveAgent(agent)),
      ],
  });
  row.classList.add("agent");
  const mark = row.querySelector(".trow-mark");
  if (agent.builtin) {
    row.classList.add("main");
    const mascot = el("img");
    mascot.src = "/ui/assets/meepo-mascot.png";
    mascot.alt = "";
    mark.append(mascot);
  } else mark.style.setProperty("--tone", tone(agent.id));
  return row;
}

// Проект — цветной значок с буквой и имя, его сессии — строки под ним. Точка у сессии — идёт ход или работают
// саб-агенты, «ждёт» — нужна карточка; у свёрнутого проекта то же показывает обводка значка.
function projectNode(item) {
  const head = treeRow({
    label: item.name, group: true, open: item.open, mark: initial(item.name),
    title: `${item.workspace}\nДвойной щелчок — переименовать`,
    // Только число сессий у свёрнутого проекта: место — под название, папка видна в подсказке.
    note: !item.open && item.sessions.length ? String(item.sessions.length) : "",
    onClick: () => toggleProject(item.id),
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

// Агенты сессии — кружки над чатом: Основной (маскот) и добавленные саб-агенты, щелчок переключает разговор,
// обводка — агент работает или ждёт решения. ＋ — добавить агента. Рядом — имя того, с кем разговор сейчас.
function renderAgentCircles(session) {
  const owner = session?.parent_id ?? session?.id;
  const team = session?.kind === "chat" && state.team?.owner_id === owner ? state.team : null;
  // Общий и личные чаты вне проектов: ни агентов сессии, ни файлов — полоса не нужна.
  $("#chat-bar").hidden = session?.kind !== "chat";
  if (!team) $("#chat-activity").hidden = true;
  $("#agent-circles").hidden = !team;
  $("#agent-current").textContent = team && session.parent_id ? agentName(session) : team ? "Основной" : "";
  const nodes = team ? team.nodes.filter((node) => node.id === "main" || node.member) : [];
  const data = nodes.map(({ id, name, description, session_id, status, task, from }) => ({ id, name, description, session_id, status, task, from }));
  renderActivity(data);
  if (!changed("circles", [owner, state.current, data])) return;
  $("#agent-circles").replaceChildren(...data.map((node) => agentCircle(node, owner)), ...(team ? [addCircle(owner)] : []));
}

// Под кружками — кто из агентов сейчас занят: кто ему поручил, что делает и в каком он состоянии.
// Щелчок по строке открывает его разговор.
const ACTIVITY = { running: "работает", queued: "в очереди", waiting: "ждёт вашего решения" };

function renderActivity(nodes) {
  // По цепочке: сначала поручения основного агента, затем то, что агенты поручили друг другу.
  const busy = nodes.filter((node) => node.id !== "main" && BUSY.has(node.status))
    .sort((a, b) => Number(Boolean(a.from) && a.from !== "Основной") - Number(Boolean(b.from) && b.from !== "Основной"));
  const root = $("#chat-activity");
  root.hidden = !busy.length;
  if (!changed("activity", busy)) return;
  root.replaceChildren(...busy.map((node) => {
    const row = el("button", `activity-row ${node.status}`);
    row.type = "button";
    row.append(
      el("span", `status-dot ${node.status}`),
      el("span", "activity-route", `${node.from || "Основной"} → ${node.name}`),
      el("span", "activity-task", node.task ?? ""),
      el("span", "activity-state", ACTIVITY[node.status]),
    );
    row.title = "Открыть разговор агента";
    row.addEventListener("click", () => openAgent(node));
    return row;
  }));
}

function agentCircle(node, owner) {
  const main = node.id === "main";
  const circle = el("button", `agent-circle${main ? " main" : ""}`);
  circle.type = "button";
  const open = main ? state.current === owner : node.session_id === state.current;
  circle.setAttribute("aria-pressed", String(open));
  const status = BUSY.has(node.status) ? node.status : "";
  if (status) circle.classList.add(status);
  circle.title = `${main ? "Основной" : node.name}${node.task ? ` — ${node.task}` : ""}${status === "waiting" ? " · ждёт решения" : ""}`;
  circle.setAttribute("aria-label", main ? "Основной" : node.name);
  if (main) {
    const mascot = el("img");
    mascot.src = "/ui/assets/meepo-mascot.png";
    mascot.alt = "";
    circle.append(mascot);
  } else {
    circle.textContent = initial(node.name);
    circle.style.setProperty("--tone", tone(node.id));
  }
  circle.addEventListener("click", () => (main ? selectSession(owner) : openAgent(node)));
  return circle;
}

function addCircle(owner) {
  const add = el("button", "agent-circle add", "＋");
  add.type = "button";
  add.title = "Добавить агента в сессию";
  add.setAttribute("aria-label", "Добавить агента в сессию");
  add.addEventListener("click", (event) => agentMenu(event.currentTarget, owner));
  return add;
}

// Меню «добавить агента в сессию» под кнопкой ＋ у сессии. Агент, который уже в ней, просто открывается.
function agentMenu(anchor, sessionId) {
  closeMenu();
  const menu = el("div", "menu");
  menu.id = "agent-menu";
  menu.setAttribute("role", "menu");
  // Основной агент и так ведёт каждую сессию: добавлять можно только саб-агентов.
  const agents = state.agents.filter((agent) => !agent.builtin);
  if (!agents.length) menu.append(el("p", "hint", "Саб-агентов пока нет: создайте его в блоке «Агенты»."));
  for (const agent of agents) {
    const item = el("button", "menu-item");
    item.type = "button";
    item.setAttribute("role", "menuitem");
    item.title = agent.description;
    const mark = el("span", "menu-mark", initial(agent.name));
    mark.style.setProperty("--tone", tone(agent.id));
    item.append(mark, el("span", "", agent.name));
    item.addEventListener("click", () => {
      closeMenu();
      void addMember(sessionId, agent.id);
    });
    menu.append(item);
  }
  document.body.append(menu);
  const box = anchor.getBoundingClientRect();
  menu.style.left = `${Math.min(box.left, window.innerWidth - menu.offsetWidth - 8)}px`;
  menu.style.top = `${Math.min(box.bottom + 4, window.innerHeight - menu.offsetHeight - 8)}px`;
  menu.querySelector("button")?.focus();
  // Закрывается щелчком мимо и Esc; щелчок, который меню открыл, до этого обработчика уже не дойдёт.
  setTimeout(() => document.addEventListener("click", closeMenu, { once: true }));
  menu.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      closeMenu();
      anchor.focus();
    }
  });
}

function closeMenu() {
  $("#agent-menu")?.remove();
}

async function addMember(sessionId, agentId) {
  try {
    const dialog = await api.teamChat(sessionId, agentId);
    await loadSessions();
    await selectSession(dialog.id);
  } catch (error) {
    toast(error);
  }
}

async function removeMember(ownerId, node) {
  if (!window.confirm(`Убрать агента «${node.name}» из сессии? Его разговор в ней удалится.`)) return;
  try {
    await api.removeAgent(ownerId, node.id);
    await loadSessions();
    if (!current()) await selectSession(ownerId);
    else await refreshTeam();
  } catch (error) {
    toast(error);
  }
}

// Личный чат с агентом вне проектов: создаётся при первом открытии с моделью, выбранной под полем ввода.
async function openPersonal(agentId) {
  const [provider, model] = $("#model-select").value.split(SEP);
  try {
    const session = await api.agentChat(agentId, provider || null, model || null);
    await loadSessions();
    await selectSession(session.id);
  } catch (error) {
    toast(error);
  }
}

function editAgent(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  if (agent?.builtin) openMainForm(agent, agentSaved);
  else if (agent) void openAgentForm(agent, agentSaved);
}

async function agentSaved() {
  await loadSessions();
  render();
  if (state.view === "capabilities") await showCapabilities(toast);
  if (state.view === "team") await showTeamReference();
}

async function archiveAgent(agent) {
  if (!window.confirm(`Убрать агента «${agent.name}» в архив? Файл останется, вернуть можно в «Возможности → Архив».`)) return;
  try {
    await api.archive("agent", agent.id);
    await loadSessions();
    // Открыт разговор с этим агентом: без агента он не работает, возвращаемся к обычной сессии.
    if (current()?.agent_id === agent.id) {
      const back = current().parent_id ?? state.sessions.find((item) => !item.hidden)?.id;
      if (back) await selectSession(back);
    }
    render();
  } catch (error) {
    toast(error);
  }
}

function agentName(session) {
  return state.agents.find((agent) => agent.id === session.agent_id)?.name ?? session.task?.agent_name ?? session.agent_id;
}

// «Очистить»: разговор с агентом начинается заново, агент остаётся там же.
async function clearDialog() {
  const session = current();
  if (!session || !window.confirm(`Очистить разговор с агентом «${agentName(session)}»? История удалится, агент останется.`)) return;
  try {
    await api.clearSession(session.id);
    await loadSessions();
    state.messages = [];
    renderChat();
    render();
  } catch (error) {
    toast(error);
  }
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
  showRoutes(project);
  closeOverlay();
  $("#project-dialog").showModal();
  void browse(project?.workspace ?? null);
}

// Запомненные маршруты проекта: «забыть» — и основной агент снова спросит, кто делает такие задачи.
function showRoutes(project) {
  const routes = Object.entries(project?.routes ?? {});
  $("#project-routes-block").hidden = !routes.length;
  const names = new Map(state.agents.map((agent) => [agent.id, agent.name]));
  $("#project-routes").replaceChildren(...routes.map(([kind, who]) => {
    const item = el("li");
    const forget = el("button", "", "Забыть");
    forget.type = "button";
    forget.addEventListener("click", async () => {
      try {
        const updated = await api.forgetRoute(project.id, kind);
        state.editingProject = updated;
        showRoutes(updated);
        await loadSessions();
      } catch (error) {
        $("#project-error").textContent = error.message;
      }
    });
    item.append(el("span", "", `${kind} → ${who === "main" ? "Основной сам" : names.get(who) ?? who}`), forget);
    return item;
  }));
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
  if (!session.model) {
    const [provider, model] = $("#model-select").value.split(SEP);
    if (!provider || !model) return toast(new Error("Выберите модель: сервер модели должен быть запущен"));
    await changeModel();
  }
  state.failed = null;
  const sent = () => {
    input.value = "";
    images.clear();
    autosize();
  };
  await run(session.id, (signal, onEvent) => api.send(session.id, content, signal, onEvent, false, attachments), sent, content, attachments);
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
      // Модель зациклилась или ответила пустотой — сервер повторяет запрос, оборванный текст убираем.
      else if (event.type === "retry") bubble.reset("Повторяю запрос");
      else if (event.type === "tool_calls") {
        push(event.message);
        bubble.reset("Выполняется инструмент");
        redrawKeepingBubble();
      } else if (event.type === "tool_result") {
        push(event.message);
        if (state.current === sessionId) revealToolChange(event.message);
        bubble.setStatus("Модель думает");
        redrawKeepingBubble();
      } else if (event.type === "done") push(event.message);
      else if (event.type === "interrupt") void refreshPanel();
      else if (event.type === "error") throw new Error(event.message);
    });
  } catch (error) {
    if (error.name !== "AbortError") {
      toast(error);
      if (content || attachments.length) state.failed = { sessionId, content, attachments, reason: error.message };
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
  const personal = session?.kind === "agent";
  const parent = session?.parent_id ? state.sessions.find((item) => item.id === session.parent_id) : null;
  $("#session-title").textContent = !session ? "—" : personal ? agentName(session)
    : session.parent_id ? `${parent?.title ?? "Сессия"} / ${agentName(session)}` : session.title;
  $("#project-chip").textContent = personal ? "Агенты" : project?.name ?? "—";
  $("#project-chip").disabled = !project;
  renderChatHead(session, parent);
  renderAgentCircles(session);
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
  const waiting = Boolean(session?.pending) && !streaming;
  const missing = Boolean(session?.model) && !state.modelsUnknown
    && ![...$("#model-select").options].some((option) => option.value === `${session.provider}${SEP}${session.model}` && !option.text.endsWith("нет на сервере"));
  const note = $("#composer-note");
  note.hidden = !waiting && !missing;
  note.textContent = waiting ? "Ход ждёт решения по карточке выше. Сообщение отсюда станет ответом на неё."
    : missing ? `Модели «${session.model}» нет на сервере — выберите модель под полем ввода.` : "";
  const mode = $("#mode-badge");
  mode.hidden = !project;
  mode.textContent = project?.mode === "read" ? "только чтение" : "полные права";
  mode.classList.toggle("read", project?.mode === "read");
  renderUsage(session);
  showModel();
  renderSidebar();
}

// Над чатом с агентом — с кем разговор и где он, «Очистить», а в сессии ещё «убрать из сессии».
function renderChatHead(session, parent) {
  const kind = session?.kind === "agent" ? "personal" : session?.parent_id ? "member" : null;
  $("#chat-head").hidden = !kind;
  if (!kind) return;
  const name = agentName(session);
  const general = kind === "personal" && session.agent_id === "main";
  $("#chat-head-title").textContent = general
    ? "Общий чат вне проектов: память о вас работает, файлов и поручений саб-агентам нет"
    : kind === "personal" ? `Личный чат с агентом «${name}» — вне проектов, без доступа к файлам`
    : `Разговор с агентом «${name}» в сессии «${parent?.title ?? ""}»`;
  $("#chat-remove").hidden = kind !== "member" || session.parent_id !== parent?.id || Boolean(parent?.parent_id);
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
  const session = current();
  // Общий чат — тот же основной агент: приветствие обычное.
  const agent = session && session.agent_id !== "main" && (session.kind === "agent" || session.parent_id) ? agentName(session) : null;
  // Строки ленты команды встают среди сообщений по времени — только в основной сессии, куда они загружены.
  const owner = session && session.kind === "chat" && !session.parent_id;
  const lines = owner ? (state.teamLog ?? []).map((item) => ({ ...item, role: "team", created_at: item.at })) : [];
  const timeline = lines.length ? [...state.messages, ...lines].sort((a, b) => a.created_at.localeCompare(b.created_at)) : state.messages;
  renderMessages(container, timeline, agent ? [`Разговор с агентом «${agent}»`, session.kind === "agent"
    ? "Здесь можно обсудить его роль и задать общий вопрос. Файлы проектов отсюда ему недоступны."
    : "Поручения основного агента и ваши сообщения идут сюда по очереди. «Очистить» начнёт разговор заново."] : undefined);
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

// Карточки открытой сессии и её агентов — в чате над полем ввода; про карточки других сессий — одна строка-ссылка.
// Задачи саб-агентов открытой сессии — в доке там же.
function renderAttention() {
  const { cards, tasks } = state.attention;
  const busy = Boolean(state.streaming);
  const owner = current()?.parent_id ?? state.current;
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
  const own = tasks.filter((task) => task.owner_id === owner);
  $("#dock").hidden = !own.length;
  if (own.length && changed("dock", own)) {
    renderDock({ count: $("#dock-count"), summary: $("#dock-summary"), list: $("#tasks") }, own, {
      onOpen: (id) => selectSession(id),
      onMore: () => showView("team"),
    });
  }
}

function plural(count, one, few, many) {
  const tens = count % 100;
  const units = count % 10;
  if (tens >= 11 && tens <= 14) return many;
  return units === 1 ? one : units >= 2 && units <= 4 ? few : many;
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
  // Личный чат с агентом вне проектов: команды сессии у него нет.
  if (current()?.kind === "agent") {
    state.team = null;
    render();
    return;
  }
  try {
    state.team = await api.team(state.current);
  } catch {
    return;
  }
  render();
  if (state.view === "team") void showTeamReference();
  await refreshTeamLog();
}

// Лента команды в основном чате: кто из саб-агентов кому что поручил или спросил и что ответил.
// Только в основной сессии: в разговоре самого агента его переписка и так видна.
async function refreshTeamLog() {
  const session = current();
  if (!session || session.kind !== "chat" || session.parent_id) return;
  let log;
  try {
    log = await api.teamLog(session.id);
  } catch {
    return;
  }
  if (state.current !== session.id) return;
  state.teamLog = log;
  if (!changed("team-log", [session.id, log])) return;
  if (state.streaming === session.id) redrawKeepingBubble();
  else renderChat();
}

// «Команда» — справочник: все агенты и кто к кому может обращаться; щелчок по карточке — настройки агента.
async function showTeamReference() {
  let data;
  try {
    data = await api.agentsTeam();
  } catch (error) {
    toast(error);
    return;
  }
  const status = new Map(state.agents.map((agent) => [agent.id, agent.status]));
  data.nodes = data.nodes.map((node) => ({ ...node, status: status.get(node.id) ?? "idle" }));
  renderTeam($("#team"), data, { onOpen: (node) => editAgent(node.id) });
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

const VIEWS = ["chat", "team", "wiki", "capabilities", "memory"];

function showView(view) {
  state.view = view;
  // После перезагрузки страница откроется на этой же вкладке.
  remember("view", view);
  for (const button of document.querySelectorAll(".tabs button")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  for (const name of VIEWS) $(`#view-${name}`).hidden = name !== view;
  if (view === "team") void showTeamReference();
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
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * 0.4)}px`;
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
  $("#new-agent").addEventListener("click", () => void openAgentForm(null, async (agent) => {
    await agentSaved();
    await openPersonal(agent.id);
  }));
  $("#chat-clear").addEventListener("click", clearDialog);
  $("#chat-remove").addEventListener("click", () => {
    const session = current();
    const node = state.team?.nodes.find((item) => item.session_id === session?.id);
    if (session?.parent_id) void removeMember(session.parent_id, node ?? { id: session.agent_id, name: agentName(session) });
  });
  initAgentForm();
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
  $("#source-close").addEventListener("click", () => $("#source-dialog").close());
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
    // Строка ленты команды: «открыть разговор» ведёт в разговор агента.
    const dialog = event.target.closest(".team-open");
    if (dialog) {
      event.preventDefault();
      void selectSession(dialog.dataset.session);
      return;
    }
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
