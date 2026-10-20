// Раздел «Настройки»: сервер модели и модель, окно по умолчанию, лимит шагов, guards, браузер, память.
// Всё хранится в data/settings.json и действует со следующего хода.
import { api } from "./api.js";
import { el } from "./util.js";

const $ = (selector) => document.querySelector(selector);

// Локальные серверы с OpenAI-совместимым API: подставляются выбором, свой адрес — вручную.
const SERVERS = { lmstudio: ["LM Studio", "http://127.0.0.1:1234/v1"], ollama: ["Ollama", "http://127.0.0.1:11434/v1"] };

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

function option(value, label) {
  return Object.assign(el("option"), { value, textContent: label });
}

// Сервер: из .env, LM Studio, Ollama или свой адрес; модель — из списка моделей выбранного сервера.
function modelBlock(settings) {
  const kind = el("select");
  kind.append(option("env", `Из .env — ${settings.env_base_url}`),
    ...Object.entries(SERVERS).map(([key, [name, url]]) => option(key, `${name} — ${url}`)), option("custom", "Другой адрес…"));
  const preset = Object.entries(SERVERS).find(([, [, url]]) => url === settings.llm_base_url.replace(/\/$/, ""));
  kind.value = !settings.llm_base_url ? "env" : preset ? preset[0] : "custom";
  const custom = input("text", kind.value === "custom" ? settings.llm_base_url : "", { placeholder: "http://адрес:порт/v1" });
  const check = el("button", "", "Проверить подключение");
  check.type = "button";
  const probe = el("span", "hint");
  const model = el("select");
  const url = () => (kind.value === "env" ? "" : kind.value === "custom" ? custom.value.trim() : SERVERS[kind.value][1]);
  const load = async () => {
    custom.hidden = kind.value !== "custom";
    const chosen = model.value || settings.model;
    probe.textContent = "Проверяю…";
    probe.className = "hint";
    let names = [];
    try {
      names = await api.models(url() || settings.env_base_url);
      probe.textContent = `✓ Сервер доступен, моделей: ${names.length}`;
      probe.className = "ok";
    } catch (error) {
      probe.textContent = `✕ ${error.message}`;
      probe.className = "error";
    }
    model.replaceChildren(option("", `Из .env — ${settings.env_model || "не задана"}`), ...names.map((name) => option(name, name)));
    // Сохранённой модели нет на этом сервере: показываем это, а не подставляем молча другую.
    if (chosen && !names.includes(chosen)) model.append(option(chosen, `${chosen} — нет на сервере`));
    model.value = chosen;
  };
  kind.addEventListener("change", load);
  check.addEventListener("click", load);
  custom.addEventListener("change", load);
  const server = el("div", "setting-row");
  server.append(kind, check);
  const nodes = [
    field("Сервер модели", server, "LM Studio или Ollama на этом компьютере; адрес — с /v1 на конце."), custom, probe,
    field("Модель", model, `Сейчас: ${settings.active_model || "не выбрана"}. Её же можно сменить над полем ввода чата.`),
  ];
  void load();
  return { nodes, values: () => ({ llm_base_url: url(), model: model.value }) };
}

function render(settings, toast, onSaved) {
  const block = modelBlock(settings);
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
        ...block.values(),
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
    ...block.nodes,
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
}
