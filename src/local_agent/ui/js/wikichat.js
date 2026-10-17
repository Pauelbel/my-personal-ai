// Чат вики на вкладке «Вики»: отдельный разговор вне проектов. Агент вики добавляет и дополняет заметки,
// отвечает по ним и проверяет вики (/lint). Модель помнит только последние реплики — старое забывается само,
// «Очистить» начинает разговор заново. Ход, очередь и карточки — обычные API сессии.
import { api } from "./api.js";
import { imageInput } from "./attach.js";
import { el, renderMessages, scrollDown, StreamingMessage } from "./chat.js";
import { renderCards } from "./panel.js";
import { openNote, showWiki } from "./pages.js";
import { fitInput, recall, remember } from "./util.js";

const $ = (selector) => document.querySelector(selector);
export const LINT_PROMPT = "/lint — проверь вики: найди противоречия, устаревшее и дубли, которые стоит слить. "
  + "Сам ничего не меняй: покажи список правок, применяй только то, что я отмечу.";
const MAX_INPUT_SHARE = 0.4;

const chat = { session: null, messages: [], memory: 3, running: false, queue: [], controller: null, bubble: null, shownCard: null };
let toast = () => {};
// Картинки в поле ввода чата вики (attach.js).
let images = null;
// [provider, model] — модель, выбранная под полем ввода основного чата: чат вики работает на ней же.
let modelOf = () => [null, null];

export function initWikiChat(options) {
  ({ toast, model: modelOf } = options);
  const input = $("#wiki-chat-input");
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void submit();
    } else if (event.key === "Escape" && chat.running) {
      event.preventDefault();
      void stop();
    }
  });
  input.addEventListener("input", autosize);
  images = imageInput({
    box: $(".wiki-chat-composer .composer-box"), input, strip: $("#wiki-attachments"), button: $("#wiki-attach"), toast,
  });
  $("#wiki-chat-send").addEventListener("click", () => void (chat.running && !input.value.trim() && !images.count ? stop() : submit()));
  $("#wiki-chat-clear").addEventListener("click", () => void clear());
  $("#wiki-chat-toggle").addEventListener("click", () => setOpen(true));
  $("#wiki-chat-close").addEventListener("click", () => setOpen(false));
  // Пути заметок в ответах и шагах открывают заметку слева.
  $("#wiki-chat-log").addEventListener("click", (event) => {
    const link = event.target.closest(".path-link");
    if (!link || !link.dataset.path.endsWith(".md")) return;
    event.preventDefault();
    openNote(link.dataset.path, toast);
  });
  setOpen(recall("wiki-chat") !== "closed");
}

// Сообщение в чат вики от кнопок и команд: /lint из основного чата, «Проверить (lint)».
export async function sendWiki(text, onSaved = () => {}, attachments = []) {
  setOpen(true);
  try {
    const [provider, model] = modelOf();
    const { session } = await api.ensureWikiChat(provider || null, model || null);
    chat.session = session;
    if (chat.running) {
      await api.enqueue(session.id, text, attachments);
      onSaved();
      await refreshWikiChat(true);
      return;
    }
    await run((signal, onEvent) => api.send(session.id, text, signal, onEvent, false, attachments), onSaved, true);
  } catch (error) {
    toast(error);
  }
}

// Состояние чата с сервера: на открытии вкладки и на событиях сервера. Пока вкладка закрыта, обновляется
// только отметка на вкладке «Вики» — ход ждёт решения по карточке.
export async function refreshWikiChat(force = false) {
  let data;
  try {
    data = await api.wikiChat();
  } catch {
    return;
  }
  const { session, running, memory } = data;
  const changed = force || session?.id !== chat.session?.id || session?.updated_at !== chat.session?.updated_at;
  Object.assign(chat, { session, memory });
  $(".tabs [data-view='wiki']").classList.toggle("attention", Boolean(session?.pending) && !running);
  if (!visible() || chat.controller) return draw();
  chat.running = running;
  if (changed) {
    chat.messages = session ? await api.messages(session.id).catch(() => []) : [];
    chat.queue = session ? (await api.turn(session.id).catch(() => ({ queue: [] }))).queue : [];
  }
  draw();
  // Ход идёт без этой вкладки (после перезагрузки или из очереди) — подключаемся к нему.
  if (running && session) void run((signal, onEvent) => api.follow(session.id, 0, signal, onEvent));
}

async function submit() {
  const input = $("#wiki-chat-input");
  const text = input.value.trim();
  if (!text && !images.count) return;
  const attachments = await images.names();
  if (!text && !attachments.length) return;
  await sendWiki(text === "/lint" ? LINT_PROMPT : text, () => {
    input.value = "";
    images.clear();
    autosize();
  }, attachments);
}

async function stop() {
  if (!chat.session) return;
  try {
    await api.stop(chat.session.id);
  } catch (error) {
    toast(error);
  }
}

async function clear() {
  try {
    await api.clearWikiChat();
    Object.assign(chat, { session: null, messages: [], queue: [] });
    draw();
  } catch (error) {
    toast(error);
  }
}

// start(signal, onEvent) — поток хода: новое сообщение, решение по карточке или подключение к идущему ходу.
// fresh — ход начат новой репликой: пока она не пришла, строка хода не забирает прошлый, законченный ход.
async function run(start, onSaved = () => {}, fresh = false) {
  const controller = new AbortController();
  chat.bubble?.finish();
  Object.assign(chat, { controller, running: true, bubble: new StreamingMessage($("#wiki-chat-log")) });
  chat.bubble.fresh = fresh;
  draw();
  // При подключении к идущему ходу события повторяются с начала: уже загруженные сообщения не дублируем.
  const push = (message) => {
    if (!chat.messages.some((item) => item.id === message.id)) chat.messages = [...chat.messages, message];
  };
  const redraw = () => {
    drawLog();
    chat.bubble?.attach();
    scrollDown($("#wiki-chat-log"));
  };
  try {
    await start(controller.signal, (event) => {
      const bubble = chat.bubble;
      if (!bubble) return;
      if (event.type === "user_message") {
        onSaved();
        push(event.message);
        bubble.fresh = false;
        redraw();
      } else if (event.type === "delta") bubble.append(event.text);
      else if (event.type === "reasoning") bubble.addReasoning(event.text);
      else if (event.type === "log") bubble.addLog(event.text);
      // Модель зациклилась или ответила пустотой — сервер повторяет запрос, оборванный текст убираем.
      else if (event.type === "retry") bubble.reset("retry");
      else if (event.type === "tool_calls") {
        push(event.message);
        const names = (event.message.tool_calls ?? []).map((call) => call.name);
        bubble.reset(names.length ? "tool" : "thinking", names);
        redraw();
      } else if (event.type === "tool_result") {
        push(event.message);
        bubble.setStatus("thinking");
        redraw();
      } else if (event.type === "done") push(event.message);
      else if (event.type === "error") throw new Error(event.message);
    });
  } catch (error) {
    if (error.name !== "AbortError") toast(error);
  } finally {
    if (chat.controller === controller) {
      chat.bubble?.finish();
      Object.assign(chat, { bubble: null, controller: null, running: false });
    }
    // Заметки могли измениться: список и граф слева перечитываются вместе с чатом.
    void showWiki(toast);
    await refreshWikiChat(true);
  }
}

// ---------- отрисовка ----------

function draw() {
  const session = chat.session;
  const pending = Boolean(session?.pending) && !chat.running;
  const status = $("#wiki-chat-status");
  status.textContent = chat.running ? "отвечает…" : pending ? "ждёт вашего решения" : "";
  status.className = `wiki-chat-status${chat.running ? " running" : pending ? " waiting" : ""}`;
  const send = $("#wiki-chat-send");
  send.textContent = chat.running ? "■" : "↵";
  // Чат вики работает на модели, выбранной в основном чате.
  $("#wiki-chat-model").textContent = modelOf()[1] || "";
  send.title = chat.running ? "Остановить ответ (Esc); с текстом — поставить в очередь" : "Отправить (Enter)";
  $("#wiki-chat-clear").disabled = chat.running || !session;
  if (!chat.controller) drawLog();
  drawQueue();
  const card = session && { session_id: session.id, owner_id: session.id, session_title: "Чат вики", agent: null, pending: session.pending };
  renderCards($("#wiki-chat-cards"), pending ? [card] : [], {
    onDecision: (_card, decision) => run((signal, onEvent) => api.resume(session.id, decision, signal, onEvent)),
    onOpen: () => {},
    readFile: async () => null,
    busy: chat.running,
  });
  // Новая карточка сжимает ленту: прокручиваем к концу один раз, чтобы видеть, к чему она относится.
  const cardId = pending ? session.pending.interrupt_id : null;
  if (cardId && cardId !== chat.shownCard) scrollDown($("#wiki-chat-log"));
  chat.shownCard = cardId;
}

// Сообщения, которые модель уже не видит, приглушены и отделены чертой: с новым сообщением агент
// помнит только последние реплики (во время хода и пока он ждёт решения — вместе с текущей).
function drawLog() {
  const log = $("#wiki-chat-log");
  const messages = chat.messages;
  if (!messages.length) {
    log.replaceChildren(el("p", "hint wiki-chat-empty",
      "Пришлите текст или ссылку и попросите «добавь заметку», спросите что-нибудь по заметкам или наберите /lint. "
      + `Агент помнит только ${chat.memory} последних сообщения — старое забывается само.`));
    return;
  }
  const starts = messages.flatMap((message, index) => (message.role === "user" ? [index] : []));
  const keep = chat.running || chat.session?.pending ? chat.memory : chat.memory - 1;
  const cut = starts.length > keep ? (keep > 0 ? starts[starts.length - keep] : messages.length) : 0;
  const near = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  log.replaceChildren();
  if (cut) {
    const old = el("div", "wiki-forgotten");
    renderMessages(old, messages.slice(0, cut));
    log.append(old, el("div", "wiki-forget-line", "выше — агент уже не помнит"));
  }
  if (cut < messages.length) {
    const recent = el("div");
    const own = chat.running && chat.bubble && !chat.bubble.fresh;
    renderMessages(recent, messages.slice(cut), undefined, {
      live: own ? chat.bubble : null, waiting: Boolean(chat.session?.pending), busy: Boolean(own),
    });
    log.append(recent);
  }
  if (near || chat.running) scrollDown(log);
}

function drawQueue() {
  const root = $("#wiki-chat-queue");
  const items = chat.queue ?? [];
  root.hidden = !items.length;
  root.replaceChildren(...items.map((item) => {
    const row = el("div", "queue-item");
    const remove = el("button", "ghost", "×");
    remove.type = "button";
    remove.title = "Убрать из очереди";
    remove.addEventListener("click", async () => {
      try {
        await api.dequeue(chat.session.id, item.id);
        await refreshWikiChat(true);
      } catch (error) {
        toast(error);
      }
    });
    row.append(el("span", "queue-mark", "⋯"), el("span", "queue-text", item.content), remove);
    return row;
  }));
}

function setOpen(open) {
  $("#wiki-chat").hidden = !open;
  $("#view-wiki").classList.toggle("chat-hidden", !open);
  $("#wiki-chat-toggle").hidden = open;
  // Поиск и lint живут в шапке чата; пока чат скрыт, они переезжают в шапку страницы.
  if (open) $("#wiki-chat-clear").before($("#wiki-tools"));
  else $(".wiki-main .page-head").append($("#wiki-tools"));
  remember("wiki-chat", open ? "open" : "closed");
  if (open) void refreshWikiChat(true);
}

function visible() {
  return !$("#view-wiki").hidden && !$("#wiki-chat").hidden;
}

function autosize() {
  fitInput($("#wiki-chat-input"), MAX_INPUT_SHARE);
}

