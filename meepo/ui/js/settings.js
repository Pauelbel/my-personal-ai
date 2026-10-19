// Раздел «Настройки»: сервер модели и модель, окно по умолчанию, лимит шагов, guards, браузер, память.
// Всё хранится в data/settings.json и действует со следующего хода.
import { api } from "./api.js";
import { el } from "./util.js";

const $ = (selector) => document.querySelector(selector);

const GUARDS = {
  language: ["Русский язык", "Ответ не по-русски (а вы не просили другой язык) — модель переписывает его. До 3 раз за ход."],
  promise: ["Пообещал и не сделал", "Ответ кончается обещанием действия без вызова инструмента — модель делает шаг. До 3 раз за ход."],
  plan: ["План задачи", "В плане хода остались несделанные пункты — модель продолжает со следующего. До 4 раз за ход."],
  retry: ["Повтор", "Пустой ответ или зацикливание — тот же запрос ещё раз, затем без инструментов, затем ошибка."],
};

export async function showSettings(toast, onSaved) {
  try {
    render(await api.settings(), toast, onSaved);
  } catch (error) {
    toast(error);
  }
}

function field(label, input, hint = "") {
  const node = el("label", "setting");
  node.append(el("span", "setting-name", label), input);
  if (hint) node.append(el("span", "hint", hint));
  return node;
}

function input(type, value, attributes = {}) {
  const node = el("input");
  Object.assign(node, { type, value: value ?? "", ...attributes });
  return node;
}

function render(settings, toast, onSaved) {
  const server = input("text", settings.llm_base_url, { placeholder: settings.env_base_url });
  const model = input("text", settings.model, { placeholder: settings.env_model || "id модели" });
  model.setAttribute("list", "model-options");
  const options = el("datalist");
  options.id = "model-options";
  const loadModels = async () => {
    try {
      options.replaceChildren(...(await api.models()).map((name) => Object.assign(el("option"), { value: name })));
      status.textContent = "";
    } catch (error) {
      status.textContent = error.message;
    }
  };
  const windowSize = input("number", settings.default_context_tokens, { min: 1024, step: 1024 });
  const rounds = input("number", settings.max_tool_rounds, { min: 1, max: 200 });
  const memory = input("number", settings.memory_auto_update_messages, { min: 0 });
  const headed = input("checkbox", "", { checked: settings.browser_headed });
  const guards = Object.fromEntries(Object.keys(GUARDS).map((name) => [name, input("checkbox", "", { checked: settings.guards[name] })]));
  const status = el("span", "hint");
  const save = el("button", "primary", "Сохранить");
  save.type = "button";
  save.addEventListener("click", async () => {
    try {
      const saved = await api.saveSettings({
        llm_base_url: server.value.trim(), model: model.value.trim(),
        default_context_tokens: Number(windowSize.value), max_tool_rounds: Number(rounds.value),
        memory_auto_update_messages: Number(memory.value), browser_headed: headed.checked,
        guards: Object.fromEntries(Object.entries(guards).map(([name, box]) => [name, box.checked])),
      });
      status.textContent = "Сохранено: действует со следующего хода.";
      onSaved(saved);
    } catch (error) {
      status.textContent = error.message;
    }
  });
  const guardRows = Object.entries(GUARDS).map(([name, [title, about]]) => {
    const row = el("label", "setting check");
    row.append(guards[name], el("span", "setting-name", title), el("span", "hint", about));
    return row;
  });
  const actions = el("div", "editor-actions");
  actions.append(status, save);
  const headedRow = el("label", "setting check");
  headedRow.append(headed, el("span", "setting-name", "Показывать окно браузера"), el("span", "hint", "Выключено — браузер работает без окна (headless)."));
  $("#settings").replaceChildren(
    el("h3", "", "Модель"),
    field("Сервер модели", server, `Пусто — из .env: ${settings.env_base_url}. LM Studio — http://127.0.0.1:1234/v1, Ollama — http://127.0.0.1:11434/v1.`),
    field("Модель", model, `Пусто — из .env: ${settings.env_model || "не задана"}. Сейчас: ${settings.active_model || "не выбрана"}.`),
    options,
    field("Окно по умолчанию, токенов", windowSize, "Если сервер модели сам не сообщает размер окна."),
    field("Лимит шагов за ход", rounds, "Дальше — карточка «продолжить или остановить»."),
    el("h3", "", "Заплатки для маленькой модели"),
    el("p", "hint", "Каждая срабатывает в логе хода строкой «Guard …»."),
    ...guardRows,
    el("h3", "", "Браузер"),
    headedRow,
    el("h3", "", "Память"),
    field("Обновлять память каждые N сообщений", memory, "0 — только вручную, кнопкой в разделе «Память»."),
    actions,
  );
  void loadModels();
}
