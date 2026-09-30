// Вкладка инструментов управляет доступными действиями выбранного проекта.
import { toolsApi } from "./api.js";
import { clearError, elements, render, selectedProject, showError, state } from "./state.js";

let availableTools = [];

function renderToolDialog() {
  const project = selectedProject();
  elements.toolsIntro.textContent = project
    ? `Настройка действует во всех сессиях проекта «${project.name}». Инструменты работают только внутри папки проекта; запись и правка файлов каждый раз подтверждаются.`
    : "Сессия без проекта: файлов у неё нет, поэтому инструменты модели не предлагаются. Выберите папку под полем ввода — сессия перейдёт в проект этой папки.";
  elements.toolsList.replaceChildren();
  for (const tool of availableTools) {
    const label = document.createElement("label");
    label.className = "tool-row";
    const text = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = tool.name;
    const id = document.createElement("code");
    id.className = "tool-id";
    id.textContent = tool.id;
    name.append(" ", id);
    const description = document.createElement("small");
    description.textContent = tool.description;
    text.append(name, description);
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = tool.enabled;
    input.disabled = state.actionsDisabled;
    input.setAttribute("aria-label", `Включить ${tool.name}`);
    input.addEventListener("change", async () => {
      elements.toolsList.querySelectorAll("input").forEach((item) => { item.disabled = true; });
      try {
        const updated = await toolsApi.configure(state.selectedId, tool.id, input.checked);
        availableTools = availableTools.map((item) => item.id === tool.id ? updated : item);
        clearError();
      } catch (error) {
        showError(error);
      } finally {
        renderToolDialog();
      }
    });
    label.append(text, input);
    elements.toolsList.append(label);
  }
}

export async function showTools() {
  state.showingSettings = false;
  state.showingTools = true;
  state.showingMemory = false;
  state.showingPrompt = false;
  state.showingSkills = false;
  state.editingTitle = false;
  availableTools = [];
  renderToolDialog();
  render();
  if (!state.selectedId) {
    elements.toolsIntro.textContent = "Выберите сессию проекта в боковой панели, чтобы настроить инструменты. Память, агенты и навыки — в соседних вкладках.";
    return;
  }
  const sessionId = state.selectedId;
  elements.toolsButton.disabled = true;
  try {
    const tools = await toolsApi.list(sessionId);
    if (state.selectedId !== sessionId || !state.showingTools) return;
    availableTools = tools;
    renderToolDialog();
    clearError();
  } catch (error) {
    showError(error);
  } finally {
    elements.toolsButton.disabled = false;
  }
}
