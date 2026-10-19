// Раздел «Память»: файлы data/memory/*.md — просмотр и ручная правка; «Обновить из разговора» разбирает
// новые сообщения открытой сессии сразу, не дожидаясь автообновления.
import { api } from "./api.js";
import { treeRow } from "./tree.js";

const $ = (selector) => document.querySelector(selector);
const state = { files: [], current: "profile.md" };

export async function showMemory(toast) {
  try {
    state.files = await api.memory();
    if (!state.files.some((file) => file.name === state.current)) state.current = state.files[0]?.name;
    render(toast);
  } catch (error) {
    toast(error);
  }
}

function render(toast) {
  $("#memory-list").replaceChildren(...state.files.map((file) => treeRow({
    label: file.name, current: file.name === state.current, note: `${(file.content.match(/^- /gm) || []).length}`,
    onClick: () => {
      state.current = file.name;
      render(toast);
    },
  })));
  const file = state.files.find((item) => item.name === state.current);
  $("#memory-text").value = file?.content ?? "";
  $("#memory-status").textContent = "Одна строка «- …» — один факт; разделы — «## …».";
}

export function initMemory(toast, currentSession) {
  $("#memory-save").addEventListener("click", async () => {
    try {
      await api.saveMemory(state.current, $("#memory-text").value);
      await showMemory(toast);
      $("#memory-status").textContent = "Сохранено.";
    } catch (error) {
      $("#memory-status").textContent = error.message;
    }
  });
  $("#memory-update").addEventListener("click", async () => {
    const session = currentSession();
    if (!session) return toast(new Error("Откройте сессию, из разговора которой обновить память"));
    $("#memory-status").textContent = "Разбираю новые сообщения…";
    try {
      const result = await api.updateMemory(session.id);
      await showMemory(toast);
      $("#memory-status").textContent = `Реплик разобрано: ${result.processed}, записей изменено: ${result.applied}`
        + (result.skipped.length ? `, пропущено: ${result.skipped.length}` : "");
    } catch (error) {
      $("#memory-status").textContent = error.message;
    }
  });
}
