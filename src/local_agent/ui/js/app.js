// Точка входа UI: отрисовывает экран по общему состоянию и связывает события с модулями.
import { renderMessages } from "./chat.js";
import {
  attachStreamingBubble, handleMessageKeydown, loadCatalog, loadModels, saveConfig, sendMessage, stopStreaming,
} from "./composer.js";
import { renderMemoryPanel, saveMemory, showMemory, updateMemory } from "./memory-panel.js";
import { renderPromptPanel, savePrompt, showPrompt } from "./prompt-panel.js";
import { createSession, deleteSession, loadSessions, renameSession, selectSession } from "./sessions.js";
import { renderSessions } from "./sidebar.js";
import { elements, render, selectedSession, setRenderer, state } from "./state.js";
import { initTheme } from "./theme.js";
import { showTools } from "./tools-dialog.js";

let renderedMessagesVersion = -1;

function estimateTranscriptTokens(history) {
  const bytes = history.reduce(
    (total, message) => total + new TextEncoder().encode(message.content).length,
    0,
  );
  return Math.ceil(bytes / 4);
}

function renderApp() {
  const selected = selectedSession();
  const filter = state.sessionFilter.trim().toLocaleLowerCase("ru-RU");
  const visibleSessions = filter
    ? state.sessions.filter((session) => session.title.toLocaleLowerCase("ru-RU").includes(filter))
    : state.sessions;
  elements.count.textContent = String(state.sessions.length);
  renderSessions(
    elements.list, visibleSessions, state.selectedId, selectSession, deleteSession,
    state.actionsDisabled, filter ? "Ничего не найдено" : "Пока нет сессий",
  );

  const { showingSettings, showingMemory, showingPrompt, editingTitle } = state;
  const showingPanel = showingSettings || showingMemory || showingPrompt;
  elements.settingsPanel.hidden = !showingSettings;
  elements.memoryPanel.hidden = !showingMemory;
  elements.promptPanel.hidden = !showingPrompt;
  elements.sessionPanel.hidden = showingPanel;
  const headerTitle = showingSettings
    ? "Настройки"
    : showingMemory ? "Память" : showingPrompt ? "Системный промпт" : (selected?.title || "Сессии");
  elements.editTitle.textContent = headerTitle;
  elements.editTitle.title = selected && !showingPanel ? "Переименовать сессию" : headerTitle;
  elements.editTitle.disabled = !selected || showingPanel;
  elements.tokenCount.hidden = !selected || showingPanel;
  const lastCount = selected?.context_tokens;
  elements.lastTokens.textContent = lastCount == null ? "—" : lastCount.toLocaleString("ru-RU");
  elements.lastTokens.setAttribute("aria-label", `Последний запрос: ${elements.lastTokens.textContent} токенов`);
  const totalCount = selected && state.messagesSessionId === selected.id
    ? estimateTranscriptTokens(state.messages)
    : null;
  elements.totalTokens.textContent = totalCount == null ? "—" : `≈ ${totalCount.toLocaleString("ru-RU")}`;
  elements.totalTokens.setAttribute("aria-label", `Вся переписка, приблизительно: ${elements.totalTokens.textContent} токенов`);
  elements.emptyState.hidden = Boolean(selected);
  elements.detail.hidden = !selected;
  elements.titleInput.hidden = !editingTitle || !selected || showingPanel;
  elements.editTitle.hidden = editingTitle && Boolean(selected) && !showingPanel;
  if (renderedMessagesVersion !== state.messagesVersion) {
    renderMessages(elements.messageList, state.messages);
    renderedMessagesVersion = state.messagesVersion;
  }
  attachStreamingBubble();

  if (selected) {
    if (!editingTitle) elements.titleInput.value = selected.title;
    if (document.activeElement !== elements.provider) elements.provider.value = selected.provider;
    if (document.activeElement !== elements.agent) elements.agent.value = selected.agent_id;
    if (document.activeElement !== elements.model) elements.model.value = selected.model || state.preferredModel;
    if (document.activeElement !== elements.workspace) elements.workspace.value = selected.workspace || "";
  }
  elements.saveMessage.textContent = state.streaming ? "■" : "↑";
  elements.saveMessage.title = state.streaming ? "Остановить ответ" : "Отправить";
  elements.saveMessage.setAttribute("aria-label", state.streaming ? "Остановить ответ" : "Отправить сообщение");
  elements.saveMessage.disabled = state.actionsDisabled && !state.streaming;
  elements.memoryUpdateChat.disabled = state.actionsDisabled || !selected;
  renderMemoryPanel();
  renderPromptPanel();
}

setRenderer(renderApp);

elements.newButton.addEventListener("click", createSession);
elements.toolsButton.addEventListener("click", showTools);
elements.memoryButton.addEventListener("click", showMemory);
elements.promptButton.addEventListener("click", showPrompt);
elements.toolsClose.addEventListener("click", () => elements.toolsDialog.close());
elements.emptyNewButton.addEventListener("click", createSession);
elements.search.addEventListener("input", () => {
  state.sessionFilter = elements.search.value;
  render();
});
elements.messageForm.addEventListener("submit", sendMessage);
elements.saveMessage.addEventListener("click", (event) => {
  // Во время ответа кнопка работает как «Стоп» и не отправляет форму.
  if (!state.streaming) return;
  event.preventDefault();
  stopStreaming();
});
elements.messageInput.addEventListener("keydown", handleMessageKeydown);
elements.memorySave.addEventListener("click", saveMemory);
elements.promptSave.addEventListener("click", savePrompt);
elements.memoryUpdateChat.addEventListener("click", updateMemory);
for (const field of [elements.agent, elements.provider, elements.model, elements.workspace]) {
  field.addEventListener("change", () => { void saveConfig(); });
}
elements.provider.addEventListener("change", () => { void loadModels(); });
elements.editTitle.addEventListener("click", () => {
  if (!state.selectedId || state.showingSettings || state.showingMemory || state.showingPrompt) return;
  state.editingTitle = true;
  render();
  elements.titleInput.focus();
  elements.titleInput.select();
});
elements.titleInput.addEventListener("blur", renameSession);
elements.titleInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    state.editingTitle = false;
    render();
    elements.editTitle.focus();
  } else if (event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    elements.titleInput.blur();
  }
});
elements.settingsButton.addEventListener("click", () => {
  state.showingSettings = true;
  state.showingMemory = false;
  state.showingPrompt = false;
  render();
});

initTheme();
render();
loadSessions().then(loadCatalog);
