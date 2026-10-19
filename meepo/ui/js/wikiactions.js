// Меню «Действия ▾» чата вики: ваши проверки — название и что искать. Пункт ставит просьбу в поле ввода,
// отправляете её вы. Список хранится на сервере в data/wiki-actions.md и правится в панели над чатом.
import { api } from "./api.js";
import { el } from "./util.js";

const $ = (selector) => document.querySelector(selector);
let toast = () => {};
let open = () => {};
let fill = () => {};

export function initWikiActions(options) {
  ({ toast, open, fill } = options);
  $("#wiki-actions").addEventListener("click", () => void ($("#wiki-actions-menu").hidden ? showMenu() : closeMenu()));
  $("#wiki-actions-menu").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeMenu();
      $("#wiki-actions").focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const items = [...$("#wiki-actions-menu").querySelectorAll("button")];
      const index = items.indexOf(document.activeElement) + (event.key === "ArrowDown" ? 1 : -1);
      items[(index + items.length) % items.length]?.focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (!event.composedPath().some((node) => node instanceof Element && node.matches(".wiki-actions"))) closeMenu();
  });
  $("#wiki-actions-add").addEventListener("click", () => {
    const row = editorRow({ title: "", text: "" });
    $("#wiki-actions-rows").append(row);
    row.querySelector("input").focus();
  });
  $("#wiki-actions-cancel").addEventListener("click", closeEditor);
  $("#wiki-actions-save").addEventListener("click", () => void save());
}

// Просьба в чат вики: что искать — ваше, остальное держит карточку правок с галочками.
function request(text) {
  return `Проверь вики: ${text.replace(/[\s.]+$/, "")}. Сам ничего не меняй: покажи список правок, применяй только то, что я отмечу.`;
}

async function showMenu() {
  let actions;
  try {
    ({ actions } = await api.wikiActions());
  } catch (error) {
    toast(error);
    return;
  }
  const menu = $("#wiki-actions-menu");
  const item = (title, hint, onClick) => {
    const button = el("button", "", title);
    button.type = "button";
    button.setAttribute("role", "menuitem");
    button.title = hint;
    button.addEventListener("click", () => {
      closeMenu();
      onClick();
    });
    return button;
  };
  menu.replaceChildren(
    ...actions.map((action) => item(action.title, action.text, () => fill(request(action.text)))),
    ...(actions.length ? [el("div", "wiki-actions-sep")] : []),
    item("Изменить действия…", "Добавить, поправить или удалить действия", () => openEditor(actions)),
  );
  menu.hidden = false;
  $("#wiki-actions").setAttribute("aria-expanded", "true");
  menu.querySelector("button").focus();
}

function closeMenu() {
  $("#wiki-actions-menu").hidden = true;
  $("#wiki-actions").setAttribute("aria-expanded", "false");
}

function openEditor(actions) {
  open();
  $("#wiki-actions-rows").replaceChildren(...actions.map(editorRow));
  $("#wiki-actions-error").textContent = "";
  $("#wiki-actions-editor").hidden = false;
  $("#wiki-actions-rows").querySelector("input")?.focus();
}

function closeEditor() {
  $("#wiki-actions-editor").hidden = true;
}

function editorRow(action) {
  const row = el("div", "wiki-actions-row");
  const title = el("input");
  Object.assign(title, { value: action.title, maxLength: 60, placeholder: "Название" });
  title.setAttribute("aria-label", "Название действия");
  const remove = el("button", "ghost icon", "×");
  Object.assign(remove, { type: "button", title: "Удалить действие" });
  remove.addEventListener("click", () => row.remove());
  const text = el("textarea");
  Object.assign(text, {
    value: action.text, rows: 2, maxLength: 1000, placeholder: "Что искать, например: заметки без тегов",
  });
  text.setAttribute("aria-label", "Что искать");
  row.append(el("div", "wiki-actions-row-head"), text);
  row.firstChild.append(title, remove);
  return row;
}

async function save() {
  const actions = [...$("#wiki-actions-rows").children].map((row) => ({
    title: row.querySelector("input").value.trim(), text: row.querySelector("textarea").value.trim(),
  }));
  try {
    await api.saveWikiActions(actions);
    closeEditor();
  } catch (error) {
    $("#wiki-actions-error").textContent = error.message;
  }
}
