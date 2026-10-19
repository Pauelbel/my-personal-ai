// Внимание пользователя: карточки, которые ждут решения, — в чате над полем ввода.
import { el, parse } from "./chat.js";
import { lineDiff, renderDiff } from "./diff.js";
import { effectsList, stepList } from "./steps.js";

const KIND = { tool: "Инструмент", agent: "Агент" };
const EDIT_KIND = { merge: "слить дубли", contradiction: "противоречие", outdated: "устарело", fix: "исправить" };
// Группа доверия карточки (MCP-сервер) — как её назвать на кнопке «утвердить все … в этой сессии».
const TRUST = { browser: "действия браузера", search: "запросы поиска" };

// onDecision(card, {action, text}) отправляет решение; onOpen(sessionId) открывает сессию;
// readFile(card, path) — текущий текст файла в проекте карточки или null: по нему считается разница правки.
// inChat — карточка стоит в чате своей сессии: строка «где» не нужна.
export function renderCards(root, cards, { onDecision, onOpen, readFile, busy, inChat = false }) {
  root.replaceChildren(...cards.map((card) => cardNode(card, onDecision, onOpen, readFile, busy, inChat)));
}

function cardNode(card, onDecision, onOpen, readFile, busy, inChat) {
  const pending = card.pending;
  const node = el("div", "card");
  if (!inChat) {
    const where = el("div", "where", card.session_title);
    where.title = "Открыть сессию";
    where.style.cursor = "pointer";
    where.addEventListener("click", () => onOpen(card.owner_id));
    node.append(where);
  }
  node.append(el("div", "title", pending.title || titleOf(pending)));
  // Лимит шагов за ход: продолжить работу или остановить и получить итог.
  if (pending.kind === "limit") return limitNode(node, pending, card, onDecision, busy);
  // Запуск процесса агента: поля «Нужно от вас», шаги и что они будут делать — затем «Запустить».
  if (pending.kind === "steps") return stepsNode(node, pending.run || {}, card, onDecision, busy);
  // Правки вики после lint — список с галочками: применятся только отмеченные.
  const checks = pending.kind === "wiki_lint" ? editsBody(node, pending.edits || []) : null;
  if (pending.kind === "proposal") proposalBody(node, pending.proposal || {});
  else if (pending.kind === "agent_patch") agentPatchBody(node, pending);
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

// Значения полей живут, пока карточка на экране: перерисовка панели не стирает то, что пользователь уже ввёл.
const drafts = new Map();

function stepsNode(node, run, card, onDecision, busy) {
  node.classList.add("steps-start");
  const key = card.pending.interrupt_id ?? card.session_id;
  const values = drafts.get(key) ?? { ...(run.values ?? {}) };
  drafts.set(key, values);
  if (run.description) node.append(el("p", "reason", run.description));
  const effects = effectsList(run.effects ?? [], values);
  const inputs = run.inputs ?? [];
  const fields = inputs.map((input) => {
    const label = el("label", "steps-input");
    const name = el("span", "steps-input-name", input.name);
    if (input.optional) name.append(el("span", "steps-input-optional", "необязательно"));
    const field = el("textarea");
    Object.assign(field, { rows: 1, value: values[input.name] ?? "", placeholder: input.hint || "", spellcheck: false });
    const grow = () => {
      field.style.height = "auto";
      field.style.height = `${field.scrollHeight + 2}px`;
    };
    field.addEventListener("input", () => {
      values[input.name] = field.value;
      grow();
      effects.refresh(values);
      sync();
    });
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !go.disabled) go.click();
    });
    requestAnimationFrame(grow);
    field.title = input.hint || "";
    label.append(name, field);
    return label;
  });
  if (fields.length) {
    const form = el("div", "steps-inputs");
    form.append(...fields);
    node.append(section("Нужно от вас"), form);
  }
  node.append(section(`Шаги · ${run.steps?.length ?? 0}`), stepList(run));
  if (run.effects?.length) node.append(section("Что шаги будут делать — без отдельных подтверждений на каждом шаге"), effects);
  const problems = run.problems ?? [];
  if (problems.length) {
    const list = el("ul", "steps-problems");
    for (const problem of problems) list.append(el("li", "", problem));
    node.append(section("Не запустится"), list);
  }
  const go = button("▶ Запустить", "primary");
  const reject = button("Отклонить");
  const reply = button("Ответить");
  const status = el("span", "steps-start-status");
  const text = el("textarea");
  text.placeholder = "Что поправить или уточнить";
  text.hidden = true;
  const send = button("Отправить ответ", "primary");
  send.hidden = true;
  const all = [go, reject, reply, send];
  const sync = () => {
    const missing = inputs.filter((input) => !input.optional && !String(values[input.name] ?? "").trim()).map((input) => input.name);
    go.disabled = busy || problems.length > 0 || missing.length > 0;
    status.textContent = missing.length ? `Заполните: ${missing.join(", ")}` : "";
  };
  const decide = async (decision) => {
    for (const item of all) item.disabled = true;
    drafts.delete(key);
    await onDecision(card, decision);
  };
  go.addEventListener("click", () => decide({ action: "approve", inputs: { ...values } }));
  reject.addEventListener("click", () => decide({ action: "reject" }));
  reply.addEventListener("click", () => {
    text.hidden = send.hidden = false;
    reply.hidden = true;
    text.focus();
  });
  send.addEventListener("click", () => {
    if (text.value.trim()) decide({ action: "reply", text: text.value.trim() });
  });
  for (const item of [reject, reply, send]) item.disabled = busy;
  sync();
  const actions = el("div", "actions");
  actions.append(go, reject, reply, status);
  node.append(actions, text, send);
  return node;
}

function section(title) {
  return el("div", "steps-section", title);
}

function limitNode(node, pending, card, onDecision, busy) {
  const go = button("Продолжить", "primary");
  const stop = button("Остановить");
  const all = [go, stop];
  for (const item of all) item.disabled = busy;
  const decide = async (decision) => {
    for (const item of all) item.disabled = true;
    await onDecision(card, decision);
  };
  go.addEventListener("click", () => decide({ action: "approve" }));
  stop.addEventListener("click", () => decide({ action: "reject" }));
  const actions = el("div", "actions");
  actions.append(go, stop);
  node.append(el("p", "reason", pending.text || ""), actions);
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
  if (["write_file", "edit_file"].includes(pending.call?.name)) {
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

function agentPatchBody(node, pending) {
  node.append(el("p", "reason", pending.reason || ""));
  for (const change of pending.diffs ?? []) {
    const action = { create: "Создать", update: "Изменить", delete: "Удалить" }[change.action] ?? "Изменить";
    node.append(block(`${action} ${change.path}`, change.diff, true));
  }
  node.append(el("p", "hint", `Инструменты агента: ${(pending.tools ?? []).join(", ") || "не указаны"}`));
  if (pending.effects?.length) node.append(section("Действия исправленного агента"), effectsList(pending.effects, {}));
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
    // Параметры и подсказка — так, как они лягут в tool.md; при доработке рядом — то, что сейчас, если меняется.
    node.append(block("Параметры", proposal.parameters || "нет"));
    if (proposal.previous_parameters != null && proposal.previous_parameters !== proposal.parameters) {
      node.append(block("Параметры сейчас", proposal.previous_parameters || "нет"));
    }
    node.append(block("Подсказка модели", proposal.hint || "нет"));
    if (proposal.previous_hint != null && proposal.previous_hint !== proposal.hint) {
      node.append(block("Подсказка сейчас", proposal.previous_hint || "нет"));
    }
    node.append(block("Код script.py", proposal.content, true));
    if (proposal.previous) node.append(block("Код сейчас", proposal.previous));
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

function button(label, className = "") {
  const node = el("button", className, label);
  node.type = "button";
  return node;
}
