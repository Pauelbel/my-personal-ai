// Холст сессии: агенты, которым агент сессии может поручать работу, и связи «кто кому пишет».
// Карточка «вход» — сам агент сессии; пока от неё нет стрелок, сессия работает как обычный чат.
// Список агентов под чатом: агент сессии выбирается из всех агентов, а агенты холста идут отдельной
// группой — выбор открывает переписку с выбранным агентом.
import { showAgents } from "./agents-panel.js";
import { renderFiles } from "./files-panel.js";
import { agentsApi, catalogApi, sessionsApi } from "./api.js";
import { initResizer } from "./resizer.js";
import { loadSessions, selectSession } from "./sessions.js";
import { elements, render, selectedSession, showError, state } from "./state.js";

// Пункт списка «Добавить агента», который открывает создание нового агента; id агента двоеточия не содержит.
const NEW_AGENT = ":new";
// Префикс пункта «агент сессии» в списке под чатом: без него id агента совпал бы с id его карточки на холсте.
const SESSION_AGENT = "agent:";
// Карточка агента сессии: через неё сообщение пользователя входит в команду.
const ENTRY = "main";
const SVG = "http://www.w3.org/2000/svg";
const ZOOM_MIN = 0.4;
const ZOOM_MAX = 1.6;
const ZOOM_STEP = 0.1;
const GRID = 20;
// На какой доле стороны карточки лежат связи «вперёд» (вправо, вниз) и «назад»: встречные связи не сливаются.
const FORWARD_LEVEL = 0.38;
const BACKWARD_LEVEL = 0.62;
// Наружная нормаль каждой стороны: в эту сторону провод выходит из карточки.
const SIDES = {
  top: { x: 0, y: -1 }, right: { x: 1, y: 0 }, bottom: { x: 0, y: 1 }, left: { x: -1, y: 0 },
};
const SAVE_DELAY_MS = 400;
// Меньше карточку не сжать: заголовок с кнопками и хотя бы одна секция должны помещаться.
const CARD_MIN_WIDTH = 220;
const CARD_MIN_HEIGHT = 120;
// Длинные списки инструментов и навыков сворачиваются до стольких строк и кнопки «ещё N».
const VISIBLE_ROWS = 4;
const TOOL_GLYPHS = {
  list_files: "▦", read_file: "▤", search_files: "⌕", write_file: "✎", edit_file: "✎", git: "⎇", search_docs: "❡",
};

let agents = [];
// Холст открытой сессии и её id: правки сохраняются в эту сессию.
let graph = null;
let sessionId = null;
let openedKey = "";
let saveTimer = null;
const view = { x: 40, y: 40, zoom: 1 };
// Свёрнутые секции и раскрытые списки карточек: `${nodeId}:${section}`.
const collapsed = new Set();
const expanded = new Set();
// Ход команды: какой агент сейчас работает.
let runView = { activeNode: null, label: "" };

export function initSessionCanvas() {
  elements.teamAddAgent.addEventListener("change", () => {
    const value = elements.teamAddAgent.value;
    elements.teamAddAgent.value = "";
    if (value === NEW_AGENT) {
      void showAgents({ create: true });
      return;
    }
    const agent = agents.find((item) => item.id === value);
    if (agent) addNode(agent);
  });
  elements.agent.addEventListener("change", () => {
    const value = elements.agent.value;
    if (value.startsWith(SESSION_AGENT)) void chooseSessionAgent(value.slice(SESSION_AGENT.length));
    else void openAgentChat(value);
  });
  elements.zoomIn.addEventListener("click", () => zoomAtCenter(view.zoom + ZOOM_STEP));
  elements.zoomOut.addEventListener("click", () => zoomAtCenter(view.zoom - ZOOM_STEP));
  elements.zoomReset.addEventListener("click", () => {
    Object.assign(view, { x: 40, y: 40, zoom: 1 });
    applyView();
  });
  for (const [button, tab] of [[elements.tabCanvas, "canvas"], [elements.tabFiles, "files"]]) {
    // Щелчок по активной вкладке сворачивает панель, по другой — открывает её.
    button.addEventListener("click", () => {
      const hidden = elements.detail.classList.contains("canvas-collapsed");
      const collapse = tab === activeTab && !hidden;
      setTab(tab);
      setCollapsed(collapse);
      render();
    });
  }
  initResizer({
    handle: elements.canvasResizer, container: elements.detail, variable: "--chat-width",
    storageKey: "chat-width", defaultWidth: 560, min: 320, reserve: 320,
  });
  setCollapsed(loadCollapsed(), false);
  setTab(loadTab(), false);
  initPanAndZoom();
  initPromptDialog();
  void loadAgents();
}

// Агента создали или поменяли во вкладке «Агенты»: карточки и список для добавления перечитываются.
export async function loadAgents() {
  try {
    agents = await catalogApi.agents();
  } catch {
    agents = [];
  }
  fillSelect(elements.teamAddAgent, [
    ["", "Выберите…"], ...agents.filter((agent) => agent.id !== ownerSession()?.agent_id)
      .map((agent) => [agent.id, agent.name]), [NEW_AGENT, "＋ Новый агент…"],
  ]);
  drawGraph();
}

// Сессия, которой принадлежит холст: открытая сессия или хозяйка открытой переписки агента.
function ownerSession() {
  const session = selectedSession();
  return session?.parent_id ? state.sessions.find((item) => item.id === session.parent_id) : session;
}

// Какого агента холста переписка сейчас открыта.
function currentNode() {
  return selectedSession()?.node_id ?? ENTRY;
}

async function openAgentChat(nodeId) {
  const owner = ownerSession();
  if (!owner || nodeId === currentNode()) return;
  if (nodeId === ENTRY) {
    selectSession(owner.id);
    return;
  }
  try {
    const opened = await sessionsApi.openNode(owner.id, nodeId);
    // Переписку могли только что создать: без неё в списке сессий её не открыть.
    if (!state.sessions.some((item) => item.id === opened.id)) await loadSessions();
    selectSession(opened.id);
  } catch (error) {
    setStatus(`Не удалось открыть переписку: ${error.message}`);
    showChatAgent();
  }
}

// Агент сессии меняется прямо в чате: переписка остаётся, следующие сообщения получает новый агент.
async function chooseSessionAgent(agentId) {
  const owner = ownerSession();
  if (!owner) return;
  try {
    if (owner.agent_id !== agentId) {
      if (state.streaming) throw new Error("дождитесь конца ответа");
      const updated = await sessionsApi.configure(owner.id, {
        provider: owner.provider, model: owner.model || state.preferredModel, workspace: null, agent_id: agentId,
      });
      state.sessions = state.sessions.map((session) => session.id === updated.id ? updated : session);
    }
    if (state.selectedId !== owner.id) selectSession(owner.id);
    else render();
  } catch (error) {
    showError(new Error(`Не удалось сменить агента: ${error.message}`));
    showChatAgent();
  }
}

// Под чатом: агент сессии — любой из агентов; агенты холста, если они есть, идут второй группой.
function renderChatAgents() {
  const own = agents.map((agent) => [SESSION_AGENT + agent.id, agent.name]);
  const team = (graph?.nodes ?? []).filter((node) => node.id !== ENTRY).map((node) => [node.id, displayName(node)]);
  const key = JSON.stringify([own, team]);
  const rebuilt = elements.agent.dataset.key !== key;
  if (rebuilt) {
    elements.agent.dataset.key = key;
    if (team.length) elements.agent.replaceChildren(optionGroup("Агент сессии", own), optionGroup("Агенты холста", team));
    else fillSelect(elements.agent, own);
  }
  if (rebuilt || document.activeElement !== elements.agent) showChatAgent();
}

// С кем сейчас разговор: с агентом сессии или с агентом холста.
function showChatAgent() {
  elements.agent.value = currentNode() === ENTRY ? SESSION_AGENT + (ownerSession()?.agent_id ?? "") : currentNode();
}

function optionGroup(label, options) {
  const group = document.createElement("optgroup");
  group.label = label;
  fillSelect(group, options);
  return group;
}

// Вызывается при каждой перерисовке: холст перестраивается, только когда открыли другую сессию.
export function renderSessionCanvas() {
  // В переписке агента холст тот же, что у сессии-хозяйки: это одна команда.
  const owner = ownerSession();
  elements.headerTabs.hidden = !owner || state.showingSettings || state.showingTools
    || state.showingMemory || state.showingPrompt || state.showingSkills;
  elements.detail.classList.toggle("no-canvas", !owner);
  renderAgentChatBar(selectedSession(), owner);
  renderFiles();
  const key = owner ? `${owner.id}:${owner.agent_id}` : "";
  if (key !== openedKey) {
    openedKey = key;
    open(owner);
  }
  renderChatAgents();
  highlightCurrent();
}

function highlightCurrent() {
  for (const card of elements.teamStage.querySelectorAll(".agent-card")) {
    const current = card.dataset.nodeId === currentNode();
    card.classList.toggle("current", current);
    card.title = current ? "С этим агентом сейчас разговор в чате" : "Щёлкните, чтобы открыть разговор с этим агентом";
  }
}

// В переписке агента сверху видно, с кем разговор, и есть возврат к агенту сессии.
function renderAgentChatBar(session, owner) {
  const agentChat = Boolean(session?.parent_id);
  elements.runBar.hidden = !agentChat;
  // Память разбирает только разговор с агентом сессии.
  if (agentChat) elements.memoryUpdateChat.disabled = true;
  const key = agentChat ? `${session.id}:${owner?.title}:${nodeName(session.node_id)}` : "";
  if (elements.runBar.dataset.key === key) return;
  elements.runBar.dataset.key = key;
  if (!agentChat) {
    elements.runBar.replaceChildren();
    return;
  }
  const back = document.createElement("button");
  back.type = "button";
  back.textContent = `← ${nodeName(ENTRY)}`;
  back.disabled = !owner;
  back.addEventListener("click", () => selectSession(session.parent_id));
  const note = document.createElement("span");
  note.className = "run-status";
  note.textContent = `разговор с ${nodeName(session.node_id)} · ${owner?.title ?? ""}`;
  elements.runBar.replaceChildren(back, note);
}

function open(session) {
  clearTimeout(saveTimer);
  sessionId = session?.id ?? null;
  graph = session ? structuredClone(session.canvas) : null;
  if (graph && !graph.nodes.some((node) => node.id === ENTRY)) {
    graph.nodes.unshift({ id: ENTRY, name: "", agent_id: "", x: 40, y: 40 });
  }
  runView = { activeNode: null, label: "" };
  setStatus("");
  void loadAgents();
}

// Ход команды сообщает, какой агент сейчас работает.
export function showRunState(update) {
  runView = { ...runView, ...update };
  drawGraph();
}

export function nodeName(nodeId) {
  const node = graph?.nodes.find((item) => item.id === nodeId);
  return node ? displayName(node) : nodeId;
}

// У карточки «вход» своего имени нет, пока его не задали: она называется по агенту сессии.
function displayName(node) {
  return node.name || agentOf(node)?.name || "Основной агент";
}

function setCollapsed(value, save = true) {
  elements.detail.classList.toggle("canvas-collapsed", value);
  syncTabs();
  if (!save) return;
  try {
    localStorage.setItem("canvas-collapsed", value ? "1" : "0");
  } catch {
    // Без хранилища состояние холста не запомнится.
  }
}

let activeTab = "canvas";

function setTab(tab, save = true) {
  activeTab = tab;
  syncTabs();
  if (!save) return;
  try {
    localStorage.setItem("right-tab", tab);
  } catch {
    // Без хранилища вкладка не запомнится.
  }
}

function syncTabs() {
  const shown = !elements.detail.classList.contains("canvas-collapsed");
  elements.canvasPane.hidden = activeTab !== "canvas";
  elements.filesPane.hidden = activeTab !== "files";
  elements.tabCanvas.setAttribute("aria-pressed", String(shown && activeTab === "canvas"));
  elements.tabFiles.setAttribute("aria-pressed", String(shown && activeTab === "files"));
}

function loadTab() {
  try {
    return localStorage.getItem("right-tab") === "files" ? "files" : "canvas";
  } catch {
    return "canvas";
  }
}

function loadCollapsed() {
  try {
    return localStorage.getItem("canvas-collapsed") === "1";
  } catch {
    return false;
  }
}

function agentOf(node) {
  const agentId = node.id === ENTRY ? ownerSession()?.agent_id : node.agent_id;
  return agents.find((item) => item.id === agentId);
}

function addNode(agent) {
  if (!graph) return;
  const ids = new Set(graph.nodes.map((node) => node.id));
  const names = new Set(graph.nodes.map((node) => node.name));
  let suffix = 1;
  while (ids.has(nodeId(agent.id, suffix)) || names.has(numberedName(agent.name, suffix))) suffix += 1;
  // Новая карточка появляется посреди видимой части холста.
  const bounds = elements.teamCanvas.getBoundingClientRect();
  const offset = graph.nodes.length % 5 * 24;
  graph.nodes.push({
    id: nodeId(agent.id, suffix), name: numberedName(agent.name, suffix), agent_id: agent.id,
    x: Math.round((bounds.width / 2 - view.x) / view.zoom - 140 + offset),
    y: Math.round((bounds.height / 3 - view.y) / view.zoom + offset),
  });
  scheduleSave();
  drawGraph();
}

const nodeId = (agentId, suffix) => suffix === 1 ? agentId : `${agentId}-${suffix}`;
const numberedName = (name, suffix) => suffix === 1 ? name : `${name} ${suffix}`;

function removeNode(node) {
  if (!window.confirm(`Убрать «${node.name}» с холста вместе с его связями?`)) return;
  graph.nodes = graph.nodes.filter((item) => item.id !== node.id);
  graph.edges = graph.edges.filter((edge) => edge.from !== node.id && edge.to !== node.id);
  scheduleSave();
  drawGraph();
}

function renameNode(node) {
  const name = window.prompt("Имя агента на холсте — так его видят соседи", displayName(node))?.trim();
  if (!name || name === node.name) return;
  node.name = name;
  scheduleSave();
  drawGraph();
}

function connect(source, target) {
  if (source === target || graph.edges.some((edge) => edge.from === source && edge.to === target)) return;
  graph.edges.push({ from: source, to: target });
  scheduleSave();
  drawGraph();
}

function removeEdge(edge) {
  if (!window.confirm(`Удалить связь ${label(edge.from)} → ${label(edge.to)}?`)) return;
  graph.edges = graph.edges.filter((item) => item !== edge);
  scheduleSave();
  drawGraph();
}

const label = (id) => nodeName(id);

function scheduleSave() {
  setStatus("Сохранение…");
  clearTimeout(saveTimer);
  const target = sessionId;
  saveTimer = setTimeout(() => { void save(target); }, SAVE_DELAY_MS);
}

async function save(target) {
  if (target !== sessionId || !graph) return;
  const snapshot = structuredClone(graph);
  try {
    const saved = await sessionsApi.saveCanvas(target, snapshot);
    // Список сессий держит холст для следующего открытия этой сессии.
    const session = state.sessions.find((item) => item.id === target);
    if (session) session.canvas = saved.canvas;
    if (target === sessionId) {
      setStatus(saved.errors.length ? `Команда не запустится: ${saved.errors.join("; ")}` : "Сохранено");
    }
  } catch (error) {
    setStatus(`Не сохранено: ${error.message}`);
  }
}

function setStatus(text) {
  elements.teamsStatus.textContent = text;
}

function drawGraph() {
  elements.teamStage.querySelectorAll(".agent-card").forEach((card) => card.remove());
  for (const node of graph?.nodes ?? []) elements.teamStage.append(card(node));
  drawEdges();
  // Подсказка видна, пока агенту сессии некому поручать работу.
  elements.canvasHint.hidden = Boolean(graph?.edges.some((edge) => edge.from === ENTRY));
  applyView();
  renderChatAgents();
  highlightCurrent();
}

function card(node) {
  const agent = agentOf(node);
  const item = document.createElement("article");
  item.className = "agent-card";
  item.classList.toggle("running", runView.activeNode === node.id);
  item.dataset.nodeId = node.id;
  item.style.left = `${node.x}px`;
  item.style.top = `${node.y}px`;
  applySize(item, node);

  const header = document.createElement("header");
  const title = document.createElement("strong");
  title.textContent = displayName(node);
  title.title = "Двойной щелчок — переименовать";
  title.addEventListener("dblclick", () => renameNode(node));
  header.append(title);
  if (node.id === ENTRY) header.append(span("agent-card-entry", "вход"));
  if (runView.activeNode === node.id) header.append(span("agent-card-badge", runView.label));
  // Агента сессии с холста не убрать: через него в команду приходят сообщения пользователя.
  if (node.id !== ENTRY) header.append(headerButton("×", `Убрать «${node.name}» с холста`, () => removeNode(node)));
  makeDraggable(header, item, node);
  // Щелчок по карточке открывает в чате разговор с этим агентом. Кнопки, порты, уголок и выделение
  // текста промпта живут своей жизнью, а щелчок после перетаскивания — это конец перетаскивания.
  item.addEventListener("click", (event) => {
    if (item.dataset.dragged || event.target.closest("button, .agent-card-prompt")) return;
    if (window.getSelection()?.toString()) return;
    void openAgentChat(node.id);
  });
  // Секции прокручиваются под заголовком, если карточку сделали ниже содержимого.
  const content = document.createElement("div");
  content.className = "agent-card-content";
  item.append(header, content);

  if (!agent) {
    content.append(section(node, "agent", "Агент", null, [span("agent-card-missing", `Агент ${node.agent_id || "сессии"} не найден`)]));
  } else {
    const about = [span("agent-card-meta", agent.name)];
    if (agent.description) about.push(paragraph(agent.description));
    const prompt = document.createElement("div");
    prompt.className = "agent-card-prompt";
    prompt.textContent = agent.system_prompt;
    const edit = headerButton("Изменить", "Изменить инструкции агента", () => openPromptEditor(agent));
    edit.classList.add("agent-card-edit");
    const skills = agent.skills === null ? null : agent.skills;
    content.append(
      section(node, "agent", "Агент", null, about),
      section(node, "prompt", "Инструкции", null, [prompt, edit]),
      section(node, "tools", "Инструменты", agent.tools.length,
        rows(node, "tools", agent.tools.map((tool) => [TOOL_GLYPHS[tool] || "⚒", tool]), "нет инструментов")),
      section(node, "skills", "Навыки", skills?.length ?? "все",
        skills === null ? [row("◆", "все навыки каталога")] : rows(node, "skills", skills.map((skill) => ["◆", skill]), "без навыков")),
    );
  }

  // Связь тянется с любой стороны карточки — с той, что ближе к получателю.
  for (const side of Object.keys(SIDES)) {
    const port = document.createElement("button");
    port.type = "button";
    port.className = `agent-card-port port-${side}`;
    port.title = "Протяните к другой карточке: этот агент сможет ей писать";
    port.setAttribute("aria-label", `Связь от «${node.name}»`);
    makeConnector(port, node, side);
    item.append(port);
  }
  const grip = document.createElement("button");
  grip.type = "button";
  grip.className = "agent-card-resize";
  grip.title = "Потяните, чтобы изменить размер; двойной щелчок — по содержимому";
  grip.setAttribute("aria-label", `Размер карточки «${node.name}»`);
  makeResizable(grip, item, node);
  item.append(grip);
  return item;
}

function applySize(item, node) {
  item.style.width = node.width ? `${node.width}px` : "";
  item.style.height = node.height ? `${node.height}px` : "";
  item.classList.toggle("sized", Boolean(node.height));
}

function makeResizable(grip, item, node) {
  grip.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    grip.setPointerCapture(event.pointerId);
    const start = { x: event.clientX, y: event.clientY, width: item.offsetWidth, height: item.offsetHeight };
    // Высоту фиксируем, только если карточку тянули вниз или вверх: иначе она по-прежнему растёт с содержимым.
    let vertical = Boolean(node.height);
    const move = (moved) => {
      const dy = (moved.clientY - start.y) / view.zoom;
      vertical ||= Math.abs(dy) > 3;
      node.width = Math.round(Math.max(CARD_MIN_WIDTH, start.width + (moved.clientX - start.x) / view.zoom));
      if (vertical) node.height = Math.round(Math.max(CARD_MIN_HEIGHT, start.height + dy));
      applySize(item, node);
      drawEdges();
    };
    const stop = () => {
      grip.removeEventListener("pointermove", move);
      if (node.width !== start.width || node.height !== start.height) scheduleSave();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", stop, { once: true });
    grip.addEventListener("pointercancel", stop, { once: true });
  });
  grip.addEventListener("dblclick", () => {
    delete node.width;
    delete node.height;
    applySize(item, node);
    drawEdges();
    scheduleSave();
  });
}

function section(node, key, title, count, content) {
  const id = `${node.id}:${key}`;
  const block = document.createElement("section");
  block.className = "agent-card-section";
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "agent-card-toggle";
  toggle.setAttribute("aria-expanded", String(!collapsed.has(id)));
  toggle.append(span("agent-card-chevron", collapsed.has(id) ? "▸" : "▾"), title);
  if (count !== null) toggle.append(span("agent-card-count", String(count)));
  toggle.addEventListener("click", () => {
    if (collapsed.has(id)) collapsed.delete(id);
    else collapsed.add(id);
    drawGraph();
  });
  block.append(toggle);
  if (!collapsed.has(id)) {
    const body = document.createElement("div");
    body.className = "agent-card-body";
    body.append(...content);
    block.append(body);
  }
  return block;
}

function rows(node, key, items, emptyText) {
  if (!items.length) return [span("agent-card-empty", emptyText)];
  const id = `${node.id}:${key}`;
  const all = expanded.has(id) || items.length <= VISIBLE_ROWS;
  const shown = all ? items : items.slice(0, VISIBLE_ROWS - 1);
  const result = shown.map(([glyph, text]) => row(glyph, text));
  if (!all) {
    const more = headerButton(`Ещё ${items.length - shown.length}`, "Показать все", () => {
      expanded.add(id);
      drawGraph();
    });
    more.className = "agent-card-row agent-card-more";
    result.push(more);
  }
  return result;
}

function row(glyph, text) {
  const item = document.createElement("div");
  item.className = "agent-card-row";
  item.append(span("agent-card-glyph", glyph), text);
  return item;
}

function span(className, text) {
  const item = document.createElement("span");
  item.className = className;
  item.textContent = text;
  return item;
}

function paragraph(text) {
  const item = document.createElement("p");
  item.textContent = text;
  return item;
}

function headerButton(text, title, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "agent-card-action";
  button.textContent = text;
  button.title = title;
  button.setAttribute("aria-label", title);
  button.addEventListener("click", onClick);
  return button;
}

function initPromptDialog() {
  let editing = null;
  elements.agentPromptForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const agent = agents.find((item) => item.id === elements.agentPromptForm.dataset.agentId);
    const text = elements.agentPromptText.value.trim();
    if (!agent || !text || editing) return;
    editing = true;
    elements.agentPromptSave.disabled = true;
    try {
      const saved = await agentsApi.savePrompt(agent.id, text);
      agent.system_prompt = saved.system_prompt;
      elements.agentPromptDialog.close();
      drawGraph();
    } catch (error) {
      elements.agentPromptError.textContent = error.message;
      elements.agentPromptError.hidden = false;
    } finally {
      editing = null;
      elements.agentPromptSave.disabled = false;
    }
  });
  for (const button of [elements.agentPromptCancel, elements.agentPromptClose]) {
    button.addEventListener("click", () => elements.agentPromptDialog.close());
  }
}

function openPromptEditor(agent) {
  elements.agentPromptForm.dataset.agentId = agent.id;
  elements.agentPromptTitle.textContent = `Инструкции · ${agent.name}`;
  elements.agentPromptText.value = agent.system_prompt;
  elements.agentPromptError.hidden = true;
  elements.agentPromptDialog.showModal();
  elements.agentPromptText.focus();
}

function makeDraggable(handle, item, node) {
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("button")) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    const start = { x: event.clientX, y: event.clientY, nodeX: node.x, nodeY: node.y };
    const move = (moved) => {
      node.x = Math.round(start.nodeX + (moved.clientX - start.x) / view.zoom);
      node.y = Math.round(start.nodeY + (moved.clientY - start.y) / view.zoom);
      item.style.left = `${node.x}px`;
      item.style.top = `${node.y}px`;
      drawEdges();
    };
    const stop = () => {
      handle.removeEventListener("pointermove", move);
      if (node.x === start.nodeX && node.y === start.nodeY) return;
      // Щелчок, который браузер пришлёт сразу после отпускания, — конец перетаскивания, а не выбор агента.
      item.dataset.dragged = "1";
      setTimeout(() => { delete item.dataset.dragged; }, 0);
      scheduleSave();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", stop, { once: true });
    handle.addEventListener("pointercancel", stop, { once: true });
  });
}

function makeConnector(port, node, side) {
  port.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    port.setPointerCapture(event.pointerId);
    const line = svg("path", { class: "team-edge draft", "marker-end": "url(#team-arrow)" });
    elements.teamEdges.append(line);
    const from = sidePoint(rect(node.id), side, 0.5);
    let target = null;
    const move = (moved) => {
      line.setAttribute("d", bezier(from, side, stagePoint(moved), null));
      // Отпустить можно в любом месте карточки: подсвечиваем, кому уйдёт связь.
      const over = document.elementFromPoint(moved.clientX, moved.clientY)?.closest(".agent-card");
      const next = over && over.dataset.nodeId !== node.id ? over : null;
      if (next === target) return;
      target?.classList.remove("drop-target");
      next?.classList.add("drop-target");
      target = next;
    };
    const finish = () => {
      port.removeEventListener("pointermove", move);
      target?.classList.remove("drop-target");
      line.remove();
    };
    move(event);
    port.addEventListener("pointermove", move);
    port.addEventListener("pointerup", () => {
      const chosen = target;
      finish();
      if (chosen) connect(node.id, chosen.dataset.nodeId);
    }, { once: true });
    port.addEventListener("pointercancel", finish, { once: true });
  });
}

function drawEdges() {
  elements.teamEdges.querySelectorAll(".team-edge-group").forEach((group) => group.remove());
  for (const edge of graph?.edges ?? []) {
    const source = rect(edge.from);
    const target = rect(edge.to);
    if (!source || !target) continue;
    const { start, startSide, end, endSide } = route(source, target);
    const path = bezier(start, startSide, end, endSide);
    const group = svg("g", { class: "team-edge-group" });
    const active = runView.activeNode && (edge.from === runView.activeNode || edge.to === runView.activeNode);
    group.classList.toggle("active", Boolean(active));
    // Широкая прозрачная линия поверх пунктира: по связи легко попасть мышью.
    const hit = svg("path", { d: path, class: "team-edge-hit" });
    const tip = svg("title", {});
    tip.textContent = `${label(edge.from)} → ${label(edge.to)} · щелчок — удалить`;
    hit.append(tip);
    hit.addEventListener("click", () => removeEdge(edge));
    group.append(
      svg("path", { d: path, class: "team-edge", "marker-end": "url(#team-arrow)" }),
      svg("circle", { cx: start.x, cy: start.y, r: 2.5, class: "team-edge-dot" }),
      hit,
    );
    elements.teamEdges.append(group);
  }
}

// Связь соединяет стороны карточек, обращённые друг к другу: боковые, если карточки стоят рядом,
// верх и низ — если одна над другой. Встречные связи идут со сдвигом вдоль стороны и не сливаются.
function route(source, target) {
  const dx = target.x + target.width / 2 - (source.x + source.width / 2);
  const dy = target.y + target.height / 2 - (source.y + source.height / 2);
  // Сравниваем зазоры между карточками, а не расстояние между центрами: карточки бывают высокими.
  const gapX = Math.abs(dx) - (source.width + target.width) / 2;
  const gapY = Math.abs(dy) - (source.height + target.height) / 2;
  const [startSide, endSide, forward] = gapX >= gapY
    ? (dx >= 0 ? ["right", "left", true] : ["left", "right", false])
    : (dy >= 0 ? ["bottom", "top", true] : ["top", "bottom", false]);
  const level = forward ? FORWARD_LEVEL : BACKWARD_LEVEL;
  return {
    start: sidePoint(source, startSide, level), startSide,
    end: sidePoint(target, endSide, level), endSide,
  };
}

// Точка на стороне карточки: `level` — доля длины стороны.
function sidePoint(box, side, level) {
  if (side === "top" || side === "bottom") {
    return { x: box.x + box.width * level, y: side === "top" ? box.y : box.y + box.height };
  }
  return { x: side === "left" ? box.x : box.x + box.width, y: box.y + box.height * level };
}

// Плавная кривая, которая выходит из стороны и входит в сторону перпендикулярно, как провод между блоками.
function bezier(start, startSide, end, endSide) {
  const reach = Math.min(160, Math.max(40, Math.hypot(end.x - start.x, end.y - start.y) / 2));
  const out = SIDES[startSide];
  const into = endSide ? SIDES[endSide] : { x: 0, y: 0 };
  return `M${start.x} ${start.y}C${start.x + out.x * reach} ${start.y + out.y * reach} `
    + `${end.x + into.x * reach} ${end.y + into.y * reach} ${end.x} ${end.y}`;
}

function rect(id) {
  const item = elements.teamStage.querySelector(`.agent-card[data-node-id="${CSS.escape(id)}"]`);
  return item && { x: item.offsetLeft, y: item.offsetTop, width: item.offsetWidth, height: item.offsetHeight };
}

function stagePoint(event) {
  const bounds = elements.teamCanvas.getBoundingClientRect();
  return { x: (event.clientX - bounds.left - view.x) / view.zoom, y: (event.clientY - bounds.top - view.y) / view.zoom };
}

// Пустое место холста тянется мышью, колесо двигает холст, Ctrl + колесо меняет масштаб у курсора.
function initPanAndZoom() {
  const canvas = elements.teamCanvas;
  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest(".agent-card, .team-edge-hit, .zoom-controls")) return;
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("panning");
    const start = { x: event.clientX - view.x, y: event.clientY - view.y };
    const move = (moved) => {
      view.x = moved.clientX - start.x;
      view.y = moved.clientY - start.y;
      applyView();
    };
    const stop = () => {
      canvas.classList.remove("panning");
      canvas.removeEventListener("pointermove", move);
    };
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", stop, { once: true });
    canvas.addEventListener("pointercancel", stop, { once: true });
  });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) {
      zoomAt(event.clientX, event.clientY, view.zoom * (event.deltaY < 0 ? 1.1 : 1 / 1.1));
    } else {
      view.x -= event.deltaX;
      view.y -= event.deltaY;
      applyView();
    }
  }, { passive: false });
}

function zoomAtCenter(zoom) {
  const bounds = elements.teamCanvas.getBoundingClientRect();
  zoomAt(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2, zoom);
}

function zoomAt(clientX, clientY, zoom) {
  const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(zoom * 100) / 100));
  const bounds = elements.teamCanvas.getBoundingClientRect();
  const pointX = clientX - bounds.left;
  const pointY = clientY - bounds.top;
  // Точка под курсором остаётся на месте, меняется всё вокруг неё.
  view.x = pointX - (pointX - view.x) * next / view.zoom;
  view.y = pointY - (pointY - view.y) * next / view.zoom;
  view.zoom = next;
  applyView();
}

function applyView() {
  elements.teamStage.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`;
  elements.teamCanvas.style.backgroundPosition = `${view.x}px ${view.y}px`;
  elements.teamCanvas.style.backgroundSize = `${GRID * view.zoom}px ${GRID * view.zoom}px`;
  elements.zoomReset.textContent = `${Math.round(view.zoom * 100)}%`;
}

function svg(name, attributes) {
  const item = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, value);
  return item;
}

function fillSelect(select, options) {
  select.replaceChildren(...options.map(([value, text]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    return option;
  }));
}
