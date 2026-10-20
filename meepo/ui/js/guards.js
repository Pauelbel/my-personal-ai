// «Настройки» → «Заплатки»: заплатки для маленькой модели. Список слева, справа — переключатель, описание
// из docstring файла и исходный код с подсветкой. Переключатель сохраняется сразу и действует со следующего хода.
import { api } from "./api.js";
import { renderMarkdown } from "./chat.js";
import { highlightInto } from "./highlight.js";
import { section } from "./skills.js";
import { treeRow } from "./tree.js";
import { el } from "./util.js";

const $ = (selector) => document.querySelector(selector);
const state = { list: [], current: null };

export async function showGuards(toast) {
  try {
    state.list = await api.guards();
    if (!state.list.some((guard) => guard.name === state.current)) state.current = state.list[0]?.name ?? null;
    render(toast);
  } catch (error) {
    toast(error);
  }
}

function render(toast) {
  $("#guard-list").replaceChildren(...state.list.map((guard) => treeRow({
    label: guard.title, current: guard.name === state.current, note: guard.enabled ? "" : "выключена",
    title: guard.file, onClick: () => {
      state.current = guard.name;
      render(toast);
    },
  })));
  const guard = state.list.find((item) => item.name === state.current);
  const card = $("#guard-card");
  if (!guard) {
    card.replaceChildren(el("p", "hint", "Заплаток нет."));
    return;
  }
  const head = el("div", "page-head");
  const toggle = el("label", "setting check guard-toggle");
  const box = Object.assign(el("input"), { type: "checkbox", checked: guard.enabled });
  toggle.append(box, el("span", "setting-name", "Включена"));
  box.addEventListener("change", () => save(guard, box, toast));
  head.append(el("h2", "", guard.title), toggle);
  const about = el("div", "reader");
  renderMarkdown(about, guard.doc);
  const code = el("pre", "code-view");
  highlightInto(code, guard.source, guard.file);
  card.replaceChildren(head, el("p", "hint", `${guard.name} · ${guard.file}`), section("Что ловит", about),
    section(`Исходный код · ${guard.file}`, code));
}

// На сервер уходят все переключатели разом: настройки guards сохраняются целым словарём.
async function save(guard, box, toast) {
  const guards = Object.fromEntries(state.list.map((item) => [item.name, item.name === guard.name ? box.checked : item.enabled]));
  try {
    await api.saveSettings({ guards });
    guard.enabled = box.checked;
  } catch (error) {
    box.checked = guard.enabled;
    toast(error);
  }
  render(toast);
}
