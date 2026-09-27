// Действия со списком сессий: загрузка, выбор, создание, переименование и удаление.
import { messagesApi, projectsApi, sessionsApi } from "./api.js";
import { clearError, elements, render, setMessages, showError, state } from "./state.js";

export function selectSession(id) {
  elements.toolsDialog.close();
  state.selectedId = id;
  state.editingTitle = false;
  setMessages([], null);
  if (!state.streaming) elements.messageInput.value = "";
  state.showingSettings = false;
  state.showingMemory = false;
  state.showingPrompt = false;
  state.showingSkills = false;
  render();
  loadMessages(id);
}

export async function loadMessages(sessionId) {
  try {
    const loaded = await messagesApi.list(sessionId);
    if (state.selectedId !== sessionId) return;
    setMessages(loaded, sessionId);
    clearError();
    render();
  } catch (error) {
    if (state.selectedId === sessionId) showError(error);
  }
}

export async function loadSessions() {
  try {
    [state.sessions, state.projects] = await Promise.all([sessionsApi.list(), projectsApi.list()]);
    const previousId = state.selectedId;
    state.selectedId = state.sessions.some((session) => session.id === state.selectedId)
      ? state.selectedId
      : (state.sessions[0]?.id || null);
    if (state.selectedId !== previousId) setMessages([], null);
    clearError();
    render();
    if (state.selectedId) await loadMessages(state.selectedId);
  } catch (error) {
    showError(error);
  }
}

export async function createSession(projectId = null) {
  elements.toolsDialog.close();
  elements.newButton.disabled = true;
  elements.emptyNewButton.disabled = true;
  try {
    const session = await sessionsApi.create(state.preferredModel, projectId);
    state.selectedId = session.id;
    state.editingTitle = false;
    setMessages([], null);
    elements.messageInput.value = "";
    state.showingSettings = false;
    state.showingMemory = false;
    state.showingPrompt = false;
    state.showingSkills = false;
    await loadSessions();
  } catch (error) {
    showError(error);
  } finally {
    elements.newButton.disabled = false;
    elements.emptyNewButton.disabled = false;
  }
}

export async function renameSession() {
  if (!state.editingTitle) return;
  const sessionId = state.selectedId;
  const title = elements.titleInput.value.trim();
  const currentTitle = state.sessions.find((session) => session.id === sessionId)?.title;
  state.editingTitle = false;
  render();
  if (!sessionId || !title || title === currentTitle) return;
  try {
    const updated = await sessionsApi.rename(sessionId, title);
    state.sessions = state.sessions.map((session) => session.id === sessionId ? updated : session);
    clearError();
    render();
  } catch (error) {
    showError(error);
  }
}

export async function deleteSession(sessionId) {
  const session = state.sessions.find((item) => item.id === sessionId);
  if (!session) return;
  const confirmed = window.confirm(
    `Удалить сессию «${session.title}» и всю её историю сообщений? Это действие нельзя отменить.`
  );
  if (!confirmed) return;
  elements.toolsDialog.close();

  state.actionsDisabled = true;
  render();
  try {
    await sessionsApi.delete(session.id);
    if (state.selectedId === session.id) {
      state.selectedId = null;
      setMessages([], null);
      state.editingTitle = false;
      elements.messageInput.value = "";
    }
    await loadSessions();
  } catch (error) {
    showError(error);
  } finally {
    state.actionsDisabled = false;
    render();
  }
}
