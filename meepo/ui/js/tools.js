// «Настройки» → «Инструменты»: встроенные инструменты агента по темам (файлы, поиск, команды, браузер…).
// Список тем слева; справа — что делает тема, её инструменты с параметрами и подтверждением, исходный код.
// Настройка, которая относится к одной теме, живёт в её карточке: у браузера — «Показывать окно».
import { api } from "./api.js";
import { renderMarkdown } from "./chat.js";
import { highlightInto } from "./highlight.js";
import { section } from "./skills.js";
import { treeRow } from "./tree.js";
import { el } from "./util.js";

const $ = (selector) => document.querySelector(selector);
const state = { list: [], current: null };

// Группы доверия: что подтверждает пользователь карточкой.
const GROUPS = {
  files_write: "запись файлов", command: "команды", browser: "браузер", skill_run: "запуск скиллов", skill_edit: "правка скиллов",
};

export async function showTools(toast) {
  try {
    state.list = await api.tools();
    if (!state.list.some((theme) => theme.id === state.current)) state.current = state.list[0]?.id ?? null;
    render(toast);
  } catch (error) {
    toast(error);
  }
}

function render(toast) {
  $("#tool-list").replaceChildren(...state.list.map((theme) => treeRow({
    label: theme.title, current: theme.id === state.current, note: String(theme.tools.length),
    title: theme.tools.map((tool) => tool.name).join(", "), onClick: () => {
      state.current = theme.id;
      render(toast);
    },
  })));
  const theme = state.list.find((item) => item.id === state.current);
  if (!theme) {
    $("#tool-card").replaceChildren(el("p", "hint", "Инструментов нет."));
    return;
  }
  const head = el("div", "page-head");
  head.append(el("h2", "", theme.title));
  if (theme.id === "browser") head.append(headedToggle(theme, toast));
  const about = el("div", "reader");
  renderMarkdown(about, theme.doc);
  const sources = theme.files.map(({ file, source }) => {
    const pre = el("pre", "code-view");
    highlightInto(pre, source, file);
    return section(`Исходный код · ${file}`, pre);
  });
  $("#tool-card").replaceChildren(head, el("p", "hint", theme.files.map((item) => item.file).join(" · ")),
    section("Что делает", about), section("Инструменты модели", ...theme.tools.map(toolBlock)), ...sources);
}

function toolBlock(tool) {
  const block = el("div", "tool-block");
  const title = el("div", "tool-title");
  title.append(el("code", "", tool.name), el("span", tool.group ? "tool-badge" : "tool-badge free",
    tool.group ? `карточка: ${GROUPS[tool.group] ?? tool.group}` : "без карточки"));
  const params = el("ul", "skill-packages");
  for (const [name, text] of Object.entries(tool.parameters)) {
    const row = el("li");
    row.append(el("code", "", name), text ? ` — ${text}` : "");
    params.append(row);
  }
  block.append(title, el("p", "", tool.description), params);
  return block;
}

// Окно браузера: сохраняется сразу, действует со следующего запуска браузера.
function headedToggle(theme, toast) {
  const toggle = el("label", "setting check guard-toggle");
  const box = Object.assign(el("input"), { type: "checkbox", checked: theme.settings.browser_headed });
  toggle.title = "Выключено — браузер работает без окна (headless). Действует со следующего запуска браузера.";
  toggle.append(box, el("span", "setting-name", "Показывать окно"));
  box.addEventListener("change", async () => {
    try {
      await api.saveSettings({ browser_headed: box.checked });
      theme.settings.browser_headed = box.checked;
    } catch (error) {
      box.checked = theme.settings.browser_headed;
      toast(error);
    }
  });
  return toggle;
}
