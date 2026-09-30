// Общее состояние экрана и ссылки на элементы; модули меняют state и просят app.js перерисовать экран.
const $ = (selector) => document.querySelector(selector);
const initialView = loadView();

export const elements = {
  list: $("#session-list"),
  sidebar: $(".sidebar"),
  sidebarResizer: $("#sidebar-resizer"),
  appShell: $(".app-shell"),
  newButton: $("#new-session"),
  toolsButton: $("#show-tools"),
  customizationButton: $("#show-customization"),
  customizationPanel: $("#customization-panel"),
  toolsPanel: $("#tools-panel"),
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
  promptAgents: $("#prompt-agents"),
  promptAgentTitle: $("#prompt-agent-title"),
  promptAgentFile: $("#prompt-agent-file"),
  promptContent: $("#prompt-content"),
  promptSave: $("#save-prompt"),
  promptStatus: $("#prompt-status"),
  agentAdd: $("#add-agent"),
  agentForm: $("#agent-form"),
  agentName: $("#agent-name"),
  agentId: $("#agent-id"),
  agentDescription: $("#agent-description"),
  agentProvider: $("#agent-provider"),
  agentModel: $("#agent-model"),
  agentRounds: $("#agent-rounds"),
  agentTools: $("#agent-tools"),
  agentSkills: $("#agent-skills"),
  agentAllSkills: $("#agent-all-skills"),
  agentDelete: $("#delete-agent"),
  skillsButton: $("#show-skills"),
  skillsPanel: $("#skills-panel"),
  skillsList: $("#skills-list"),
  skillsEmpty: $("#skills-empty"),
  skillsStatus: $("#skills-status"),
  skillTitle: $("#skill-title"),
  skillDescription: $("#skill-description"),
  skillUsers: $("#skill-users"),
  skillContent: $("#skill-content"),
  skillPreview: $("#skill-preview"),
  skillView: $("#skill-view"),
  skillEdit: $("#skill-edit"),
  skillDelete: $("#delete-skill"),
  skillArchive: $("#show-skill-archive"),
  skillArchiveDialog: $("#skill-archive-dialog"),
  skillArchiveClose: $("#skill-archive-close"),
  skillArchiveList: $("#skill-archive-list"),
  skillArchiveStatus: $("#skill-archive-status"),
  skillArchiveClear: $("#clear-skill-archive"),
  skillAdd: $("#add-skill"),
  skillDialog: $("#skill-dialog"),
  skillForm: $("#skill-form"),
  skillClose: $("#skill-close"),
  skillCancel: $("#skill-cancel"),
  skillCreate: $("#skill-create"),
  skillCreateError: $("#skill-create-error"),
  newSkillName: $("#new-skill-name"),
  newSkillDescription: $("#new-skill-description"),
  newSkillInstructions: $("#new-skill-instructions"),
  skillSave: $("#save-skill"),
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
  workspaceLabel: $("#workspace-label"),
  newProjectButton: $("#new-project"),
  projectDialog: $("#project-dialog"),
  projectForm: $("#project-form"),
  projectDialogTitle: $("#project-dialog-title"),
  projectName: $("#project-name"),
  projectFolder: $("#project-folder"),
  projectChooseFolder: $("#project-choose-folder"),
  projectError: $("#project-error"),
  projectDelete: $("#project-delete"),
  projectCancel: $("#project-cancel"),
  projectClose: $("#project-close"),
  projectSave: $("#project-save"),
  folderDialog: $("#folder-dialog"),
  folderPathForm: $("#folder-path-form"),
  folderPath: $("#folder-path"),
  folderUp: $("#folder-up"),
  folderList: $("#folder-list"),
  folderError: $("#folder-error"),
  folderSelect: $("#folder-select"),
  folderCancel: $("#folder-cancel"),
  folderClose: $("#folder-close"),
  messageList: $("#message-list"),
  runBar: $("#run-bar"),
  teamsStatus: $("#teams-status"),
  toggleCanvas: $("#toggle-canvas"),
  canvasPane: $("#canvas-pane"),
  canvasResizer: $("#canvas-resizer"),
  canvasHint: $("#canvas-hint"),
  teamAddAgent: $("#team-add-agent"),
  teamCanvas: $("#team-canvas"),
  teamStage: $("#team-stage"),
  teamEdges: $("#team-edges"),
  zoomIn: $("#zoom-in"),
  zoomOut: $("#zoom-out"),
  zoomReset: $("#zoom-reset"),
  agentPromptDialog: $("#agent-prompt-dialog"),
  agentPromptForm: $("#agent-prompt-form"),
  agentPromptTitle: $("#agent-prompt-title"),
  agentPromptText: $("#agent-prompt-text"),
  agentPromptError: $("#agent-prompt-error"),
  agentPromptSave: $("#agent-prompt-save"),
  agentPromptCancel: $("#agent-prompt-cancel"),
  agentPromptClose: $("#agent-prompt-close"),
  turnRecovery: $("#turn-recovery"),
  turnRecoveryText: $("#turn-recovery-text"),
  retryTurn: $("#retry-turn"),
  messageForm: $("#message-form"),
  messageInput: $("#message-input"),
  saveMessage: $("#save-message"),
};

export const state = {
  sessions: [],
  turnFailures: new Map(),
  projects: [],
  // Свёрнутые в сайдбаре проекты; запоминаются в этом браузере.
  collapsedProjects: loadCollapsed(),
  messages: [],
  messagesSessionId: null,
  preferredModel: "",
  selectedId: null,
  showingSettings: initialView === "settings",
  showingTools: initialView === "tools",
  showingMemory: initialView === "memory",
  showingPrompt: initialView === "prompt",
  showingSkills: initialView === "skills",
  editingTitle: false,
  actionsDisabled: false,
  // Идёт ли сейчас ответ модели: тогда кнопка отправки превращается в «Стоп».
  streaming: false,
  // Растёт при каждой замене списка сообщений: чат перерисовывается только тогда, а не на каждый токен.
  messagesVersion: 0,
};

function loadView() {
  const viewFromUrl = window.location.hash.replace(/^#(?:customization\/)?/, "");
  if (["chat", "settings", "tools", "memory", "prompt", "skills"].includes(viewFromUrl)) {
    return viewFromUrl;
  }
  try {
    return localStorage.getItem("app-view") || "chat";
  } catch {
    return "chat";
  }
}

function loadCollapsed() {
  try {
    return new Set(JSON.parse(localStorage.getItem("collapsed-projects") || "[]"));
  } catch {
    return new Set();
  }
}

export function toggleProject(projectId) {
  if (state.collapsedProjects.has(projectId)) state.collapsedProjects.delete(projectId);
  else state.collapsedProjects.add(projectId);
  try {
    localStorage.setItem("collapsed-projects", JSON.stringify([...state.collapsedProjects]));
  } catch {
    // Без хранилища сворачивание просто не запомнится.
  }
  render();
}

export function selectedProject() {
  const session = selectedSession();
  return session?.project_id ? state.projects.find((project) => project.id === session.project_id) : undefined;
}

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
  const view = state.showingSettings ? "settings" : state.showingTools ? "tools"
    : state.showingMemory ? "memory" : state.showingPrompt ? "prompt"
    : state.showingSkills ? "skills" : "chat";
  try {
    localStorage.setItem("app-view", view);
  } catch {
    // Без localStorage навигация работает, но не сохраняется после обновления.
  }
  const hash = ["chat", "settings"].includes(view) ? `#${view}` : `#customization/${view}`;
  if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
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
