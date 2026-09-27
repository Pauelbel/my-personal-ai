// Диалог инструментов включает и выключает инструменты текущей сессии.
import { toolsApi } from "./api.js";
import { clearError, elements, showError, state } from "./state.js";

let availableTools = [];

function renderToolDialog() {
  elements.toolsIntro.textContent = "Настройка действует только для текущей сессии. Файловые инструменты работают только внутри её рабочей папки; запись файла каждый раз подтверждается.";
  elements.toolsList.replaceChildren();
  for (const tool of availableTools) {
    const label = document.createElement("label");
    label.className = "tool-row";
    const text = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = tool.name;
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
  if (!state.selectedId) {
    showError(new Error("Выберите сессию, чтобы настроить её инструменты"));
    return;
  }
  elements.toolsButton.disabled = true;
  try {
    availableTools = await toolsApi.list(state.selectedId);
    renderToolDialog();
    elements.toolsDialog.showModal();
  } catch (error) {
    showError(error);
  } finally {
    elements.toolsButton.disabled = false;
  }
}
