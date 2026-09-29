// По сохранённой истории определяет, какой запрос можно повторить после сбоя.
export function turnRecovery(messages, failure = null) {
  const lastUser = messages.findLastIndex((message) => message.role === "user");
  const matching = lastUser >= 0 && (!failure || messages[lastUser].content === failure.content);
  const tail = matching ? messages.slice(lastUser + 1) : [];
  const answer = tail.findLast((message) => message.role === "assistant" && !message.tool_calls?.length);
  const interrupted = answer?.content?.endsWith("_(ответ прерван)_");
  if (!failure && (lastUser < 0 || (answer && !interrupted))) return null;
  const saved = matching || failure?.saved;
  const content = failure?.content ?? messages[lastUser]?.content;
  if (!content) return null;
  const tools = tail.some((message) => message.role === "tool" || message.tool_calls?.length);
  const details = saved
    ? `Запрос сохранён.${interrupted ? " Частичный ответ сохранён." : answer ? " Ответ сохранён." : " Завершённого ответа нет."}`
    : "Сохранение запроса не подтверждено. Он мог попасть на сервер; проверьте историю.";
  return {
    content, tools,
    text: `${failure?.reason ? failure.reason + " " : ""}${details}${tools ? " В истории есть вызовы инструментов; выполненные действия не отменены." : ""} Повтор создаст новый запрос с тем же текстом.`,
  };
}
