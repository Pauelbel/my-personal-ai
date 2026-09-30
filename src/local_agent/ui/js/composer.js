// Поле ввода: настройки сессии (агент, провайдер, модель, папка), отправка с потоковым ответом и «Стоп».
import { catalogApi, sessionsApi, turnsApi } from "./api.js";
import { StreamingMessage } from "./chat.js";
import { pickFolder } from "./folder-picker.js";
import { openProjectDialog } from "./projects.js";
import { turnRecovery } from "./recovery.js";
import { nodeName, showRunState } from "./session-canvas.js";
import { loadSessions } from "./sessions.js";
import {
  clearError, elements, render, selectedProject, setMessages, showError, state,
} from "./state.js";

let pendingConfigSave = Promise.resolve();
let controller = null;
let bubble = null;
let bubbleSessionId = null;
let pendingApproval = null;
// Карточка агента сессии на холсте: ей уходит сообщение, написанное в чате самой сессии.
const ENTRY = "main";

// Пузырь печатающегося ответа показываем, только пока открыта сессия, в которой он идёт.
export function attachStreamingBubble() {
  if (bubble && bubbleSessionId === state.selectedId) bubble.attach();
  else bubble?.detach();
}

export async function loadCatalog() {
  try {
    fillSelect(elements.provider, await catalogApi.providers());
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
    agent_id: null,
  };
  const operation = pendingConfigSave.then(async () => {
    const current = state.sessions.find((session) => session.id === sessionId);
    if (!current) return false;
    if (
      current.provider === config.provider && current.model === config.model
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
      agent_id: null,
    });
    await loadSessions();
  } catch (error) {
    showError(error);
  }
}

export function renderTurnRecovery() {
  const recovery = state.messagesSessionId === state.selectedId
    ? turnRecovery(state.messages, state.turnFailures.get(state.selectedId)) : null;
  elements.turnRecovery.hidden = !recovery || state.streaming;
  elements.turnRecoveryText.textContent = recovery?.text || "";
  elements.retryTurn.disabled = state.actionsDisabled || state.streaming;
}

export async function retryTurn(event) {
  if (state.messagesSessionId !== state.selectedId) return;
  const recovery = turnRecovery(state.messages, state.turnFailures.get(state.selectedId));
  if (!recovery) return;
  if (recovery.tools && !window.confirm(
    "Некоторые инструменты уже выполнялись. Повтор может выполнить действия ещё раз. Повторить запрос?",
  )) return;
  await sendMessage(event, recovery.content);
}

export async function sendMessage(event, retryContent = null) {
  event.preventDefault();
  if (state.actionsDisabled || state.streaming) return;
  const content = retryContent ?? elements.messageInput.value.trim();
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
  state.turnFailures.delete(sessionId);
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
  // Кому написали: агенту сессии или агенту холста напрямую — его переписка открыта в чате.
  const addressee = state.sessions.find((session) => session.id === sessionId)?.node_id ?? ENTRY;
  try {
    await turnsApi.stream(sessionId, content, controller.signal, (item) => {
      // С агентами на холсте события приходят от разных агентов. В открытый чат попадает только то,
      // что относится к агенту, которому написали; работу остальных видно в статусе ответа и на холсте.
      const own = !item.node_id || item.node_id === addressee;
      const who = own ? "" : `${nodeName(item.node_id)}: `;
      if (item.type === "node_started") {
        showRunState({ activeNode: item.node_id, label: "работает" });
        if (!own) bubble.reset(`${nodeName(item.node_id)} работает`);
      } else if (item.type === "user_message") {
        if (!own) return;
        saved = true;
        if (!item.message.sender && retryContent === null && state.selectedId === sessionId) elements.messageInput.value = "";
        push(item.message);
      } else if (item.type === "log") {
        bubble.addLog(who + item.text);
      } else if (item.type === "reasoning") {
        if (own) bubble.addReasoning(item.text);
      } else if (item.type === "delta") {
        if (own) bubble.append(item.text);
      } else if (item.type === "tool_calls") {
        if (own) push(item.message);
        bubble.reset(`${who}выполняется инструмент`);
      } else if (item.type === "approval_required") {
        const approvalSession = item.session_id || sessionId;
        pendingApproval = { sessionId: approvalSession, callId: item.call.id };
        if (!own) showRunState({ activeNode: item.node_id, label: "ждёт подтверждения" });
        bubble.askApproval({ ...item, actor: own ? undefined : nodeName(item.node_id) }, (approved) => {
          pendingApproval = null;
          return turnsApi.decide(approvalSession, item.call.id, approved).catch(showError);
        });
      } else if (item.type === "tool_result") {
        if (own) push(item.message);
        bubble.setStatus(`${who}модель думает`);
      } else if (item.type === "done" || item.type === "notice") {
        if (own) push(item.message);
      } else if (item.type === "error") {
        const error = new Error(item.message);
        error.messageSaved = item.user_message_saved;
        throw error;
      }
      // Потоковые фрагменты меняют только пузырь ответа. Полная перерисовка здесь
      // пересоздавала кнопки навигации и срывала клики во время генерации.
      if (own && ["user_message", "tool_calls", "tool_result", "done", "notice"].includes(item.type)) render();
    });
  } catch (error) {
    if (error.name === "AbortError") {
      notice = new Error("Ответ остановлен.");
    } else {
      if (retryContent === null && (error.messageSaved || saved) && state.selectedId === sessionId) elements.messageInput.value = "";
      notice = error;
    }
  } finally {
    bubble?.finish();
    bubble = null;
    pendingApproval = null;
    controller = null;
    showRunState({ activeNode: null, label: "" });
    state.streaming = false;
    state.actionsDisabled = false;
    if (notice) state.turnFailures.set(sessionId, {
      content, saved: saved || notice.messageSaved === true, reason: notice.message,
    });
    await loadSessions();
    // loadSessions сбрасывает уведомления, поэтому итог хода показываем после неё.
    render();
  }
}

export function stopStreaming() {
  // Иначе сервер до таймаута ждал бы решения по вызову инструмента и держал сессию занятой.
  if (pendingApproval) {
    turnsApi.decide(pendingApproval.sessionId, pendingApproval.callId, false).catch(() => {});
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
