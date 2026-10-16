// Внимание пользователя: карточки, которые ждут решения, — в чате над полем ввода;
// задачи саб-агентов открытой сессии — в доке там же.
import { el, parse, renderMarkdown } from "./chat.js";
import { lineDiff, renderDiff } from "./diff.js";
import { initial, tone } from "./marks.js";

const STATUS = { queued: "в очереди", running: "работает", waiting: "ждёт решения", done: "готово", failed: "ошибка" };
const KIND = { tool: "Инструмент", skill: "Скилл", agent: "Саб-агент" };
const ACTIVE = new Set(["queued", "running", "waiting"]);
const EDIT_KIND = { merge: "слить дубли", contradiction: "противоречие", outdated: "устарело", fix: "исправить" };
const DOCK_ROWS = 5;
// Столько агентов в карточке «Кто сделает» — строками, остальные — в списке «Другому агенту…».
const ROUTE_ROWS = 4;
// Группа доверия карточки (MCP-сервер) — как её назвать на кнопке «утвердить все … в этой сессии».
const TRUST = { browser: "действия браузера", search: "запросы поиска" };

// onDecision(card, {action, text}) отправляет решение; onOpen(sessionId) открывает сессию;
// readFile(card, path) — текущий текст файла в проекте карточки или null: по нему считается разница правки.
// inChat — карточка стоит в чате своей сессии: строка «где» нужна, только если просит саб-агент.
export function renderCards(root, cards, { onDecision, onOpen, readFile, busy, inChat = false }) {
  root.replaceChildren(...cards.map((card) => cardNode(card, onDecision, onOpen, readFile, busy, inChat)));
}

// Задачи — деревом «кто кому поручил»: задача агента, которую поручил другой агент, стоит под задачей поручившего.
// Сначала ветки, где работа ещё идёт, затем остальные по свежести; лишние — ссылкой на «Команду».
export function renderDock({ count, summary, list }, tasks, { onOpen, onMore }) {
  const ordered = taskTree(tasks);
  count.textContent = `· ${tasks.length}`;
  const totals = Object.keys(STATUS)
    .map((status) => [status, tasks.filter((task) => task.status === status).length])
    .filter(([, number]) => number);
  summary.textContent = totals.map(([status, number]) => `${number} ${STATUS[status]}`).join(" · ");
  const rows = ordered.slice(0, DOCK_ROWS).map(([task, depth]) => taskNode(task, depth, onOpen));
  if (ordered.length > DOCK_ROWS) {
    const more = el("li", "more", `+${ordered.length - DOCK_ROWS} ещё — все задачи на вкладке «Команда»`);
    more.addEventListener("click", onMore);
    rows.push(more);
  }
  list.replaceChildren(...rows);
}

function cardNode(card, onDecision, onOpen, readFile, busy, inChat) {
  const pending = card.pending;
  const node = el("div", "card");
  if (!inChat) {
    const where = el("div", "where", card.agent ? `${card.agent} · ${card.session_title}` : card.session_title);
    where.title = "Открыть сессию";
    where.style.cursor = "pointer";
    where.addEventListener("click", () => onOpen(card.owner_id));
    node.append(where);
  } else if (card.agent) {
    // Просит саб-агент этой сессии: щелчок открывает его разговор.
    const where = el("div", "where", pending.kind === "question" ? "вопрос от агента · открыть разговор" : `${card.agent} просит подтвердить`);
    where.title = "Открыть разговор с агентом";
    where.style.cursor = "pointer";
    where.addEventListener("click", () => onOpen(card.session_id));
    node.append(where);
  }
  node.append(el("div", "title", pending.title || titleOf(pending)));
  // Вопрос агента пользователю: текст вопроса, сразу поле ответа; «Пусть решает сам» — без ответа.
  if (pending.kind === "question") return questionNode(node, pending, card, onDecision, busy);
  // Кто сделает задачу: предложенный агент, сам основной или другой агент. Выбор запомнится для таких задач.
  if (pending.kind === "route") return routeNode(node, pending, card, onDecision, busy);
  // Правки вики после lint — список с галочками: применятся только отмеченные.
  const checks = pending.kind === "wiki_lint" ? editsBody(node, pending.edits || []) : null;
  if (pending.kind === "proposal") proposalBody(node, pending.proposal || {});
  else if (pending.kind === "tools_request") toolsBody(node, pending);
  else if (!checks) callBody(node, pending, (path) => readFile(card, path));

  const actions = el("div", "actions");
  const approve = button(checks ? "Применить отмеченные" : "Утвердить", "primary");
  const reject = button("Отклонить");
  const reply = button("Ответить");
  // Действия браузера (и других MCP-серверов) можно разрешить разом до конца сессии.
  const trust = pending.trust_group ? button(`Утвердить все ${TRUST[pending.trust_group] ?? `вызовы ${pending.trust_group}`} в этой сессии`) : null;
  const text = el("textarea");
  text.placeholder = "Что поправить или уточнить";
  text.hidden = true;
  const send = button("Отправить ответ", "primary");
  send.hidden = true;
  const all = [approve, reject, reply, send, trust].filter(Boolean);
  for (const item of all) item.disabled = busy;
  if (checks) {
    const sync = () => { approve.disabled = busy || !checks.some((box) => box.checked); };
    for (const box of checks) box.addEventListener("change", sync);
  }
  const decide = async (decision) => {
    for (const item of all) item.disabled = true;
    await onDecision(card, decision);
  };
  approve.addEventListener("click", () => decide(checks
    ? { action: "approve", selected: checks.flatMap((box, index) => (box.checked ? [index + 1] : [])) }
    : { action: "approve" }));
  reject.addEventListener("click", () => decide({ action: "reject" }));
  trust?.addEventListener("click", () => decide({ action: "approve", trust: true }));
  reply.addEventListener("click", () => {
    text.hidden = send.hidden = false;
    reply.hidden = true;
    text.focus();
  });
  send.addEventListener("click", () => {
    if (text.value.trim()) decide({ action: "reply", text: text.value.trim() });
  });
  actions.append(...[approve, trust, reject, reply].filter(Boolean));
  node.append(actions, text, send);
  return node;
}

function questionNode(node, pending, card, onDecision, busy) {
  const text = el("div", "question text");
  renderMarkdown(text, pending.question || "");
  const answer = el("textarea");
  answer.placeholder = "Ваш ответ";
  const send = button("Ответить", "primary");
  const skip = button("Пусть решает сам");
  const all = [send, skip];
  for (const item of all) item.disabled = busy;
  const decide = async (decision) => {
    for (const item of all) item.disabled = true;
    await onDecision(card, decision);
  };
  send.addEventListener("click", () => {
    if (answer.value.trim()) decide({ action: "reply", text: answer.value.trim() });
  });
  answer.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && answer.value.trim()) decide({ action: "reply", text: answer.value.trim() });
  });
  skip.addEventListener("click", () => decide({ action: "reject" }));
  const actions = el("div", "actions");
  actions.append(send, skip);
  node.classList.add("question-card");
  node.append(text, answer, actions);
  return node;
}

// Варианты — строками со значком агента, как в сайдбаре: предложенный Основным первым и с пометкой,
// дальше остальные агенты команды и «сделает сам». Большая команда — лишние уходят в список.
function routeNode(node, pending, card, onDecision, busy) {
  const agents = [...(pending.agents || [])].sort((a, b) => (b.id === pending.suggested) - (a.id === pending.suggested));
  const shown = agents.slice(0, ROUTE_ROWS);
  const rest = agents.slice(ROUTE_ROWS);
  const rows = [...shown.map((agent) => routeOption(agent, agent.id === pending.suggested)), routeOption(null, !pending.suggested)];
  const other = el("select");
  if (rest.length) {
    other.append(el("option", "", "Другому агенту…"), ...rest.map((agent) => {
      const option = el("option", "", agent.name);
      option.value = agent.id;
      option.title = agent.description || "";
      return option;
    }));
    other.firstChild.value = "";
    other.addEventListener("change", () => {
      if (other.value) decide(other.value, `Поручить: ${other.selectedOptions[0].textContent}`);
    });
  }
  const all = [...rows, other];
  for (const item of all) item.disabled = busy;
  for (const [index, row] of rows.entries()) {
    const agent = shown[index];
    row.addEventListener("click", () => (agent ? decide(agent.id, `Поручить: ${agent.name}`) : decide("main", "Сделай сам")));
  }
  async function decide(choice, label) {
    for (const item of all) item.disabled = true;
    await onDecision(card, { action: "reply", text: label, choice });
  }
  const options = el("div", "route-options");
  options.append(...rows, ...(rest.length ? [other] : []));
  node.classList.add("question-card");
  node.append(options);
  if (pending.task_kind) node.append(el("p", "hint", `Выбор запомнится для задач «${pending.task_kind}» в этом проекте — изменить можно в настройках проекта.`));
  return node;
}

// agent — саб-агент; null — сам Основной.
function routeOption(agent, suggested) {
  const row = button("", `route-option${suggested ? " suggested" : ""}`);
  const mark = el("span", "route-mark");
  if (agent) {
    mark.textContent = initial(agent.name);
    mark.style.setProperty("--tone", tone(agent.id));
  } else {
    const mascot = el("img");
    mascot.src = "/ui/assets/meepo-mascot.png";
    mascot.alt = "";
    mark.append(mascot);
  }
  const text = el("span", "route-text");
  text.append(
    el("span", "route-name", agent ? `Поручить: ${agent.name}` : "Сделай сам"),
    el("span", "route-about", agent ? agent.description || agent.id : "Основной агент сделает задачу без саб-агентов"),
  );
  row.append(mark, text);
  if (suggested) row.append(el("span", "route-tag", "предлагает Основной"));
  return row;
}

// Агент просит подключить ему инструменты: какие и зачем. После «Утвердить» они записываются в его файл.
function toolsBody(node, pending) {
  if (pending.reason) node.append(el("p", "reason", pending.reason));
  node.append(el("p", "hint", `Инструменты: ${(pending.tools || []).join(", ")} — появятся у агента сразу и останутся у него.`));
}

function titleOf(pending) {
  const args = parse(pending.call?.arguments);
  if (pending.call?.name === "write_file") return `Записать файл ${args.path} (${(args.content || "").length} символов)`;
  if (pending.call?.name === "edit_file") return `Изменить файл ${args.path}`;
  return `Выполнить «${pending.tool_name || pending.call?.name}»`;
}

function callBody(node, pending, readFile) {
  const args = parse(pending.call?.arguments);
  if (pending.preview) node.append(block("Что будет записано", pending.preview, true));
  else if (["write_file", "edit_file"].includes(pending.call?.name)) {
    const slot = el("div", "hint", "Сравниваю с файлом…");
    node.append(slot);
    void fileChange(pending.call.name, args, readFile).then((change) => slot.replaceWith(change));
  } else node.append(block("Аргументы", JSON.stringify(args, null, 2), true));
}

// Разница правки по всему файлу: так видны номера строк и соседние строки. Если файл прочитать нельзя,
// write_file показывает новый текст целиком, edit_file — разницу самих фрагментов.
async function fileChange(name, args, readFile) {
  const current = typeof args.path === "string" ? await readFile(args.path) : null;
  let before = current ?? "";
  let after = String(args.content ?? "");
  let label = current === null ? "Новый файл" : "Изменения в файле";
  if (name === "edit_file") {
    const oldText = String(args.old_text ?? "");
    const newText = String(args.new_text ?? "");
    const once = current !== null && oldText && current.split(oldText).length === 2;
    [before, after] = once ? [current, current.replace(oldText, () => newText)] : [oldText, newText];
    label = once ? "Изменения в файле" : "Замена фрагмента";
  }
  const ops = lineDiff(before, after);
  if (!ops) return block(label, after, true);
  const details = el("details", "change");
  details.open = true;
  details.append(el("summary", "", label), renderDiff(ops));
  return details;
}

function editsBody(node, edits) {
  const list = el("div", "edits");
  const boxes = edits.map((edit, index) => {
    const row = el("label", "edit");
    const box = el("input");
    box.type = "checkbox";
    box.checked = true;
    const text = el("div", "edit-text");
    text.append(
      el("div", "edit-head", `${index + 1}. ${EDIT_KIND[edit.kind] || edit.kind} · ${edit.notes.join(", ")}`),
      el("div", "edit-problem", edit.problem),
      el("div", "edit-change", `→ ${edit.change}`),
    );
    row.append(box, text);
    list.append(row);
    return box;
  });
  node.append(list);
  return boxes;
}

function proposalBody(node, proposal) {
  if (proposal.reason) node.append(el("p", "reason", proposal.reason));
  if (proposal.action) return;
  if (proposal.description) node.append(el("p", "hint", `${KIND[proposal.kind] || ""}: ${proposal.description}`));
  if (proposal.kind === "tool") {
    const flags = [
      proposal.requires_approval ? "каждый вызов с подтверждением, может менять файлы" : "только чтение, без подтверждения",
      proposal.network ? "нужен интернет" : "без сети",
    ];
    if (proposal.requires?.length) {
      const missing = proposal.missing ?? [];
      flags.push(`пакеты: ${proposal.requires.join(", ")}${missing.length ? ` (ещё не установлены: ${missing.join(", ")})` : ""}`);
    }
    if (proposal.secrets?.length) {
      const absent = proposal.missing_secrets ?? [];
      flags.push(`секреты: ${proposal.secrets.join(", ")}${absent.length ? ` (ещё не заданы: ${absent.join(", ")})` : ""}`);
    }
    node.append(el("p", "hint", flags.join(" · ")));
    node.append(block("Параметры", JSON.stringify(proposal.parameters, null, 2)));
    node.append(block("Код script.py", proposal.content, true));
    if (proposal.previous) node.append(block("Сейчас", proposal.previous));
  } else if (proposal.kind === "agent") {
    node.append(el("p", "hint", `Инструменты: ${(proposal.tools || []).join(", ") || "нет"} · Скиллы: ${(proposal.skills || []).join(", ") || "нет"}`));
    node.append(block("Системный промпт", proposal.content, true));
    if (proposal.previous) node.append(block("Сейчас", proposal.previous));
  } else {
    node.append(block("Инструкции", proposal.content, true));
    if (proposal.previous) node.append(block("Сейчас", proposal.previous));
  }
}

function block(label, text, open = false) {
  const details = el("details");
  details.open = open;
  const pre = el("pre");
  pre.textContent = text;
  details.append(el("summary", "", label), pre);
  return details;
}

// [задача, глубина] в порядке показа. Корни — поручения основного агента; поручивший, которого среди задач нет,
// или задачи, поручившие друг другу по кругу, тоже становятся корнями — ничего не теряется.
function taskTree(tasks) {
  const byAgent = new Map(tasks.map((task) => [task.agent, task]));
  const children = new Map();
  const roots = [];
  for (const task of tasks) {
    const parent = task.from && task.from !== task.agent ? byAgent.get(task.from) : null;
    if (parent) children.set(parent, [...(children.get(parent) ?? []), task]);
    else roots.push(task);
  }
  const active = (task) => ACTIVE.has(task.status) || (children.get(task) ?? []).some(active);
  const order = (items) => [...items.filter(active), ...items.filter((task) => !active(task))];
  const rows = [];
  const seen = new Set();
  const walk = (task, depth) => {
    if (seen.has(task)) return;
    seen.add(task);
    rows.push([task, depth]);
    for (const child of order(children.get(task) ?? [])) walk(child, depth + 1);
  };
  for (const task of order(roots)) walk(task, 0);
  for (const task of tasks) walk(task, 0);
  return rows;
}

function taskNode(task, depth, onOpen) {
  const item = el("li");
  const title = el("span", "task-title", task.error ? `${task.title} — ${task.error}` : task.title);
  const agent = el("span", "task-agent");
  if (depth) agent.append(el("span", "task-guide", `${"   ".repeat(depth - 1)}└─ `));
  agent.append(task.agent);
  item.append(el("span", `status-dot ${task.status}`), agent, title,
    el("span", `task-state ${task.status}`, STATUS[task.status] || task.status));
  item.title = `${task.from || "Основной"} → ${task.agent}: ${task.title} · открыть разговор`;
  item.addEventListener("click", () => onOpen(task.session_id));
  return item;
}

function button(label, className = "") {
  const node = el("button", className, label);
  node.type = "button";
  return node;
}
