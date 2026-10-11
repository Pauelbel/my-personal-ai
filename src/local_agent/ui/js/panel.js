// Внимание пользователя: карточки, которые ждут решения, — в правой панели;
// задачи саб-агентов открытой сессии — в доке над полем ввода.
import { el, parse } from "./chat.js";
import { lineDiff, renderDiff } from "./diff.js";

const STATUS = { queued: "в очереди", running: "работает", waiting: "ждёт решения", done: "готово", failed: "ошибка" };
const KIND = { tool: "Инструмент", skill: "Скилл", agent: "Саб-агент" };
const ACTIVE = new Set(["queued", "running", "waiting"]);
const EDIT_KIND = { merge: "слить дубли", contradiction: "противоречие", outdated: "устарело", fix: "исправить" };
const DOCK_ROWS = 5;

// onDecision(card, {action, text}) отправляет решение; onOpen(sessionId) открывает сессию;
// readFile(card, path) — текущий текст файла в проекте карточки или null: по нему считается разница правки.
export function renderCards(root, cards, { onDecision, onOpen, readFile, busy }) {
  root.replaceChildren(...cards.map((card) => cardNode(card, onDecision, onOpen, readFile, busy)));
}

// Сначала задачи, которые ещё идут, затем остальные по свежести; лишние — ссылкой на холст команды.
export function renderDock({ count, summary, list }, tasks, { onOpen, onMore }) {
  const ordered = [...tasks.filter((task) => ACTIVE.has(task.status)), ...tasks.filter((task) => !ACTIVE.has(task.status))];
  count.textContent = `· ${tasks.length}`;
  const totals = Object.keys(STATUS)
    .map((status) => [status, tasks.filter((task) => task.status === status).length])
    .filter(([, number]) => number);
  summary.textContent = totals.map(([status, number]) => `${number} ${STATUS[status]}`).join(" · ");
  const rows = ordered.slice(0, DOCK_ROWS).map((task) => taskNode(task, onOpen));
  if (ordered.length > DOCK_ROWS) {
    const more = el("li", "more", `+${ordered.length - DOCK_ROWS} ещё — все задачи на вкладке «Команда»`);
    more.addEventListener("click", onMore);
    rows.push(more);
  }
  list.replaceChildren(...rows);
}

function cardNode(card, onDecision, onOpen, readFile, busy) {
  const pending = card.pending;
  const node = el("div", "card");
  const where = el("div", "where", card.agent ? `${card.agent} · ${card.session_title}` : card.session_title);
  where.title = "Открыть сессию";
  where.style.cursor = "pointer";
  where.addEventListener("click", () => onOpen(card.owner_id));
  node.append(where, el("div", "title", pending.title || titleOf(pending)));
  // Правки вики после lint — список с галочками: применятся только отмеченные.
  const checks = pending.kind === "wiki_lint" ? editsBody(node, pending.edits || []) : null;
  if (pending.kind === "proposal") proposalBody(node, pending.proposal || {});
  else if (!checks) callBody(node, pending, (path) => readFile(card, path));

  const actions = el("div", "actions");
  const approve = button(checks ? "Применить отмеченные" : "Утвердить", "primary");
  const reject = button("Отклонить");
  const reply = button("Ответить");
  const text = el("textarea");
  text.placeholder = "Что поправить или уточнить";
  text.hidden = true;
  const send = button("Отправить ответ", "primary");
  send.hidden = true;
  const all = [approve, reject, reply, send];
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
  reply.addEventListener("click", () => {
    text.hidden = send.hidden = false;
    reply.hidden = true;
    text.focus();
  });
  send.addEventListener("click", () => {
    if (text.value.trim()) decide({ action: "reply", text: text.value.trim() });
  });
  actions.append(approve, reject, reply);
  node.append(actions, text, send);
  return node;
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
    node.append(el("p", "hint", flags.join(" · ")));
    node.append(block("Параметры", JSON.stringify(proposal.parameters, null, 2)));
    node.append(block("Код script.py", proposal.content, true));
  } else if (proposal.kind === "agent") {
    node.append(el("p", "hint", `Инструменты: ${(proposal.tools || []).join(", ") || "нет"} · Скиллы: ${(proposal.skills || []).join(", ") || "нет"}`));
    node.append(block("Системный промпт", proposal.content, true));
  } else {
    node.append(block("Инструкции", proposal.content, true));
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

function taskNode(task, onOpen) {
  const item = el("li");
  const title = el("span", "task-title", task.error ? `${task.title} — ${task.error}` : task.title);
  item.append(el("span", `status-dot ${task.status}`), el("span", "task-agent", task.agent), title,
    el("span", `task-state ${task.status}`, STATUS[task.status] || task.status));
  item.title = `Открыть разговор с саб-агентом: ${task.title}`;
  item.addEventListener("click", () => onOpen(task.session_id));
  return item;
}

function button(label, className = "") {
  const node = el("button", className, label);
  node.type = "button";
  return node;
}
