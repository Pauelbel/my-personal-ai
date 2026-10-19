// Чат вики на вкладке «Вики»: отдельный разговор вне проектов. Агент вики добавляет и дополняет заметки,
// отвечает по ним и проверяет вики (меню «Действия ▾»). Модель помнит только последние реплики — старое забывается само,
// «Очистить» начинает разговор заново. Ход и карточки — обычные API сессии.
import { api } from "./api.js";
import { imageInput } from "./attach.js";
import { renderCard } from "./cards.js";
import { renderMessages, scrollDown } from "./chat.js";
import { Live } from "./live.js";
import { fitInput, recall, remember, el } from "./util.js";
import { openNote, showWiki } from "./wiki.js";
import { initWikiActions } from "./wikiactions.js";

const $ = (selector) => document.querySelector(selector);
const MAX_INPUT_SHARE = 0.4;

const chat = { session: null, messages: [], memory: 3, running: false, controller: null, live: null, card: null };
let toast = () => {};
// Картинки в поле ввода чата вики (attach.js).
let images = null;

export function initWikiChat(options) {
  ({ toast } = options);
  const input = $("#wiki-chat-input");
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!chat.running) void submit();
    } else if (event.key === "Escape" && chat.running) {
      event.preventDefault();
      void stop();
    }
  });
  input.addEventListener("input", autosize);
  images = imageInput({
    box: $(".wiki-chat-composer .composer-box"), input, strip: $("#wiki-attachments"), button: $("#wiki-attach"), toast,
  });
  $("#wiki-chat-send").addEventListener("click", () => void (chat.running ? stop() : submit()));
  $("#wiki-chat-clear").addEventListener("click", () => void clear());
  $("#wiki-chat-toggle").addEventListener("click", () => setOpen(true));
  $("#wiki-chat-close").addEventListener("click", () => setOpen(false));
  // Пути заметок в ответах открывают заметку справа.
  $("#wiki-chat-log").addEventListener("click", (event) => {
    const link = event.target.closest(".path-link");
    if (!link) return;
    event.preventDefault();
    openNote(link.dataset.path, toast);
  });
  initWikiActions({ toast, open: () => setOpen(true), fill });
  setOpen(recall("wiki-chat") !== "closed");
}

// Действие из меню встаёт в поле ввода: его можно поправить под этот раз и отправить Enter.
function fill(text) {
  setOpen(true);
  const input = $("#wiki-chat-input");
  input.value = text;
  autosize();
  input.focus();
  input.setSelectionRange(text.length, text.length);
}

// Состояние чата с сервера: на открытии вкладки. Ход идёт без этой вкладки — подключаемся к нему.
export async function refreshWikiChat() {
  let data;
  try {
    data = await api.wikiChat();
  } catch {
    return;
  }
  Object.assign(chat, { session: data.session, memory: data.memory });
  if (chat.controller) return draw();
  chat.messages = data.session ? await api.messages(data.session.id).catch(() => []) : [];
  chat.card = data.session?.pending ?? null;
  draw();
  if (data.running && data.session) void run((signal, onEvent) => api.follow(data.session.id, 0, signal, onEvent));
}

async function submit() {
  const input = $("#wiki-chat-input");
  const text = input.value.trim();
  if (!text && !images.count) return;
  const attachments = await images.names();
  if (!text && !attachments.length) return;
  try {
    const { session } = await api.ensureWikiChat();
    chat.session = session;
    input.value = "";
    images.clear();
    autosize();
    chat.messages = [...chat.messages, { id: "local", role: "user", content: text, attachments, created_at: new Date().toISOString() }];
    await run((signal, onEvent) => api.send(session.id, text, signal, onEvent, attachments));
  } catch (error) {
    toast(error);
  }
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
    Object.assign(chat, { session: null, messages: [], card: null });
    draw();
  } catch (error) {
    toast(error);
  }
}

// open(signal, onEvent) — поток хода: новое сообщение или подключение к идущему ходу.
async function run(open) {
  chat.controller?.abort();
  chat.live?.finish();
  const controller = new AbortController();
  Object.assign(chat, { controller, running: true, live: null });
  draw();
  chat.live = new Live($("#wiki-chat-log"));
  scrollDown($("#wiki-chat-log"));
  try {
    await open(controller.signal, (event) => {
      if (event.type === "approval") {
        chat.card = event.card;
        drawCard();
      } else if (["tool_result", "done", "error"].includes(event.type) && chat.card) {
        chat.card = null;
        drawCard();
      }
      if (event.type === "error") toast(new Error(event.message));
      else if (event.type !== "done") {
        chat.live?.handle(event);
        scrollDown($("#wiki-chat-log"));
      }
    });
  } catch (error) {
    if (error.name !== "AbortError") toast(error);
  } finally {
    if (chat.controller === controller) {
      chat.live?.finish();
      Object.assign(chat, { live: null, controller: null, running: false });
      // Заметки могли измениться: список и граф справа перечитываются вместе с чатом.
      void showWiki(toast);
      await refreshWikiChat();
    }
  }
}

// ---------- отрисовка ----------

function draw() {
  const session = chat.session;
  const pending = Boolean(chat.card) && !chat.running;
  const status = $("#wiki-chat-status");
  status.textContent = chat.running ? "отвечает…" : pending ? "ждёт вашего решения" : "";
  status.className = `wiki-chat-status${chat.running ? " running" : pending ? " waiting" : ""}`;
  const send = $("#wiki-chat-send");
  send.textContent = chat.running ? "■" : "↵";
  send.title = chat.running ? "Остановить ответ (Esc)" : "Отправить (Enter)";
  $("#wiki-chat-clear").disabled = chat.running || !session;
  $(".tabs [data-view='wiki']").classList.toggle("attention", pending);
  drawLog();
  drawCard();
}

function drawCard() {
  renderCard($("#wiki-chat-cards"), chat.card, async (decision) => {
    try {
      await api.decide(chat.session.id, decision);
      chat.card = null;
      drawCard();
    } catch (error) {
      toast(error);
    }
  });
}

// Сообщения, которые модель уже не видит, приглушены и отделены чертой: с новым сообщением агент
// помнит только последние реплики (во время хода — вместе с текущей).
function drawLog() {
  const log = $("#wiki-chat-log");
  const live = chat.live?.node;
  const messages = chat.messages;
  if (!messages.length) {
    log.replaceChildren(el("p", "hint wiki-chat-empty",
      "Пришлите текст или ссылку и попросите «добавь заметку», спросите что-нибудь по заметкам или выберите проверку в «Действия ▾». "
      + `Агент помнит только ${chat.memory} последних сообщения — старое забывается само.`));
    if (live) log.append(live);
    return;
  }
  const starts = messages.flatMap((message, index) => (message.role === "user" && !message.card_reply ? [index] : []));
  const keep = chat.running || chat.card ? chat.memory : chat.memory - 1;
  const cut = starts.length > keep ? (keep > 0 ? starts[starts.length - keep] : messages.length) : 0;
  log.replaceChildren();
  if (cut) {
    const old = el("div", "wiki-forgotten");
    renderMessages(old, messages.slice(0, cut));
    log.append(old, el("div", "wiki-forget-line", "выше — агент уже не помнит"));
  }
  if (cut < messages.length) {
    const recent = el("div");
    renderMessages(recent, messages.slice(cut), { running: chat.running });
    log.append(recent);
  }
  markNotes(log);
  if (live) log.append(live);
  scrollDown(log);
}

// Пути заметок (`rag_основы.md`) в ответах — ссылки: щелчок открывает заметку справа.
function markNotes(root) {
  for (const code of root.querySelectorAll(":not(pre) > code")) {
    const path = code.textContent.trim();
    if (/^[^\s`<>]+\.md$/.test(path)) {
      code.classList.add("path-link");
      code.dataset.path = path;
      code.title = "Открыть заметку";
    }
  }
}

function setOpen(open) {
  $("#wiki-chat").hidden = !open;
  $("#view-wiki").classList.toggle("chat-hidden", !open);
  $("#wiki-chat-toggle").hidden = open;
  // Действия живут в шапке чата; пока чат скрыт, они переезжают в шапку страницы.
  if (open) $("#wiki-chat-clear").before($("#wiki-tools"));
  else $(".wiki-main .page-head").append($("#wiki-tools"));
  remember("wiki-chat", open ? "open" : "closed");
  if (open) void refreshWikiChat();
}

function autosize() {
  fitInput($("#wiki-chat-input"), MAX_INPUT_SHARE);
}
