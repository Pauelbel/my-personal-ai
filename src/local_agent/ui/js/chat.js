// Этот модуль показывает сохранённые сообщения и оформляет ответы агента.
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

  for (const message of messages) {
    const item = document.createElement("article");
    item.className = `message ${message.role}`;

    const role = document.createElement("span");
    role.className = "message-role";
    role.textContent = message.role === "user" ? "Вы" : "Агент";

    const content = document.createElement("div");
    content.className = "message-content";
    if (message.role === "assistant") appendMarkdown(content, message.content);
    else content.textContent = message.content;

    item.append(role, content);
    container.append(item);
  }
  container.scrollTop = container.scrollHeight;
}
