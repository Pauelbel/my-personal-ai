// Панель системного промпта: просмотр и правка промпта агента из его Markdown-файла.
import { agentsApi, catalogApi } from "./api.js";
import { clearError, elements, render, showError, state } from "./state.js";

let agents = [];
let selectedAgentId = null;

export function renderPromptPanel() {
  const single = agents.length < 2;
  elements.promptAgents.hidden = single;
  elements.promptWorkspace.classList.toggle("single", single);
  elements.promptAgents.replaceChildren(...agents.map((agent) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "memory-file-button";
    button.textContent = agent.name;
    button.classList.toggle("active", agent.id === selectedAgentId);
    button.addEventListener("click", () => selectAgent(agent.id));
    return button;
  }));
}

export async function showPrompt() {
  state.showingTools = false;
  state.showingSettings = false;
  state.showingMemory = false;
  state.showingPrompt = true;
  state.showingSkills = false;
  state.editingTitle = false;
  elements.promptStatus.textContent = "";
  render();
  try {
    agents = await catalogApi.agents();
    // По умолчанию открываем агента текущей сессии: его промпт и уходит в модель.
    const current = state.sessions.find((session) => session.id === state.selectedId)?.agent_id;
    selectedAgentId = [selectedAgentId, current].find((id) => agents.some((agent) => agent.id === id))
      || agents[0]?.id || null;
    render();
    if (selectedAgentId) await selectAgent(selectedAgentId);
  } catch (error) {
    showError(error);
  }
}

async function selectAgent(agentId) {
  selectedAgentId = agentId;
  render();
  try {
    const agent = await agentsApi.prompt(agentId);
    if (selectedAgentId !== agentId) return;
    showAgent(agent);
    clearError();
  } catch (error) {
    showError(error);
  }
}

function showAgent(agent) {
  elements.promptAgentTitle.textContent = agent.name;
  elements.promptAgentFile.textContent = `Хранится в файле ${agent.id}.md в папке агентов.`;
  elements.promptContent.value = agent.system_prompt;
  elements.promptContent.disabled = false;
  elements.promptSave.disabled = false;
}

export async function savePrompt() {
  if (!selectedAgentId) return;
  const agentId = selectedAgentId;
  elements.promptSave.disabled = true;
  try {
    const agent = await agentsApi.savePrompt(agentId, elements.promptContent.value);
    if (selectedAgentId === agentId) showAgent(agent);
    elements.promptStatus.textContent = "Промпт сохранён. Он действует со следующего сообщения.";
    clearError();
  } catch (error) {
    elements.promptStatus.textContent = `Не удалось сохранить: ${error.message}`;
    showError(error);
  } finally {
    elements.promptSave.disabled = false;
  }
}
