// Окно «Настройки»: провайдеры моделей (Ollama, LM Studio, OpenAI-совместимый API с ключом), проверка подключения
// со списком моделей, модель по умолчанию и правила основного агента. Провайдеры из .env только показываются —
// меняются в .env.
import { api } from "./api.js";
import { el } from "./chat.js";

const $ = (selector) => document.querySelector(selector);
const SEP = "\u0000";
// Столько моделей показать после проверки; остальные — числом.
const SHOWN_MODELS = 24;
let config = { kinds: {}, default_provider: "", default_model: "" };
let editing = null;
let changed = null;

// onChanged — провайдеры или модели поменялись: список моделей над полем ввода нужно собрать заново.
export function initProviders({ onChanged }) {
  changed = onChanged;
  for (const id of ["#providers-open", "#settings-open"]) $(id).addEventListener("click", openProviders);
  $("#providers-close").addEventListener("click", () => $("#providers-dialog").close());
  $("#provider-add").addEventListener("click", () => openForm(null));
  $("#provider-cancel").addEventListener("click", closeForm);
  $("#provider-check").addEventListener("click", check);
  $("#provider-form").addEventListener("submit", save);
  $("#provider-kind").addEventListener("change", syncKind);
  $("#provider-default").addEventListener("change", saveDefault);
  $("#rules-save").addEventListener("click", saveRules);
  $("#rules-text").addEventListener("input", () => { $("#rules-status").textContent = ""; });
}

async function openProviders() {
  closeForm();
  $("#providers-note").textContent = "";
  $("#rules-status").textContent = "";
  $("#providers-dialog").showModal();
  await Promise.all([refresh(), loadRules()]);
}

// Правила основного агента — поверх встроенного промпта; сам промпт только показывается.
async function loadRules() {
  try {
    const rules = await api.instructions();
    $("#rules-text").value = rules.instructions;
    $("#rules-builtin").textContent = rules.builtin;
  } catch (error) {
    $("#rules-status").textContent = error.message;
  }
}

async function saveRules() {
  try {
    $("#rules-text").value = (await api.saveInstructions($("#rules-text").value)).instructions;
    $("#rules-status").textContent = "Сохранено: действуют со следующего ответа";
  } catch (error) {
    $("#rules-status").textContent = error.message;
  }
}

async function refresh() {
  let providers = [];
  try {
    [providers, config] = await Promise.all([api.providers(), api.providerConfig()]);
  } catch (error) {
    $("#providers-note").textContent = error.message;
  }
  $("#provider-kind").replaceChildren(...Object.entries(config.kinds).map(([kind, info]) => new Option(info.name, kind)));
  // Модели каждого провайдера — сразу: по ним видно, какой сервер сейчас доступен.
  const lists = await Promise.all(providers.map((provider) => api.models(provider.id).then((list) => list.models, () => null)));
  $("#provider-list").replaceChildren(...providers.map((provider, index) => providerRow(provider, lists[index])));
  renderDefault(providers, lists);
}

function providerRow(provider, models) {
  const row = el("li", "provider-row");
  const text = el("div", "provider-text");
  const kind = provider.kind === "env" ? "из .env" : config.kinds[provider.kind]?.name ?? provider.kind;
  text.append(
    el("span", "provider-name", provider.name),
    el("span", "provider-meta", [kind, provider.base_url, provider.key && `ключ ${provider.key}`].filter(Boolean).join(" · ")),
  );
  const state = el("span", `provider-state ${models ? "ok" : "off"}`, models ? `${models.length} мод.` : "недоступен");
  state.title = models ? models.slice(0, 40).join("\n") : "Сервер не ответил: выключен, неверный адрес или ключ";
  row.append(text, state);
  if (provider.editable) {
    const edit = button("Изменить", () => openForm(provider));
    const remove = button("Удалить", () => removeProvider(provider));
    row.append(edit, remove);
  }
  return row;
}

function renderDefault(providers, lists) {
  const select = $("#provider-default");
  const groups = providers.flatMap((provider, index) => {
    if (!lists[index]?.length) return [];
    const group = document.createElement("optgroup");
    group.label = provider.name;
    group.append(...lists[index].map((model) => new Option(model, `${provider.id}${SEP}${model}`)));
    return [group];
  });
  const current = `${config.default_provider}${SEP}${config.default_model}`;
  select.replaceChildren(...groups);
  if (config.default_model && ![...select.options].some((option) => option.value === current)) {
    // Модели по умолчанию сейчас нет: сервер выключен — показываем выбор, а не подменяем его.
    select.prepend(new Option(`${config.default_model} — сейчас недоступна`, current));
  }
  if (!select.options.length) select.append(new Option("Нет доступных моделей", ""));
  select.value = current;
  select.disabled = !groups.length;
}

async function saveDefault() {
  const [provider, model] = $("#provider-default").value.split(SEP);
  if (!model) return;
  try {
    await api.setDefaultModel(provider, model);
    $("#providers-note").textContent = `По умолчанию: ${model}`;
  } catch (error) {
    $("#providers-note").textContent = error.message;
  }
}

function openForm(provider) {
  editing = provider;
  $("#provider-form-title").textContent = provider ? `Провайдер «${provider.name}»` : "Новый провайдер";
  $("#provider-kind").value = provider?.kind ?? "openai";
  $("#provider-name").value = provider?.name ?? "";
  $("#provider-url").value = provider?.base_url ?? "";
  $("#provider-key").value = "";
  $("#provider-status").textContent = "";
  $("#provider-error").textContent = "";
  $("#provider-models").hidden = true;
  syncKind();
  $("#provider-form").hidden = false;
  $("#provider-add").hidden = true;
  $("#provider-name").focus();
}

function closeForm() {
  editing = null;
  $("#provider-form").hidden = true;
  $("#provider-add").hidden = false;
}

// Тип подставляет адрес по умолчанию (если адрес не меняли) и подсказывает, нужен ли ключ.
function syncKind() {
  const kind = $("#provider-kind").value;
  const url = $("#provider-url");
  const defaults = Object.values(config.kinds).map((info) => info.base_url);
  if (!url.value || defaults.includes(url.value)) url.value = config.kinds[kind]?.base_url ?? "";
  if (!$("#provider-name").value || Object.values(config.kinds).some((info) => info.name === $("#provider-name").value)) {
    $("#provider-name").value = config.kinds[kind]?.name ?? "";
  }
  const key = $("#provider-key");
  key.placeholder = editing?.key
    ? `сохранён ${editing.key} — оставьте пустым, чтобы не менять`
    : kind === "openai" ? "sk-… — если сервер требует ключ" : "не нужен";
}

function fields() {
  const key = $("#provider-key").value.trim();
  return {
    name: $("#provider-name").value.trim(),
    kind: $("#provider-kind").value,
    base_url: $("#provider-url").value.trim(),
    // Пустой ключ при правке — оставить сохранённый.
    api_key: key || (editing ? undefined : ""),
  };
}

async function check() {
  const status = $("#provider-status");
  const list = $("#provider-models");
  status.textContent = "Подключаюсь…";
  list.hidden = true;
  $("#provider-check").disabled = true;
  try {
    const { models } = await api.probeProvider({ ...fields(), id: editing?.id });
    status.textContent = models.length ? `Подключено · моделей: ${models.length}` : "Подключено, но моделей нет";
    list.replaceChildren(
      ...models.slice(0, SHOWN_MODELS).map((model) => el("span", "provider-model", model)),
      ...(models.length > SHOWN_MODELS ? [el("span", "hint", `и ещё ${models.length - SHOWN_MODELS}`)] : []),
    );
    list.hidden = !models.length;
  } catch (error) {
    status.textContent = `Не подключилось: ${error.message}`;
  } finally {
    $("#provider-check").disabled = false;
  }
}

async function save(event) {
  event.preventDefault();
  try {
    if (editing) await api.updateProvider(editing.id, fields());
    else await api.createProvider(fields());
    closeForm();
    await refresh();
    await changed?.();
  } catch (error) {
    $("#provider-error").textContent = error.message;
  }
}

async function removeProvider(provider) {
  if (!window.confirm(`Удалить провайдера «${provider.name}»? Сессии с его моделями останутся, но модель придётся выбрать заново.`)) return;
  try {
    await api.deleteProvider(provider.id);
    await refresh();
    await changed?.();
  } catch (error) {
    $("#providers-note").textContent = error.message;
  }
}

function button(label, onClick) {
  const node = el("button", "", label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}
