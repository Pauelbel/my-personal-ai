// «Настройки» → «Скиллы»: список слева, карточка скилла справа — SKILL.md, код script.py с подсветкой, библиотеки,
// секреты (ввод значений; обратно сервер их не отдаёт), история версий с откатом, ручная правка файлов.
import { api } from "./api.js";
import { renderMarkdown } from "./chat.js";
import { highlightInto } from "./highlight.js";
import { treeRow } from "./tree.js";
import { el } from "./util.js";

const $ = (selector) => document.querySelector(selector);
const state = { list: [], current: null, detail: null, editing: false };

export async function showSkills(toast) {
  try {
    state.list = await api.skills();
    renderList(toast);
    const name = state.list.some((skill) => skill.name === state.current) ? state.current : state.list[0]?.name;
    if (name) await openSkill(name, toast);
    else $("#skill-card").replaceChildren(el("p", "hint", "Скиллов пока нет: агент предложит новый сам, когда для задачи не хватит инструмента."));
  } catch (error) {
    toast(error);
  }
}

function renderList(toast) {
  $("#skill-list").replaceChildren(...state.list.map((skill) => treeRow({
    label: skill.name, current: skill.name === state.current, note: skill.error ? "сломан" : "", noteClass: "failed",
    title: skill.error || skill.description, onClick: () => openSkill(skill.name, toast),
  })));
}

async function openSkill(name, toast) {
  try {
    state.current = name;
    state.detail = await api.skill(name);
    state.editing = false;
    renderList(toast);
    renderCard(toast);
  } catch (error) {
    toast(error);
  }
}

export function section(title, ...children) {
  const node = el("section", "skill-section");
  node.append(el("h3", "", title), ...children);
  return node;
}

function renderCard(toast) {
  const skill = state.detail;
  const card = $("#skill-card");
  const head = el("div", "page-head");
  head.append(el("h2", "", skill.name));
  const edit = el("button", "", state.editing ? "Отмена" : "✎ Править файлы");
  edit.type = "button";
  edit.addEventListener("click", () => {
    state.editing = !state.editing;
    renderCard(toast);
  });
  head.append(edit);
  const parts = [head, el("p", "hint", `${skill.description} · таймаут ${skill.timeout} с`)];
  if (skill.error) parts.push(el("p", "error", `Скилл сломан: ${skill.error}`));
  if (state.editing) parts.push(editor(skill, toast));
  else {
    const text = el("div", "reader");
    renderMarkdown(text, skill.files["SKILL.md"].replace(/^---\n[\s\S]*?\n---\n/, ""));
    parts.push(section("SKILL.md", text));
    for (const [path, content] of Object.entries(skill.files)) {
      if (path === "SKILL.md") continue;
      const pre = el("pre", "code-view");
      highlightInto(pre, content, path);
      parts.push(section(path, pre));
    }
  }
  parts.push(section("Библиотеки", packages(skill)), section("Секреты", secrets(skill, toast)), section("История", history(skill, toast)));
  card.replaceChildren(...parts);
}

function packages(skill) {
  if (!skill.requires.length) return el("p", "hint", "Только стандартная библиотека Python.");
  const list = el("ul", "skill-packages");
  for (const item of skill.requires) {
    const row = el("li");
    row.append(el("code", "", item.spec), " ", el("span", item.installed ? "ok" : "hint",
      item.installed ? `установлен ${item.installed}` : "не установлен — поставится при запуске"));
    list.append(row);
  }
  return list;
}

function secrets(skill, toast) {
  const names = Object.keys(skill.secrets);
  if (!names.length) return el("p", "hint", "Скиллу не нужны секреты.");
  const box = el("div", "skill-secrets");
  box.append(el("p", "hint", "Значения видит только скрипт скилла: в чат, модель и ответы сервера они не попадают."));
  for (const name of names) {
    const row = el("form", "secret-row");
    const input = el("input");
    Object.assign(input, { type: "password", autocomplete: "off", placeholder: skill.secrets[name] ? "задан — введите новый, чтобы заменить" : "не задан" });
    const save = el("button", "primary", "Сохранить");
    const clear = el("button", "", "Очистить");
    clear.type = "button";
    clear.disabled = !skill.secrets[name];
    row.append(el("code", "", name), el("span", skill.secrets[name] ? "ok" : "error", skill.secrets[name] ? "задан" : "не задан"), input, save, clear);
    row.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!input.value.trim()) return;
      try {
        await api.setSecret(skill.name, name, input.value);
        await openSkill(skill.name, toast);
      } catch (error) {
        toast(error);
      }
    });
    clear.addEventListener("click", async () => {
      try {
        await api.deleteSecret(skill.name, name);
        await openSkill(skill.name, toast);
      } catch (error) {
        toast(error);
      }
    });
    box.append(row);
  }
  return box;
}

function history(skill, toast) {
  if (!skill.history.length) return el("p", "hint", "Правок ещё не было: копия появляется перед каждой правкой.");
  const list = el("ul", "skill-history");
  for (const version of skill.history) {
    const row = el("li");
    const restore = el("button", "small", "откатить");
    restore.type = "button";
    restore.addEventListener("click", async () => {
      if (!window.confirm(`Вернуть версию ${label(version)}? Текущая уйдёт в историю.`)) return;
      try {
        state.detail = await api.rollback(skill.name, version);
        renderCard(toast);
      } catch (error) {
        toast(error);
      }
    });
    row.append(el("code", "", label(version)), " ", restore);
    list.append(row);
  }
  return list;
}

// 20261019-142501 → 19.10.2026 14:25:01
function label(version) {
  const [, y, mo, d, h, mi, s] = /^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)/.exec(version) ?? [];
  return y ? `${d}.${mo}.${y} ${h}:${mi}:${s}` : version;
}

function editor(skill, toast) {
  const box = el("div", "editor");
  const fields = new Map();
  for (const [path, content] of Object.entries(skill.files)) {
    const area = el("textarea", "code");
    area.value = content;
    area.spellcheck = false;
    area.rows = Math.min(Math.max(content.split("\n").length + 1, 6), 30);
    fields.set(path, area);
    box.append(el("strong", "", path), area);
  }
  const actions = el("div", "editor-actions");
  const status = el("span", "hint", "Перед сохранением текущая версия уйдёт в историю.");
  const save = el("button", "primary", "Сохранить");
  save.type = "button";
  save.addEventListener("click", async () => {
    try {
      state.detail = await api.saveSkill(skill.name, Object.fromEntries([...fields].map(([path, area]) => [path, area.value])));
      state.editing = false;
      renderCard(toast);
    } catch (error) {
      status.textContent = error.message;
      status.className = "error";
    }
  });
  actions.append(status, save);
  box.append(actions);
  return box;
}
