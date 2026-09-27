// Общее состояние экрана и ссылки на элементы; модули меняют state и просят app.js перерисовать экран.
const $ = (selector) => document.querySelector(selector);

export const elements = {
  list: $("#session-list"),
  search: $("#session-search"),
  count: $("#session-count"),
  newButton: $("#new-session"),
  toolsButton: $("#show-tools"),
  toolsDialog: $("#tools-dialog"),
  toolsClose: $("#close-tools"),
  toolsIntro: $("#tools-intro"),
  toolsList: $("#tools-list"),
  memoryButton: $("#show-memory"),
  emptyNewButton: $("#empty-new-session"),
  settingsButton: $("#show-settings"),
  notice: $("#notice"),
  tokenCount: $("#header-tokens"),
  lastTokens: $("#last-tokens"),
  totalTokens: $("#total-tokens"),
  sessionPanel: $("#session-panel"),
  settingsPanel: $("#settings-panel"),
  memoryPanel: $("#memory-panel"),
  memoryFiles: $("#memory-files"),
  memoryDocumentTitle: $("#memory-document-title"),
  memoryDocumentDescription: $("#memory-document-description"),
  memoryContent: $("#memory-content"),
  memorySave: $("#save-memory"),
  memoryUpdateChat: $("#update-memory-chat"),
  memoryStatus: $("#memory-status"),
  promptButton: $("#show-prompt"),
  promptPanel: $("#prompt-panel"),
  promptWorkspace: $("#prompt-panel .prompt-workspace"),
  promptAgents: $("#prompt-agents"),
  promptAgentTitle: $("#prompt-agent-title"),
  promptAgentFile: $("#prompt-agent-file"),
  promptContent: $("#prompt-content"),
  promptSave: $("#save-prompt"),
  promptStatus: $("#prompt-status"),
  emptyState: $("#empty-state"),
  detail: $("#session-detail"),
  editTitle: $("#edit-title"),
  titleInput: $("#title-input"),
  agentField: $("#agent-field"),
  agent: $("#session-agent"),
  provider: $("#session-provider"),
  model: $("#session-model"),
  modelOptions: $("#available-models"),
  workspace: $("#session-workspace"),
  messageList: $("#message-list"),
  messageForm: $("#message-form"),
  messageInput: $("#message-input"),
  saveMessage: $("#save-message"),
};

export const state = {
  sessions: [],
  messages: [],
  messagesSessionId: null,
  preferredModel: "",
  selectedId: null,
  showingSettings: false,
  showingMemory: false,
  showingPrompt: false,
  editingTitle: false,
  actionsDisabled: false,
  sessionFilter: "",
  // Идёт ли сейчас ответ модели: тогда кнопка отправки превращается в «Стоп».
  streaming: false,
  // Растёт при каждой замене списка сообщений: чат перерисовывается только тогда, а не на каждый токен.
  messagesVersion: 0,
};

export function setMessages(messages, sessionId = state.messagesSessionId) {
  state.messages = messages;
  state.messagesSessionId = sessionId;
  state.messagesVersion += 1;
}

let renderer = () => {};

export function setRenderer(callback) {
  renderer = callback;
}

export function render() {
  renderer();
}

export function selectedSession() {
  return state.sessions.find((session) => session.id === state.selectedId);
}

export function showError(error) {
  elements.notice.textContent = error.message || "Не удалось загрузить данные";
  elements.notice.hidden = false;
}

export function clearError() {
  elements.notice.hidden = true;
  elements.notice.textContent = "";
}
