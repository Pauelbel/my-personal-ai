// Точка входа UI: отрисовывает экран по общему состоянию и связывает события с модулями.
import { renderMessages } from "./chat.js";
import {
  attachStreamingBubble, handleMessageKeydown, loadCatalog, loadModels, saveConfig, sendMessage,
  showModel, stopStreaming, renderTurnRecovery, retryTurn,
} from "./composer.js";
import { initFiles } from "./files-panel.js";
import { initFolderPicker } from "./folder-picker.js";
import { renderMemoryPanel, saveMemory, showMemory, updateMemory } from "./memory-panel.js";
import { initProjectDialog, openProjectDialog } from "./projects.js";
import { initAgentsPanel, renderAgentsPanel, showAgents } from "./agents-panel.js";
import { createSession, deleteSession, loadSessions, renameSession, selectSession } from "./sessions.js";
import { deleteSkill, initSkillDialog, renderSkillsPanel, saveSkill, setSkillEditing, showSkills } from "./skills-panel.js";
import { renderSidebar } from "./sidebar.js";
import { initSessionCanvas, renderSessionCanvas } from "./session-canvas.js";
import {
  elements, render, selectedProject, selectedSession, setRenderer, state, toggleProject,
} from "./state.js";
import { initTheme } from "./theme.js";
import { showTools } from "./tools-dialog.js";

let renderedMessagesVersion = -1;
// Открыта ли панель сессий на экране: по смене состояния переносится фокус.
let sessionsShown = false;

// Сколько контекстного окна занял последний запрос и как быстро модель его сгенерировала.
function renderUsage(session) {
  const used = session?.context_tokens;
  const window = session?.context_window;
  const format = (value) => value.toLocaleString("ru-RU");
  elements.chatUsage.hidden = !session;
  if (!session) return;
  const share = used != null && window ? used / window : null;
  elements.usageText.textContent = used == null ? "—"
    : window ? `${format(used)} / ${format(window)} (${Math.round(share * 100)}%)` : format(used);
  elements.usageFill.style.width = `${Math.min(share ?? 0, 1) * 100}%`;
  elements.usageContext.classList.toggle("warn", share != null && share >= 0.8);
  elements.usageContext.classList.toggle("full", share != null && share >= 1);
  const speed = session.tokens_per_second;
  elements.usageSpeed.textContent = speed == null ? "⚡ —" : `⚡ ${speed.toFixed(1)} ток/с`;
}

// Панель сессий выезжает поверх чата. При открытии фокус уходит на открытую сессию,
// при закрытии возвращается на ☰ Сессии — если он был в панели и не ушёл туда, куда щёлкнули.
function renderSessionsDrawer() {
  const open = state.sessionsOpen;
  elements.sessionsButton.setAttribute("aria-expanded", String(open));
  if (open === sessionsShown) return;
  const focusInside = elements.sessionsDrawer.contains(document.activeElement);
  elements.sessionsDrawer.hidden = !open;
  sessionsShown = open;
  if (open) {
    (elements.list.querySelector(".session-item.active") || elements.sessionsDrawer.querySelector("button"))?.focus();
  } else if (focusInside || document.activeElement === document.body) {
    elements.sessionsButton.focus();
  }
}

function setSessionsOpen(open) {
  state.sessionsOpen = open;
  render();
}

// Повторный щелчок по активной странице возвращает к сессии.
function showSessionView() {
  state.showingSettings = false;
  state.showingTools = false;
  state.showingMemory = false;
  state.showingPrompt = false;
  state.showingSkills = false;
  render();
}

const showingCustomization = () => state.showingTools || state.showingMemory || state.showingPrompt || state.showingSkills;

function renderApp() {
  const selected = selectedSession();
  // Чаты узлов команды открываются из ленты запуска, в списке их нет.
  const listed = state.sessions.filter((session) => !session.hidden);
  renderSidebar(elements.list, {
    projects: state.projects,
    sessions: listed,
    selectedId: state.selectedId,
    collapsed: state.collapsedProjects,
    actionsDisabled: state.actionsDisabled,
    emptyText: "Пока нет сессий",
    onSelect: selectSession,
    onDelete: deleteSession,
    onCreate: (projectId) => createSession(projectId),
    onEdit: openProjectDialog,
    onToggle: toggleProject,
  });
  renderSessionsDrawer();

  const { showingSettings, showingTools, showingMemory, showingPrompt, showingSkills, editingTitle } = state;
  const customizing = showingCustomization();
  const showingPanel = showingSettings || customizing;
  elements.customizationPanel.hidden = !customizing;
  elements.toolsPanel.hidden = !showingTools;
  elements.customizationButton.setAttribute("aria-pressed", String(customizing));
  elements.settingsButton.setAttribute("aria-pressed", String(showingSettings));
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
    : customizing ? "Кастомизация"
    : (selected?.title || "Сессии");
  elements.titleText.textContent = headerTitle;
  elements.editTitle.title = selected && !showingPanel ? "Переименовать сессию" : headerTitle;
  elements.editTitle.disabled = !selected || showingPanel;
  // Рабочая папка открытой сессии — папка её проекта. В шапке — две последние папки пути, полный путь —
  // в подсказке. Если места мало, путь обрезается слева; метки направления держат его слева направо.
  const project = selectedProject();
  elements.sessionFolder.hidden = !project || showingPanel;
  if (project) {
    const parts = project.workspace.split(/[\\/]/).filter(Boolean);
    const shown = parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : project.workspace;
    elements.sessionFolderPath.textContent = `\u200e${shown}\u200e`;
    elements.sessionFolder.title = `Рабочая папка проекта «${project.name}»: ${project.workspace}\nЩелчок — настройки проекта`;
    elements.sessionFolder.setAttribute("aria-label", `Рабочая папка ${project.workspace}. Открыть настройки проекта «${project.name}»`);
  }
  renderUsage(selected);
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
    if (document.activeElement !== elements.model) showModel(selected.model || state.preferredModel);
  }
  elements.saveMessage.textContent = state.streaming ? "■" : "↑";
  elements.saveMessage.title = state.streaming ? "Остановить ответ" : "Отправить";
  elements.saveMessage.setAttribute("aria-label", state.streaming ? "Остановить ответ" : "Отправить сообщение");
  elements.saveMessage.disabled = state.actionsDisabled && !state.streaming;
  elements.memoryUpdateChat.disabled = state.actionsDisabled || !selected;
  renderSessionCanvas();
  renderMemoryPanel();
  renderAgentsPanel();
  renderSkillsPanel();
}

setRenderer(renderApp);

elements.sessionsButton.addEventListener("click", () => setSessionsOpen(!state.sessionsOpen));
// Новая сессия — в проекте открытой сессии; если сессии нет, сервер создаст её в «Черновиках».
elements.newButton.addEventListener("click", () => createSession(selectedSession()?.project_id ?? null));
elements.newProjectButton.addEventListener("click", () => openProjectDialog());
// Папку меняют в настройках проекта: они открываются щелчком по папке в шапке.
elements.sessionFolder.addEventListener("click", () => {
  const project = selectedProject();
  if (project) openProjectDialog(project);
});
elements.toolsButton.addEventListener("click", showTools);
elements.customizationButton.addEventListener("click", () => {
  if (showingCustomization()) showSessionView();
  else void showTools();
});
elements.memoryButton.addEventListener("click", showMemory);
elements.promptButton.addEventListener("click", () => { void showAgents(); });
elements.skillsButton.addEventListener("click", showSkills);
elements.emptyNewButton.addEventListener("click", () => createSession());
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
elements.skillSave.addEventListener("click", saveSkill);
elements.skillView.addEventListener("click", () => setSkillEditing(false));
elements.skillEdit.addEventListener("click", () => setSkillEditing(true));
elements.skillDelete.addEventListener("click", deleteSkill);
elements.memoryUpdateChat.addEventListener("click", updateMemory);
for (const field of [elements.provider, elements.model]) {
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
  if (state.showingSettings) {
    showSessionView();
    return;
  }
  state.showingSettings = true;
  state.showingTools = false;
  state.showingMemory = false;
  state.showingPrompt = false;
  state.showingSkills = false;
  render();
});

// Панель сессий закрывается клавишей Esc и щелчком вне неё. Диалоги, открытые из панели
// (настройки проекта, новый проект), её не закрывают: Esc и щелчки в них относятся к диалогу.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !state.sessionsOpen || document.querySelector("dialog[open]")) return;
  setSessionsOpen(false);
});
document.addEventListener("pointerdown", (event) => {
  if (!state.sessionsOpen || event.target.closest("#sessions-drawer, #show-sessions, dialog")) return;
  setSessionsOpen(false);
});

initTheme();
initFolderPicker();
initProjectDialog();
initSkillDialog();
initSessionCanvas();
initFiles();
try {
  // Сайдбара, сворачивания правой панели и раскрытия карточек больше нет: их ключи в хранилище не нужны.
  for (const key of ["sidebar-width", "canvas-collapsed", "expanded-cards"]) localStorage.removeItem(key);
} catch {
  // Без хранилища удалять нечего.
}
initAgentsPanel();
render();
loadSessions().then(async () => {
  await loadCatalog();
  // После восстановления сессии загружаем данные открытой до обновления вкладки.
  if (state.showingTools) await showTools();
  else if (state.showingMemory) await showMemory();
  else if (state.showingPrompt) await showAgents();
  else if (state.showingSkills) await showSkills();
});
