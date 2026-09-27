// Панель памяти: просмотр и правка Markdown-файлов и ручное обновление по текущей сессии.
import { memoryApi } from "./api.js";
import { memoryDocumentDescription, renderMemoryFiles } from "./memory.js";
import { clearError, elements, render, showError, state } from "./state.js";

let memoryDocuments = [];
let selectedMemoryName = null;

export function renderMemoryPanel() {
  renderMemoryFiles(elements.memoryFiles, memoryDocuments, selectedMemoryName, selectMemoryFile);
}

export async function showMemory() {
  state.showingSettings = false;
  state.showingMemory = true;
  state.showingPrompt = false;
  state.showingSkills = false;
  state.editingTitle = false;
  render();
  try {
    memoryDocuments = await memoryApi.list();
    selectedMemoryName = memoryDocuments.some((item) => item.name === selectedMemoryName)
      ? selectedMemoryName
      : (memoryDocuments[0]?.name || null);
    render();
    if (selectedMemoryName) await selectMemoryFile(selectedMemoryName);
  } catch (error) {
    showError(error);
  }
}

async function selectMemoryFile(name) {
  selectedMemoryName = name;
  render();
  try {
    const document = await memoryApi.read(name);
    if (selectedMemoryName !== name) return;
    elements.memoryDocumentTitle.textContent = document.title;
    elements.memoryDocumentDescription.textContent = memoryDocumentDescription(document.name);
    elements.memoryContent.value = document.content;
    elements.memoryContent.disabled = false;
    elements.memorySave.disabled = false;
    clearError();
  } catch (error) {
    showError(error);
  }
}

export async function saveMemory() {
  if (!selectedMemoryName || state.actionsDisabled) return;
  elements.memorySave.disabled = true;
  try {
    const document = await memoryApi.save(selectedMemoryName, elements.memoryContent.value);
    elements.memoryContent.value = document.content;
    elements.memoryStatus.textContent = "Изменения сохранены.";
    memoryDocuments = await memoryApi.list();
    clearError();
    render();
  } catch (error) {
    showError(error);
  } finally {
    elements.memorySave.disabled = false;
  }
}

export async function updateMemory() {
  const sessionId = state.selectedId;
  if (!sessionId || state.actionsDisabled) {
    elements.memoryStatus.textContent = "Сначала выберите сессию.";
    return;
  }
  state.actionsDisabled = true;
  showMemoryUpdateStatus("Память обновляется для текущей сессии…");
  render();
  try {
    const result = await memoryApi.update(sessionId);
    const status = result.processed_messages === 0
      ? "Новых сообщений для обработки нет."
      : `Готово: обработано сообщений — ${result.processed_messages}, изменений — ${result.applied_operations}.`;
    memoryDocuments = await memoryApi.list();
    if (selectedMemoryName) await selectMemoryFile(selectedMemoryName);
    clearError();
    showMemoryUpdateStatus(status);
  } catch (error) {
    showMemoryUpdateStatus(`Ошибка обновления: ${error.message}`);
    showError(error);
  } finally {
    state.actionsDisabled = false;
    render();
  }
}

function showMemoryUpdateStatus(message) {
  elements.memoryStatus.textContent = message;
  if (!state.showingMemory) {
    elements.notice.textContent = message;
    elements.notice.hidden = false;
  }
}
