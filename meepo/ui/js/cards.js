// Карточка подтверждения над полем ввода: что агент хочет сделать, diff файла и решение —
// «Да», «Нет» или ответить словами; галочка «разрешать такие до конца сессии».
import { highlightInto } from "./highlight.js";
import { el } from "./util.js";

const GROUPS = {
  files_write: "запись файлов", command: "команды", skill_run: "запуск скиллов", skill_edit: "правку скиллов",
  browser: "браузер",
};

// decide({action, text, trust, card_id}) — отправить решение; карточка блокируется до ответа сервера.
export function renderCard(root, card, decide) {
  root.replaceChildren();
  root.hidden = !card;
  if (!card) return;
  const node = el("div", "card");
  node.append(el("div", "where", card.kind === "limit" ? "лимит шагов" : card.kind === "wiki_lint" ? "проверка вики" : `✋ ${card.tool}`));
  node.append(el("div", "title", card.title || `Вызвать ${card.tool}`));
  if (card.text) node.append(el("p", "reason", card.text));
  if (card.command) node.append(el("pre", "card-command", `$ ${card.command}`));
  if (card.skill) node.append(skillNode(card.skill));
  // Правки вики после проверки: применятся только отмеченные.
  const checks = card.kind === "wiki_lint" ? editsNode(node, card.edits ?? []) : null;
  if (card.diff) node.append(diffNode(card.diff));
  else if (!card.skill && !card.command && !checks && card.tool !== "browser" && card.arguments && Object.keys(card.arguments).length) {
    const details = el("details");
    details.append(el("summary", "", "Аргументы"), el("pre", "", JSON.stringify(card.arguments, null, 2)));
    node.append(details);
  }
  const actions = el("div", "actions");
  const yes = button(card.kind === "limit" ? "Продолжить" : "Да", "primary");
  const no = button(card.kind === "limit" ? "Остановить" : "Нет");
  actions.append(yes, no);
  let trust = null;
  let reply = null;
  if (card.kind !== "limit") {
    const answer = button("Ответить текстом");
    actions.append(answer);
    reply = el("textarea");
    reply.placeholder = "Что сделать иначе — агент получит этот ответ вместо выполнения";
    reply.hidden = true;
    answer.addEventListener("click", () => {
      if (reply.hidden) {
        reply.hidden = false;
        reply.focus();
        return;
      }
      if (reply.value.trim()) send({ action: "reply", text: reply.value.trim() });
    });
    if (card.group && !checks) {
      const label = el("label", "trust");
      trust = el("input");
      trust.type = "checkbox";
      // Браузер — десятки действий подряд: первая карточка предлагает разрешить его на всю сессию.
      trust.checked = card.group === "browser";
      label.append(trust, ` разрешать ${GROUPS[card.group] ?? card.group} до конца сессии`);
      actions.append(label);
    }
  }
  const send = (decision) => {
    for (const control of node.querySelectorAll("button, input, textarea")) control.disabled = true;
    decide({ ...decision, card_id: card.id });
  };
  yes.addEventListener("click", () => send({
    action: "approve", trust: Boolean(trust?.checked),
    ...(checks ? { selected: checks.flatMap((box, index) => (box.checked ? [index + 1] : [])) } : {}),
  }));
  no.addEventListener("click", () => send({ action: "reject" }));
  node.append(actions);
  if (reply) node.append(reply);
  root.append(node);
}

function button(label, className = "") {
  const node = el("button", className, label);
  node.type = "button";
  return node;
}

// Unified diff от сервера: строки с номерами, добавленные и удалённые — цветом.
function diffNode(text) {
  const root = el("div", "diff");
  let before = 0;
  let after = 0;
  let added = 0;
  let removed = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("---") || line.startsWith("+++")) continue;
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
    if (hunk) {
      [before, after] = [Number(hunk[1]), Number(hunk[2])];
      if (root.childElementCount) root.append(el("div", "diff-skip", "⋯"));
      continue;
    }
    const sign = line[0];
    const kind = sign === "+" ? " added" : sign === "-" ? " removed" : "";
    const rowNode = el("div", `diff-line${kind}`);
    const number = sign === "-" ? before++ : sign === "+" ? after++ : (before++, after++);
    if (sign === "+") added += 1;
    if (sign === "-") removed += 1;
    rowNode.append(el("span", "diff-number", String(number)), el("span", "diff-sign", sign === "+" ? "+" : sign === "-" ? "−" : ""),
      el("span", "diff-text", line.slice(1) || " "));
    root.append(rowNode);
  }
  root.prepend(el("div", "diff-summary", text.trim() ? `+${added} −${removed}` : "без изменений"));
  return root;
}

// Новый скилл одним экраном: библиотеки, секреты и все файлы с подсветкой.
function skillNode(skill) {
  const box = el("div", "card-skill");
  const facts = [skill.requires.length && `Библиотеки: ${skill.requires.join(", ")}`,
    skill.secrets.length && `Секреты (заполните в «Настройки» → «Скиллы»):${skill.secrets.join(", ")}`].filter(Boolean);
  for (const fact of facts) box.append(el("p", "reason", fact));
  for (const [path, content] of Object.entries(skill.files)) {
    const details = el("details");
    details.open = path !== "SKILL.md";
    const pre = el("pre");
    highlightInto(pre, content, path);
    details.append(el("summary", "", path), pre);
    box.append(details);
  }
  return box;
}

const EDIT_KIND = { merge: "слить дубли", contradiction: "противоречие", outdated: "устарело", fix: "исправить" };

function editsNode(node, edits) {
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
