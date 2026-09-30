// Этот модуль показывает сообщения, вызовы инструментов и ответ агента, пока он ещё печатается.
import { appendMarkdown } from "./markdown.js";

export function renderMessages(container, messages) {
  container.replaceChildren();

  if (messages.length === 0) {
    const empty = document.createElement("p");
    empty.className = "message-empty";
    empty.textContent = "Сообщений пока нет";
    container.append(empty);
    return;
  }

  const results = new Map(
    messages.filter((message) => message.role === "tool").map((message) => [message.tool_call_id, message]),
  );
  let toolBubble = null;
  for (const message of messages) {
    if (message.role === "tool") continue;
    if (message.sender) {
      // Ответ агента с холста агенту сессии: короткая сворачиваемая строка, полный текст — по щелчку.
      container.append(incomingNode(message));
      toolBubble = null;
      continue;
    }
    // Раунды инструментов и итоговый ответ одного хода показываем одним пузырём агента.
    const shell = message.role === "assistant" && toolBubble ? toolBubble : messageShell(message.role);
    const { item, content } = shell;
    toolBubble = message.role === "assistant" && message.tool_calls?.length ? shell : null;
    if (message.role === "assistant") {
      if (message.content) renderAssistantText(content, message.content);
      for (const call of message.tool_calls || []) content.append(toolCallNode(call, results.get(call.id)));
    } else {
      content.textContent = message.content;
    }
    container.append(item);
  }
  container.scrollTop = container.scrollHeight;
}

// Пузырь ответа, который модель ещё пишет: текст, таймер ожидания, лог хода и запрос подтверждения.
export class StreamingMessage {
  constructor(container) {
    this.container = container;
    this.text = "";
    this.startedAt = Date.now();
    const { item, content } = messageShell("assistant");
    this.item = item;
    this.item.classList.add("streaming");
    this.content = content;
    // Клик по статусу разворачивает лог: что делает агент, пока модель думает.
    this.status = button("", "message-status");
    this.status.setAttribute("aria-expanded", "false");
    this.status.addEventListener("click", () => this.#toggleLog(this.log.hidden));
    this.log = document.createElement("div");
    this.log.className = "thinking-log";
    this.log.hidden = true;
    this.logCount = 0;
    this.reasoning = null;
    this.content.before(this.status, this.log);
    this.container.querySelector(".message-empty")?.remove();
    this.container.append(this.item);
    this.setStatus("Модель думает");
    this.timer = setInterval(() => this.#renderStatus(), 1000);
  }

  attach() {
    this.container.querySelector(".message-empty")?.remove();
    if (this.item.parentNode !== this.container || this.item !== this.container.lastElementChild) {
      this.container.append(this.item);
    }
    this.#scroll();
  }

  detach() {
    this.item.remove();
  }

  // Текст до вызова инструмента уже сохранён отдельным сообщением: начинаем пузырь заново.
  reset(label) {
    this.text = "";
    this.content.replaceChildren();
    this.setStatus(label);
  }

  append(text) {
    this.text += text;
    this.content.replaceChildren();
    appendMarkdown(this.content, this.text);
    // Модель перестала думать и начала отвечать: лог больше не нужен на виду.
    if (this.label) this.#toggleLog(false);
    this.setStatus("");
  }

  setStatus(label) {
    this.label = label;
    this.#renderStatus();
  }

  addLog(text) {
    this.reasoning = null;
    const line = document.createElement("div");
    line.className = "log-line";
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = `+${((Date.now() - this.startedAt) / 1000).toFixed(1)} с`;
    line.append(time, text);
    this.#appendLog(line);
  }

  // Рассуждения thinking-модели приходят кусками: дописываем их в один блок, пока его не прервёт строка лога.
  addReasoning(text) {
    if (!this.reasoning) {
      this.reasoning = document.createElement("div");
      this.reasoning.className = "log-reasoning";
      this.#appendLog(this.reasoning);
    }
    this.reasoning.textContent += text;
    this.#followLog();
  }

  #appendLog(node) {
    this.log.append(node);
    this.logCount += 1;
    this.#renderStatus();
    this.#followLog();
  }

  #followLog() {
    if (!this.log.hidden) this.log.scrollTop = this.log.scrollHeight;
  }

  #toggleLog(open) {
    this.log.hidden = !open;
    this.status.setAttribute("aria-expanded", String(open));
    this.#followLog();
    this.#scroll();
  }

  #renderStatus() {
    const seconds = Math.floor((Date.now() - this.startedAt) / 1000);
    this.status.textContent = this.label ? `${this.label}… ${seconds} с` : `Лог хода · ${this.logCount}`;
    this.status.classList.toggle("idle", !this.label);
    this.status.hidden = !this.label && this.logCount === 0;
    this.#scroll();
  }

  askApproval(event, onDecision) {
    const card = document.createElement("div");
    card.className = "approval-card";
    const title = document.createElement("strong");
    const args = parseArguments(event.call.arguments);
    const editing = event.call.name === "edit_file" && typeof args.old_text === "string";
    // В запуске команды подтверждение просит узел графа: показываем, кто именно.
    title.textContent = (event.actor ? `${event.actor}: ` : "") + (event.call.name === "write_file" && args.path
      ? `Модель хочет записать файл ${args.path} (${(args.content || "").length} символов)`
      : editing ? `Модель хочет изменить файл ${args.path}` : `Модель хочет выполнить «${event.tool_name}»`);
    const preview = document.createElement("details");
    // Правку показываем сразу: по ней видно, что именно заменится.
    preview.open = editing;
    const summary = document.createElement("summary");
    summary.textContent = editing ? "Что заменится" : "Показать содержимое";
    const body = document.createElement("pre");
    if (editing) {
      body.append(diffBlock("removed", args.old_text), diffBlock("added", args.new_text ?? ""));
    } else {
      body.textContent = event.call.name === "write_file" && typeof args.content === "string"
        ? args.content
        : JSON.stringify(args, null, 2);
    }
    preview.append(summary, body);
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const allow = button("Разрешить", "primary-button");
    const deny = button("Отклонить", "secondary-button");
    const decide = async (approved) => {
      allow.disabled = true;
      deny.disabled = true;
      await onDecision(approved);
      card.remove();
      this.setStatus(approved ? "Выполняется инструмент" : "Модель думает");
    };
    allow.addEventListener("click", () => decide(true));
    deny.addEventListener("click", () => decide(false));
    actions.append(allow, deny);
    card.append(title, preview, actions);
    this.item.append(card);
    this.setStatus("Ждёт вашего подтверждения");
    allow.focus();
  }

  finish() {
    clearInterval(this.timer);
    this.item.remove();
  }

  #scroll() {
    this.container.scrollTop = this.container.scrollHeight;
  }
}

function messageShell(roleName) {
  const item = document.createElement("article");
  item.className = `message ${roleName}`;
  const role = document.createElement("span");
  role.className = "message-role";
  role.textContent = roleName === "user" ? "Вы" : "Агент";
  const content = document.createElement("div");
  content.className = "message-content";
  item.append(role, content);
  return { item, content };
}

function renderAssistantText(container, text) {
  const block = document.createElement("div");
  block.className = "assistant-text";
  appendMarkdown(block, text);
  container.append(block);
  for (const pre of block.querySelectorAll("pre")) {
    const copy = button("Копировать", "code-copy");
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(pre.querySelector("code")?.textContent ?? "");
        copy.textContent = "Скопировано";
      } catch {
        copy.textContent = "Не удалось";
      }
      setTimeout(() => { copy.textContent = "Копировать"; }, 1500);
    });
    pre.classList.add("has-copy");
    pre.append(copy);
  }
}

function incomingNode(message) {
  const item = document.createElement("article");
  item.className = "message incoming";
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  // Координатор начинает реплику с пометки «[ответ: QA]»: в строке она не нужна, имя уже есть.
  const text = message.content.replace(/^\[(?:ответ|от): [^\]]*\]\s*/, "");
  summary.textContent = `⇠ ${message.sender}: ${firstLine(text)}`;
  const body = document.createElement("div");
  body.className = "message-content";
  appendMarkdown(body, text);
  details.append(summary, body);
  item.append(details);
  return item;
}

function firstLine(text) {
  const line = text.trim().split("\n")[0];
  return line.length > 120 ? `${line.slice(0, 119)}…` : line;
}

function toolCallNode(call, result) {
  const details = document.createElement("details");
  details.className = "tool-call";
  details.classList.toggle("failed", Boolean(result?.is_error));
  const summary = document.createElement("summary");
  const args = parseArguments(call.arguments);
  if (call.name === "send_message") {
    // Поручение агенту с холста читается как строка переписки, а не как вызов инструмента.
    details.classList.add("delegation");
    summary.textContent = `→ ${args.to ?? "?"}: ${firstLine(String(args.content ?? ""))}`;
    const body = document.createElement("pre");
    body.textContent = result?.is_error ? result.content : String(args.content ?? "");
    details.append(summary, body);
    return details;
  }
  const target = args.path || args.query || args.commit || args.name || "";
  summary.textContent = `⚒ ${call.name}${target ? ` · ${target}` : ""}${result ? "" : " · нет результата"}`;
  const body = document.createElement("pre");
  body.textContent = result ? result.content : "Вызов не был выполнен";
  details.append(summary, body);
  return details;
}

function diffBlock(kind, text) {
  const block = document.createElement("span");
  block.className = `diff-${kind}`;
  const sign = kind === "removed" ? "− " : "+ ";
  block.textContent = text ? text.split("\n").map((line) => sign + line).join("\n") + "\n" : `${sign}(пусто)\n`;
  return block;
}

function parseArguments(raw) {
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function button(label, className) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = className;
  node.textContent = label;
  return node;
}
