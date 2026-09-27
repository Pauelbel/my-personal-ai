// Этот модуль связывает части UI и хранит только текущее состояние экрана.
import { memoryApi, messagesApi, modelsApi, sessionsApi, toolsApi, turnsApi } from "./api.js";
import { renderMessages } from "./chat.js";
import { memoryDocumentDescription, renderMemoryFiles } from "./memory.js";
import { renderSessions } from "./sidebar.js";

const elements = {
  list: document.querySelector("#session-list"),
  count: document.querySelector("#session-count"),
  newButton: document.querySelector("#new-session"),
  toolsButton: document.querySelector("#show-tools"),
  toolsDialog: document.querySelector("#tools-dialog"),
  toolsClose: document.querySelector("#close-tools"),
  toolsIntro: document.querySelector("#tools-intro"),
  toolsList: document.querySelector("#tools-list"),
  memoryButton: document.querySelector("#show-memory"),
  emptyNewButton: document.querySelector("#empty-new-session"),
  settingsButton: document.querySelector("#show-settings"),
  notice: document.querySelector("#notice"),
  tokenCount: document.querySelector("#header-tokens"),
  lastTokens: document.querySelector("#last-tokens"),
  totalTokens: document.querySelector("#total-tokens"),
  sessionPanel: document.querySelector("#session-panel"),
  settingsPanel: document.querySelector("#settings-panel"),
  memoryPanel: document.querySelector("#memory-panel"),
  memoryFiles: document.querySelector("#memory-files"),
  memoryDocumentTitle: document.querySelector("#memory-document-title"),
  memoryDocumentDescription: document.querySelector("#memory-document-description"),
  memoryContent: document.querySelector("#memory-content"),
  memorySave: document.querySelector("#save-memory"),
  memoryUpdateChat: document.querySelector("#update-memory-chat"),
  memoryStatus: document.querySelector("#memory-status"),
  emptyState: document.querySelector("#empty-state"),
  detail: document.querySelector("#session-detail"),
  editTitle: document.querySelector("#edit-title"),
  titleInput: document.querySelector("#title-input"),
  provider: document.querySelector("#session-provider"),
  model: document.querySelector("#session-model"),
  modelOptions: document.querySelector("#available-models"),
  workspace: document.querySelector("#session-workspace"),
  messageList: document.querySelector("#message-list"),
  messageForm: document.querySelector("#message-form"),
  messageInput: document.querySelector("#message-input"),
  saveMessage: document.querySelector("#save-message"),
};

let sessions = [];
let messages = [];
let messagesSessionId = null;
let preferredModel = "";
let selectedId = null;
let showingSettings = false;
let showingMemory = false;
let editingTitle = false;
let actionsDisabled = false;
let availableTools = [];
let pendingConfigSave = Promise.resolve();
let memoryDocuments = [];
let selectedMemoryName = null;

function showError(error) {
  elements.notice.textContent = error.message || "Не удалось загрузить данные";
  elements.notice.hidden = false;
}

function clearError() {
  elements.notice.hidden = true;
  elements.notice.textContent = "";
}

function estimateTranscriptTokens(history) {
  const bytes = history.reduce(
    (total, message) => total + new TextEncoder().encode(message.content).length,
    0,
  );
  return Math.ceil(bytes / 4);
}

function render() {
  const selected = sessions.find((session) => session.id === selectedId);
  elements.count.textContent = String(sessions.length);
  renderSessions(
    elements.list, sessions, selectedId, selectSession, deleteSession, actionsDisabled
  );

  elements.settingsPanel.hidden = !showingSettings;
  elements.memoryPanel.hidden = !showingMemory;
  elements.sessionPanel.hidden = showingSettings || showingMemory;
  const headerTitle = showingSettings
    ? "Settings"
    : (showingMemory ? "Память" : (selected?.title || "Сессии"));
  elements.editTitle.textContent = headerTitle;
  elements.editTitle.title = selected && !showingSettings ? "Переименовать сессию" : headerTitle;
  elements.editTitle.disabled = !selected || showingSettings || showingMemory;
  elements.tokenCount.hidden = !selected || showingSettings || showingMemory;
  const lastCount = selected?.context_tokens;
  elements.lastTokens.textContent = lastCount == null ? "—" : lastCount.toLocaleString("ru-RU");
  elements.lastTokens.setAttribute("aria-label", `Последний запрос: ${elements.lastTokens.textContent} токенов`);
  const totalCount = selected && messagesSessionId === selected.id
    ? estimateTranscriptTokens(messages)
    : null;
  elements.totalTokens.textContent = totalCount == null ? "—" : `≈ ${totalCount.toLocaleString("ru-RU")}`;
  elements.totalTokens.setAttribute("aria-label", `Вся переписка, приблизительно: ${elements.totalTokens.textContent} токенов`);
  elements.emptyState.hidden = Boolean(selected);
  elements.detail.hidden = !selected;
  elements.titleInput.hidden = !editingTitle || !selected || showingSettings || showingMemory;
  elements.editTitle.hidden = editingTitle && Boolean(selected) && !showingSettings && !showingMemory;
  renderMessages(elements.messageList, messages);

  if (selected) {
    if (!editingTitle) elements.titleInput.value = selected.title;
    elements.provider.value = selected.provider;
    elements.model.value = selected.model || preferredModel;
    elements.workspace.value = selected.workspace || "";
  }
  elements.memoryUpdateChat.disabled = actionsDisabled || !selected;
  renderMemoryFiles(elements.memoryFiles, memoryDocuments, selectedMemoryName, selectMemoryFile);
}

function selectSession(id) {
  elements.toolsDialog.close();
  selectedId = id;
  editingTitle = false;
  messages = [];
  messagesSessionId = null;
  elements.messageInput.value = "";
  showingSettings = false;
  showingMemory = false;
  render();
  loadMessages(id);
}

async function loadMessages(sessionId) {
  try {
    const loaded = await messagesApi.list(sessionId);
    if (selectedId !== sessionId) return;
    messages = loaded;
    messagesSessionId = sessionId;
    clearError();
    render();
  } catch (error) {
    if (selectedId === sessionId) showError(error);
  }
}

async function loadSessions() {
  try {
    sessions = await sessionsApi.list();
    const previousId = selectedId;
    selectedId = sessions.some((session) => session.id === selectedId)
      ? selectedId
      : (sessions[0]?.id || null);
    if (selectedId !== previousId) {
      messages = [];
      messagesSessionId = null;
    }
    clearError();
    render();
    if (selectedId) await loadMessages(selectedId);
  } catch (error) {
    showError(error);
  }
}

async function loadModels() {
  try {
    const available = await modelsApi.list();
    preferredModel = available.default_model || available.models[0] || "";
    elements.modelOptions.replaceChildren();
    for (const model of available.models) {
      const option = document.createElement("option");
      option.value = model;
      elements.modelOptions.append(option);
    }
    clearError();
    render();
  } catch (error) {
    showError(error);
  }
}

async function createSession() {
  elements.toolsDialog.close();
  elements.newButton.disabled = true;
  elements.emptyNewButton.disabled = true;
  try {
    const session = await sessionsApi.create(preferredModel);
    selectedId = session.id;
    editingTitle = false;
    messages = [];
    messagesSessionId = null;
    elements.messageInput.value = "";
    showingSettings = false;
    showingMemory = false;
    await loadSessions();
  } catch (error) {
    showError(error);
  } finally {
    elements.newButton.disabled = false;
    elements.emptyNewButton.disabled = false;
  }
}

async function renameSession() {
  if (!editingTitle) return;
  const sessionId = selectedId;
  const title = elements.titleInput.value.trim();
  const currentTitle = sessions.find((session) => session.id === sessionId)?.title;
  editingTitle = false;
  render();
  if (!sessionId || !title || title === currentTitle) return;
  try {
    const updated = await sessionsApi.rename(sessionId, title);
    sessions = sessions.map((session) => session.id === sessionId ? updated : session);
    clearError();
    render();
  } catch (error) {
    showError(error);
  }
}

async function deleteSession(sessionId) {
  const session = sessions.find((item) => item.id === sessionId);
  if (!session) return;
  const confirmed = window.confirm(
    `Удалить сессию «${session.title}» и всю её историю сообщений? Это действие нельзя отменить.`
  );
  if (!confirmed) return;
  elements.toolsDialog.close();

  actionsDisabled = true;
  renderSessions(elements.list, sessions, selectedId, selectSession, deleteSession, actionsDisabled);
  elements.saveMessage.disabled = true;
  try {
    await sessionsApi.delete(session.id);
    if (selectedId === session.id) {
      selectedId = null;
      messages = [];
      messagesSessionId = null;
      editingTitle = false;
      elements.messageInput.value = "";
    }
    await loadSessions();
  } catch (error) {
    showError(error);
  } finally {
    actionsDisabled = false;
    renderSessions(elements.list, sessions, selectedId, selectSession, deleteSession, actionsDisabled);
    elements.saveMessage.disabled = false;
  }
}

function renderToolDialog() {
  elements.toolsIntro.textContent = "Настройка общая для всех сессий. Файловые инструменты работают только внутри рабочей папки, выбранной в сессии.";
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
    input.disabled = actionsDisabled;
    input.setAttribute("aria-label", `Включить ${tool.name}`);
    input.addEventListener("change", async () => {
      elements.toolsList.querySelectorAll("input").forEach((item) => { item.disabled = true; });
      try {
        const updated = await toolsApi.configure(tool.id, input.checked);
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

async function showTools() {
  elements.toolsButton.disabled = true;
  try {
    availableTools = await toolsApi.list();
    renderToolDialog();
    elements.toolsDialog.showModal();
  } catch (error) {
    showError(error);
  } finally {
    elements.toolsButton.disabled = false;
  }
}

async function showMemory() {
  showingSettings = false;
  showingMemory = true;
  editingTitle = false;
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

async function saveMemory() {
  if (!selectedMemoryName || actionsDisabled) return;
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

async function updateMemory() {
  const sessionId = selectedId;
  if (!sessionId || actionsDisabled) {
    elements.memoryStatus.textContent = "Сначала выберите сессию.";
    return;
  }
  actionsDisabled = true;
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
    actionsDisabled = false;
    render();
  }
}

function showMemoryUpdateStatus(message) {
  elements.memoryStatus.textContent = message;
  if (!showingMemory) {
    elements.notice.textContent = message;
    elements.notice.hidden = false;
  }
}

function saveConfig() {
  const sessionId = selectedId;
  const model = elements.model.value.trim();
  if (!sessionId || !model) {
    showError(new Error("Укажите модель для сессии"));
    return Promise.resolve(false);
  }
  const config = {
    provider: elements.provider.value,
    model,
    workspace: elements.workspace.value.trim() || null,
  };
  const operation = pendingConfigSave.then(async () => {
    const current = sessions.find((session) => session.id === sessionId);
    if (!current) return false;
    if (current.provider === config.provider && current.model === config.model && current.workspace === config.workspace) {
      return true;
    }
    try {
      const updated = await sessionsApi.configure(sessionId, config);
      sessions = sessions.map((session) => session.id === sessionId ? updated : session);
      if (selectedId === sessionId && updated.context_tokens == null) {
        elements.lastTokens.textContent = "—";
        elements.lastTokens.setAttribute("aria-label", "Последний запрос: нет данных");
      }
      clearError();
      return true;
    } catch (error) {
      if (selectedId === sessionId) showError(error);
      return false;
    }
  });
  pendingConfigSave = operation.then(() => undefined);
  return operation;
}

async function saveMessage(event) {
  event.preventDefault();
  if (actionsDisabled) return;
  const content = elements.messageInput.value.trim();
  const sessionId = selectedId;
  if (!sessionId || !content) return;

  elements.saveMessage.disabled = true;
  actionsDisabled = true;
  renderSessions(elements.list, sessions, selectedId, selectSession, deleteSession, actionsDisabled);
  try {
    if (!(await saveConfig())) return;
    await turnsApi.create(sessionId, content);
    if (selectedId === sessionId) elements.messageInput.value = "";
    await loadSessions();
  } catch (error) {
    await loadSessions();
    if (error.messageSaved && selectedId === sessionId) elements.messageInput.value = "";
    showError(error);
  } finally {
    elements.saveMessage.disabled = false;
    actionsDisabled = false;
    renderSessions(elements.list, sessions, selectedId, selectSession, deleteSession, actionsDisabled);
  }
}

function handleMessageKeydown(event) {
  if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;

  if (event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault();
    const input = elements.messageInput;
    input.setRangeText("\n", input.selectionStart, input.selectionEnd, "end");
    return;
  }

  if (event.shiftKey || event.altKey || event.metaKey) return;
  event.preventDefault();
  if (!actionsDisabled && !elements.saveMessage.disabled) {
    elements.messageForm.requestSubmit(elements.saveMessage);
  }
}

elements.newButton.addEventListener("click", createSession);
elements.toolsButton.addEventListener("click", showTools);
elements.memoryButton.addEventListener("click", showMemory);
elements.toolsClose.addEventListener("click", () => elements.toolsDialog.close());
elements.emptyNewButton.addEventListener("click", createSession);
elements.messageForm.addEventListener("submit", saveMessage);
elements.messageInput.addEventListener("keydown", handleMessageKeydown);
elements.memorySave.addEventListener("click", saveMemory);
elements.memoryUpdateChat.addEventListener("click", updateMemory);
for (const field of [elements.provider, elements.model, elements.workspace]) {
  field.addEventListener("change", () => { void saveConfig(); });
}
elements.editTitle.addEventListener("click", () => {
  if (!selectedId || showingSettings || showingMemory) return;
  editingTitle = true;
  render();
  elements.titleInput.focus();
  elements.titleInput.select();
});
elements.titleInput.addEventListener("blur", renameSession);
elements.titleInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    editingTitle = false;
    render();
    elements.editTitle.focus();
  } else if (event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    elements.titleInput.blur();
  }
});
elements.settingsButton.addEventListener("click", () => {
  showingSettings = true;
  showingMemory = false;
  render();
});

const currentTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
document.documentElement.dataset.theme = currentTheme;
for (const option of document.querySelectorAll('input[name="theme"]')) {
  option.checked = option.value === currentTheme;
  option.addEventListener("change", () => {
    if (!option.checked) return;
    document.documentElement.dataset.theme = option.value;
    try { localStorage.setItem("app-theme", option.value); } catch { /* Theme still applies for this page. */ }
  });
}

render();
loadSessions().then(loadModels);
