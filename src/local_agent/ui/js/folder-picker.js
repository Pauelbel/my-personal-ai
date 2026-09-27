// Выбор папки: браузер не отдаёт странице настоящий путь, поэтому папки показывает сервер.
import { foldersApi } from "./api.js";
import { elements } from "./state.js";

let listing = null;
let resolveChoice = null;
// Номер последнего запроса: при быстрых кликах ответы могут прийти не по порядку.
let latestRequest = 0;

// Открывает диалог и возвращает выбранный абсолютный путь или null, если выбор отменён.
export function pickFolder(initialPath = null) {
  finish(null);
  return new Promise((resolve) => {
    resolveChoice = resolve;
    elements.folderDialog.showModal();
    void open(initialPath);
  });
}

export function initFolderPicker() {
  elements.folderPathForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void open(elements.folderPath.value.trim() || null);
  });
  elements.folderUp.addEventListener("click", () => { void open(listing?.parent ?? null); });
  elements.folderSelect.addEventListener("click", () => finish(listing?.path ?? null));
  elements.folderCancel.addEventListener("click", () => finish(null));
  elements.folderClose.addEventListener("click", () => finish(null));
  // Escape закрывает диалог сам: выбор при этом считается отменённым.
  elements.folderDialog.addEventListener("close", () => finish(null));
}

async function open(path) {
  const request = ++latestRequest;
  elements.folderError.hidden = true;
  try {
    const next = await foldersApi.list(path);
    if (request !== latestRequest) return;
    listing = next;
    elements.folderPath.value = next.path ?? "";
    elements.folderUp.disabled = next.path === null;
    elements.folderSelect.disabled = next.path === null;
    const items = next.folders.map((folder) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "folder-item";
      button.textContent = folder.name;
      button.title = folder.path;
      button.addEventListener("click", () => { void open(folder.path); });
      return button;
    });
    if (items.length === 0) {
      const empty = document.createElement("p");
      empty.className = "folder-empty";
      empty.textContent = "Вложенных папок нет";
      items.push(empty);
    }
    elements.folderList.replaceChildren(...items);
    elements.folderList.scrollTop = 0;
  } catch (error) {
    if (request !== latestRequest) return;
    elements.folderError.textContent = error.message;
    elements.folderError.hidden = false;
  }
}

function finish(value) {
  const resolve = resolveChoice;
  resolveChoice = null;
  if (elements.folderDialog.open) elements.folderDialog.close();
  resolve?.(value);
}
