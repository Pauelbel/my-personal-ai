// Точка входа UI: отрисовывает экран по общему состоянию и связывает события с модулями.
import { renderMessages } from "./chat.js";
import {
  attachStreamingBubble, chooseWorkspace, handleMessageKeydown, loadCatalog, loadModels, saveConfig, sendMessage,
  stopStreaming, renderTurnRecovery, retryTurn,
} from "./composer.js";
import { initFolderPicker } from "./folder-picker.js";
import { renderMemoryPanel, saveMemory, showMemory, updateMemory } from "./memory-panel.js";
import { initProjectDialog, openProjectDialog } from "./projects.js";
import { renderPromptPanel, savePrompt, showPrompt } from "./prompt-panel.js";
import { createSession, deleteSession, loadSessions, renameSession, selectSession } from "./sessions.js";
import { deleteSkill, initSkillDialog, renderSkillsPanel, saveSkill, setSkillEditing, showSkills } from "./skills-panel.js";
import { renderSidebar } from "./sidebar.js";
import {
  elements, render, selectedProject, selectedSession, setRenderer, state, toggleProject,
} from "./state.js";
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
  elements.count.textContent = String(state.sessions.length);
  renderSidebar(elements.list, {
    projects: state.projects,
    sessions: state.sessions,
    filter,
    selectedId: state.selectedId,
    collapsed: state.collapsedProjects,
    actionsDisabled: state.actionsDisabled,
    emptyText: filter ? "Ничего не найдено" : "Пока нет сессий",
    onSelect: selectSession,
    onDelete: deleteSession,
    onCreate: (projectId) => createSession(projectId),
    onEdit: openProjectDialog,
    onToggle: toggleProject,
  });

  const { showingSettings, showingTools, showingMemory, showingPrompt, showingSkills, editingTitle } = state;
  const showingCustomization = showingTools || showingMemory || showingPrompt || showingSkills;
  const showingPanel = showingSettings || showingCustomization;
  elements.customizationPanel.hidden = !showingCustomization;
  elements.toolsPanel.hidden = !showingTools;
  elements.customizationButton.classList.toggle("active", showingCustomization);
  for (const [button, active] of [[elements.toolsButton, showingTools], [elements.memoryButton, showingMemory],
    [elements.promptButton, showingPrompt], [elements.skillsButton, showingSkills]]) {
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  elements.settingsPanel.hidden = !showingSettings;
  elements.memoryPanel.hidden = !showingMemory;
  elements.promptPanel.hidden = !showingPrompt;
  elements.skillsPanel.hidden = !showingSkills;
  elements.sessionPanel.hidden = showingPanel;
  const headerTitle = showingSettings
    ? "Настройки"
    : showingCustomization ? "Кастомизация"
    : (selected?.title || "Сессии");
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
  renderTurnRecovery();

  if (selected) {
    if (!editingTitle) elements.titleInput.value = selected.title;
    if (document.activeElement !== elements.provider) elements.provider.value = selected.provider;
    if (document.activeElement !== elements.agent) elements.agent.value = selected.agent_id;
    if (document.activeElement !== elements.model) elements.model.value = selected.model || state.preferredModel;
  }
  const project = selectedProject();
  elements.workspaceLabel.textContent = project ? project.name : "Выбрать папку…";
  elements.workspace.title = project
    ? `Проект «${project.name}»: ${project.workspace}. Нажмите, чтобы открыть настройки проекта`
    : "Сессия без проекта: файлы недоступны. Выберите папку — сессия перейдёт в проект этой папки";
  elements.workspace.disabled = !selected || state.streaming;
  elements.saveMessage.textContent = state.streaming ? "■" : "↑";
  elements.saveMessage.title = state.streaming ? "Остановить ответ" : "Отправить";
  elements.saveMessage.setAttribute("aria-label", state.streaming ? "Остановить ответ" : "Отправить сообщение");
  elements.saveMessage.disabled = state.actionsDisabled && !state.streaming;
  elements.memoryUpdateChat.disabled = state.actionsDisabled || !selected;
  renderMemoryPanel();
  renderPromptPanel();
  renderSkillsPanel();
}

setRenderer(renderApp);

elements.newButton.addEventListener("click", () => createSession());
elements.newProjectButton.addEventListener("click", () => openProjectDialog());
elements.workspace.addEventListener("click", () => { void chooseWorkspace(); });
elements.toolsButton.addEventListener("click", showTools);
elements.customizationButton.addEventListener("click", showTools);
elements.memoryButton.addEventListener("click", showMemory);
elements.promptButton.addEventListener("click", showPrompt);
elements.skillsButton.addEventListener("click", showSkills);
elements.emptyNewButton.addEventListener("click", () => createSession());
elements.search.addEventListener("input", () => {
  state.sessionFilter = elements.search.value;
  render();
});
elements.messageForm.addEventListener("submit", sendMessage);
elements.retryTurn.addEventListener("click", retryTurn);
elements.saveMessage.addEventListener("click", (event) => {
  // Во время ответа кнопка работает как «Стоп» и не отправляет форму.
  if (!state.streaming) return;
  event.preventDefault();
  stopStreaming();
});
elements.messageInput.addEventListener("keydown", handleMessageKeydown);
elements.memorySave.addEventListener("click", saveMemory);
elements.promptSave.addEventListener("click", savePrompt);
elements.skillSave.addEventListener("click", saveSkill);
elements.skillView.addEventListener("click", () => setSkillEditing(false));
elements.skillEdit.addEventListener("click", () => setSkillEditing(true));
elements.skillDelete.addEventListener("click", deleteSkill);
elements.memoryUpdateChat.addEventListener("click", updateMemory);
for (const field of [elements.agent, elements.provider, elements.model]) {
  field.addEventListener("change", () => { void saveConfig(); });
}
elements.provider.addEventListener("change", () => { void loadModels(); });
elements.editTitle.addEventListener("click", () => {
  if (!state.selectedId || state.showingSettings || state.showingTools || state.showingMemory || state.showingPrompt || state.showingSkills) return;
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
  state.showingTools = false;
  state.showingMemory = false;
  state.showingPrompt = false;
  state.showingSkills = false;
  render();
});

initTheme();
initFolderPicker();
initProjectDialog();
initSkillDialog();
render();
loadSessions().then(async () => {
  await loadCatalog();
  // После восстановления сессии загружаем данные открытой до обновления вкладки.
  if (state.showingTools) await showTools();
  else if (state.showingMemory) await showMemory();
  else if (state.showingPrompt) await showPrompt();
  else if (state.showingSkills) await showSkills();
});
