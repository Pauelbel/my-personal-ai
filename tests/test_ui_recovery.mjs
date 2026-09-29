// Проверяет восстановление хода и разрыв SSE без браузера и настоящей модели.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function moduleFromFile(path) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
}
const { turnRecovery } = await moduleFromFile("../src/local_agent/ui/js/recovery.js");
const { turnsApi } = await moduleFromFile("../src/local_agent/ui/js/api.js");
const user = { role: "user", content: "Запрос" };

test("Завершённый ответ и пустая история не требуют повтора", () => {
  assert.equal(turnRecovery([]), null);
  assert.equal(turnRecovery([user, { role: "assistant", content: "Ответ" }]), null);
});
test("Сохранённый запрос без ответа восстанавливается после обновления", () => {
  const recovery = turnRecovery([user]);
  assert.equal(recovery.content, user.content);
  assert.match(recovery.text, /Запрос сохранён/);
});
test("Остановленный ответ остаётся частичным, действия не отменяются", () => {
  const recovery = turnRecovery([
    user, { role: "assistant", tool_calls: [{ id: "write" }] },
    { role: "tool", content: "Записано" },
    { role: "assistant", content: "Начало\n\n_(ответ прерван)_" },
  ]);
  assert.equal(recovery.tools, true);
  assert.match(recovery.text, /Частичный ответ сохранён/);
  assert.match(recovery.text, /действия не отменены/);
});
test("Ошибка до сохранения не приписывает инструменты предыдущего хода", () => {
  const recovery = turnRecovery([user, { role: "tool" }], {
    content: "Новый запрос", saved: false, reason: "Сеть недоступна",
  });
  assert.equal(recovery.tools, false);
  assert.equal(recovery.content, "Новый запрос");
  assert.match(recovery.text, /не подтверждено/);
});
test("История подтверждает сохранение после сетевой неопределённости", () => {
  const recovery = turnRecovery([user], { content: user.content, saved: false });
  assert.match(recovery.text, /Запрос сохранён/);
});
test("Инструменты предыдущего хода не попадают в повтор нового", () => {
  assert.equal(turnRecovery([user, { role: "tool" }, { role: "user", content: "Ещё" }]).tools, false);
});

function response(events) {
  return new Response(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""));
}
test("SSE принимает успешное завершение", async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async () => response([{ type: "user_message" }, { type: "done" }]);
  try {
    const events = [];
    await turnsApi.stream("s", "Текст", undefined, (event) => events.push(event.type));
    assert.deepEqual(events, ["user_message", "done"]);
  } finally { globalThis.fetch = previous; }
});
test("Закрытие SSE без done считается сбоем, а не успехом", async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async () => response([{ type: "user_message" }, { type: "delta", text: "Начало" }]);
  try {
    await assert.rejects(turnsApi.stream("s", "Текст", undefined, () => {}), /до завершения/);
  } finally { globalThis.fetch = previous; }
});
test("Ошибка SSE сохраняет серверный признак сохранённого запроса", async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async () => response([{ type: "error", user_message_saved: true, message: "Сбой" }]);
  try {
    await assert.rejects(turnsApi.stream("s", "Текст", undefined, (event) => {
      const error = new Error(event.message);
      error.messageSaved = event.user_message_saved;
      throw error;
    }), (error) => error.messageSaved === true);
  } finally { globalThis.fetch = previous; }
});
