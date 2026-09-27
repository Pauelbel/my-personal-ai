// Поле ввода: настройки сессии (агент, провайдер, модель, папка), отправка с потоковым ответом и «Стоп».
import { catalogApi, sessionsApi, turnsApi } from "./api.js";
import { StreamingMessage } from "./chat.js";
import { pickFolder } from "./folder-picker.js";
import { openProjectDialog } from "./projects.js";
import { loadSessions } from "./sessions.js";
import {
  clearError, elements, render, selectedProject, setMessages, showError, state,
} from "./state.js";

let pendingConfigSave = Promise.resolve();
let controller = null;
let bubble = null;
let bubbleSessionId = null;
let pendingApproval = null;

// Пузырь печатающегося ответа показываем, только пока открыта сессия, в которой он идёт.
export function attachStreamingBubble() {
  if (bubble && bubbleSessionId === state.selectedId) bubble.attach();
  else bubble?.detach();
}

export async function loadCatalog() {
  try {
    const [providers, agents] = await Promise.all([catalogApi.providers(), catalogApi.agents()]);
    fillSelect(elements.provider, providers);
    fillSelect(elements.agent, agents);
    elements.agentField.hidden = agents.length < 2;
    render();
  } catch (error) {
    showError(error);
  }
  await loadModels();
}

export async function loadModels() {
  try {
    const available = await catalogApi.models(elements.provider.value || "lm_studio");
    state.preferredModel = available.default_model || available.models[0] || "";
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

function fillSelect(select, items) {
  const current = select.value;
  select.replaceChildren(...items.map((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name;
    return option;
  }));
  if (items.some((item) => item.id === current)) select.value = current;
}

export function saveConfig() {
  const sessionId = state.selectedId;
  const model = elements.model.value.trim();
  if (!sessionId || !model) {
    showError(new Error("Укажите модель для сессии"));
    return Promise.resolve(false);
  }
  const config = {
    provider: elements.provider.value,
    model,
    // Папка задаётся проектом; сессия меняет её только через chooseWorkspace.
    workspace: null,
    agent_id: elements.agent.value || null,
  };
  const operation = pendingConfigSave.then(async () => {
    const current = state.sessions.find((session) => session.id === sessionId);
    if (!current) return false;
    if (
      current.provider === config.provider && current.model === config.model
      && (!config.agent_id || current.agent_id === config.agent_id)
    ) {
      return true;
    }
    try {
      const updated = await sessionsApi.configure(sessionId, config);
      state.sessions = state.sessions.map((session) => session.id === sessionId ? updated : session);
      clearError();
      render();
      return true;
    } catch (error) {
      if (state.selectedId === sessionId) showError(error);
      return false;
    }
  });
  pendingConfigSave = operation.then(() => undefined);
  return operation;
}

// Кнопка папки: в проекте открывает его настройки, без проекта — выбор папки,
// после которого сессия переходит в проект этой папки.
export async function chooseWorkspace() {
  const sessionId = state.selectedId;
  if (!sessionId || state.streaming) return;
  const project = selectedProject();
  if (project) {
    openProjectDialog(project);
    return;
  }
  const folder = await pickFolder();
  if (!folder || state.selectedId !== sessionId) return;
  try {
    await sessionsApi.configure(sessionId, {
      provider: elements.provider.value,
      model: elements.model.value.trim() || state.preferredModel,
      workspace: folder,
      agent_id: elements.agent.value || null,
    });
    await loadSessions();
  } catch (error) {
    showError(error);
  }
}

export async function sendMessage(event) {
  event.preventDefault();
  if (state.actionsDisabled || state.streaming) return;
  const content = elements.messageInput.value.trim();
  const sessionId = state.selectedId;
  if (!sessionId || !content) return;

  state.actionsDisabled = true;
  render();
  if (!(await saveConfig())) {
    state.actionsDisabled = false;
    render();
    return;
  }
  controller = new AbortController();
  state.streaming = true;
  bubbleSessionId = sessionId;
  bubble = new StreamingMessage(elements.messageList);
  render();
  const visible = () => state.selectedId === sessionId && state.messagesSessionId === sessionId;
  const push = (message) => {
    if (visible()) setMessages([...state.messages, message]);
  };
  let saved = false;
  let notice = null;
  try {
    await turnsApi.stream(sessionId, content, controller.signal, (item) => {
      if (item.type === "user_message") {
        saved = true;
        if (state.selectedId === sessionId) elements.messageInput.value = "";
        push(item.message);
      } else if (item.type === "log") {
        bubble.addLog(item.text);
      } else if (item.type === "reasoning") {
        bubble.addReasoning(item.text);
      } else if (item.type === "delta") {
        bubble.append(item.text);
      } else if (item.type === "tool_calls") {
        push(item.message);
        bubble.reset("Выполняется инструмент");
      } else if (item.type === "approval_required") {
        pendingApproval = item.call.id;
        bubble.askApproval(item, (approved) => {
          pendingApproval = null;
          return turnsApi.decide(sessionId, item.call.id, approved).catch(showError);
        });
      } else if (item.type === "tool_result") {
        push(item.message);
        bubble.setStatus("Модель думает");
      } else if (item.type === "done") {
        push(item.message);
      } else if (item.type === "error") {
        const error = new Error(item.message);
        error.messageSaved = item.user_message_saved;
        throw error;
      }
      render();
    });
  } catch (error) {
    if (error.name === "AbortError") {
      notice = new Error("Ответ остановлен. То, что модель успела написать, сохранено.");
    } else {
      if ((error.messageSaved || saved) && state.selectedId === sessionId) elements.messageInput.value = "";
      notice = error;
    }
  } finally {
    bubble?.finish();
    bubble = null;
    pendingApproval = null;
    controller = null;
    state.streaming = false;
    state.actionsDisabled = false;
    await loadSessions();
    // loadSessions сбрасывает уведомления, поэтому итог хода показываем после неё.
    if (notice) showError(notice);
  }
}

export function stopStreaming() {
  // Иначе сервер до таймаута ждал бы решения по вызову инструмента и держал сессию занятой.
  if (pendingApproval && bubbleSessionId) {
    turnsApi.decide(bubbleSessionId, pendingApproval, false).catch(() => {});
    pendingApproval = null;
  }
  controller?.abort();
}

export function handleMessageKeydown(event) {
  if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;

  if (event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault();
    const input = elements.messageInput;
    input.setRangeText("\n", input.selectionStart, input.selectionEnd, "end");
    return;
  }

  if (event.shiftKey || event.altKey || event.metaKey) return;
  event.preventDefault();
  if (!state.actionsDisabled && !state.streaming) {
    elements.messageForm.requestSubmit(elements.saveMessage);
  }
}
