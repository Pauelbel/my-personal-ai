// Вкладка «Агенты»: список агентов, создание, правка всех параметров и удаление в архив.
import { agentsApi, catalogApi } from "./api.js";
import { loadAgents as reloadCanvasAgents } from "./session-canvas.js";
import { loadSessions } from "./sessions.js";
import { clearError, elements, render, showError, state } from "./state.js";

// На нём работают обычные сессии: удалить его нельзя.
const DEFAULT_AGENT_ID = "default";
// Новый агент по умолчанию только читает проект; менять файлы разрешают явно.
const DEFAULT_TOOLS = ["list_files", "read_file", "search_files"];

let agents = [];
let options = { tools: [], skills: [] };
let selectedAgentId = null;
let creating = false;

export function initAgentsPanel() {
  elements.agentForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void saveAgent();
  });
  elements.agentAdd.addEventListener("click", startCreate);
  elements.agentDelete.addEventListener("click", () => { void deleteAgent(); });
  elements.agentAllSkills.addEventListener("change", syncSkills);
}

export function renderAgentsPanel() {
  const items = agents.map((agent) => listButton(
    agent.id === DEFAULT_AGENT_ID ? `${agent.name} · основной` : agent.name,
    agent.id === selectedAgentId && !creating, () => selectAgent(agent.id),
  ));
  if (creating) items.push(listButton("＋ Новый агент", true, () => {}));
  elements.promptAgents.replaceChildren(...items);
}

function listButton(text, active, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "memory-file-button";
  button.textContent = text;
  button.classList.toggle("active", active);
  button.addEventListener("click", onClick);
  return button;
}

export async function showAgents({ create = false } = {}) {
  state.showingTools = false;
  state.showingSettings = false;
  state.showingMemory = false;
  state.showingPrompt = true;
  state.showingSkills = false;
  state.editingTitle = false;
  elements.promptStatus.textContent = "";
  render();
  try {
    [agents, options] = await Promise.all([catalogApi.agents(), agentsApi.options()]);
    fillChecks();
    clearError();
  } catch (error) {
    showError(error);
    return;
  }
  if (create) {
    startCreate();
    return;
  }
  // По умолчанию открываем агента текущей сессии: его промпт и уходит в модель.
  const current = state.sessions.find((session) => session.id === state.selectedId)?.agent_id;
  selectAgent([selectedAgentId, current].find((id) => agents.some((agent) => agent.id === id)) || agents[0]?.id);
}

function selectAgent(agentId) {
  creating = false;
  selectedAgentId = agentId ?? null;
  fillForm(agents.find((agent) => agent.id === agentId) ?? null);
  render();
}

function startCreate() {
  creating = true;
  selectedAgentId = null;
  elements.promptStatus.textContent = "";
  fillForm(null);
  render();
  elements.agentName.focus();
}

function fillChecks() {
  elements.agentTools.replaceChildren(legend("Инструменты"), ...options.tools.map((tool) => check(
    "tool", tool.id, tool.name, tool.requires_approval ? "меняет файлы — каждый вызов с подтверждением" : tool.description,
  )));
  const all = elements.agentAllSkills.closest("label");
  elements.agentSkills.replaceChildren(legend("Навыки"), all, ...options.skills.map((skill) => check("skill", skill.id, skill.name, "")));
}

function legend(text) {
  const item = document.createElement("legend");
  item.textContent = text;
  return item;
}

function check(kind, value, name, hint) {
  const label = document.createElement("label");
  label.className = "agent-check";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = value;
  input.dataset.kind = kind;
  const text = document.createElement("span");
  const code = document.createElement("code");
  code.textContent = value;
  text.append(name, " ", code);
  label.append(input, text);
  if (hint) {
    const small = document.createElement("small");
    small.textContent = hint;
    label.title = hint;
    label.append(small);
  }
  return label;
}

function fillForm(agent) {
  const disabled = !agent && !creating;
  elements.promptAgentTitle.textContent = agent ? agent.name : creating ? "Новый агент" : "Агентов нет";
  elements.promptAgentFile.textContent = agent?.id === DEFAULT_AGENT_ID
    ? "Основной агент: с него начинается каждая новая сессия, сменить агента можно под полем ввода в чате. Удалить его нельзя. Файл default.md в папке агентов."
    : agent
    ? `Файл ${agent.id}.md в папке агентов.`
    : "ID — имя агента для команды: по нему соседи пишут ему через send_message. Оставьте пустым — он сгенерируется.";
  elements.agentName.value = agent?.name ?? "";
  elements.agentId.value = agent?.id ?? "";
  elements.agentId.disabled = !creating;
  elements.agentDescription.value = agent?.description ?? "";
  elements.agentRounds.value = agent?.max_tool_rounds ?? "";
  const tools = new Set(agent ? agent.tools : DEFAULT_TOOLS);
  const skills = agent?.skills ?? null;
  for (const input of elements.agentForm.querySelectorAll('input[data-kind="tool"]')) input.checked = tools.has(input.value);
  for (const input of elements.agentForm.querySelectorAll('input[data-kind="skill"]')) {
    input.checked = Boolean(skills?.includes(input.value));
  }
  elements.agentAllSkills.checked = skills === null;
  elements.promptContent.value = agent?.system_prompt ?? "";
  for (const field of elements.agentForm.querySelectorAll("input, select, textarea")) {
    if (field !== elements.agentId) field.disabled = disabled;
  }
  syncSkills();
  elements.promptSave.disabled = disabled;
  // У основного агента кнопки нет вовсе: неактивная выглядела сломанной.
  elements.agentDelete.hidden = creating || !agent || agent.id === DEFAULT_AGENT_ID;
}

function syncSkills() {
  // «Все навыки» включает и будущие, поэтому отдельные галочки тогда не нужны.
  for (const input of elements.agentForm.querySelectorAll('input[data-kind="skill"]')) {
    input.disabled = elements.agentAllSkills.checked || elements.agentAllSkills.disabled;
  }
}

function checked(kind) {
  return [...elements.agentForm.querySelectorAll(`input[data-kind="${kind}"]:checked`)].map((input) => input.value);
}

function formData() {
  return {
    name: elements.agentName.value.trim(),
    description: elements.agentDescription.value.trim(),
    max_tool_rounds: elements.agentRounds.value ? Number(elements.agentRounds.value) : null,
    tools: checked("tool"),
    skills: elements.agentAllSkills.checked ? null : checked("skill"),
    system_prompt: elements.promptContent.value,
  };
}

async function saveAgent() {
  const payload = formData();
  elements.promptSave.disabled = true;
  try {
    const saved = creating
      ? await agentsApi.create({ ...payload, id: elements.agentId.value.trim() || null })
      : await agentsApi.update(selectedAgentId, payload);
    const known = agents.some((agent) => agent.id === saved.id);
    agents = known ? agents.map((agent) => agent.id === saved.id ? saved : agent) : [...agents, saved];
    elements.promptStatus.textContent = creating
      ? `Агент «${saved.name}» создан. Его можно выбрать под полем ввода в чате и поставить на холст.`
      : "Агент сохранён. Изменения действуют со следующего сообщения.";
    selectAgent(saved.id);
    // Холст тоже должен узнать о новом агенте: его можно сразу поставить на холст.
    void reloadCanvasAgents();
  } catch (error) {
    elements.promptStatus.textContent = `Не удалось сохранить: ${error.message}`;
  } finally {
    elements.promptSave.disabled = false;
  }
}

async function deleteAgent() {
  const agent = agents.find((item) => item.id === selectedAgentId);
  if (!agent || !window.confirm(
    `Удалить агента «${agent.name}»? Файл уйдёт в архив data/agents/.deleted, а его сессии перейдут к основному агенту.`,
  )) return;
  try {
    await agentsApi.delete(agent.id);
    agents = agents.filter((item) => item.id !== agent.id);
    elements.promptStatus.textContent = `Агент «${agent.name}» удалён в архив.`;
    selectAgent(agents[0]?.id);
    await Promise.all([reloadCanvasAgents(), loadSessions()]);
  } catch (error) {
    elements.promptStatus.textContent = `Не удалось удалить: ${error.message}`;
  }
}
