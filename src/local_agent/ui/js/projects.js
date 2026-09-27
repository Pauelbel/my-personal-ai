// Диалог проекта: создание, переименование, смена папки и удаление.
import { projectsApi } from "./api.js";
import { pickFolder } from "./folder-picker.js";
import { createSession, loadSessions } from "./sessions.js";
import { clearError, elements, showError } from "./state.js";

let editing = null;
let folder = null;

export function openProjectDialog(project = null) {
  editing = project;
  folder = project?.workspace ?? null;
  elements.projectDialogTitle.textContent = project ? "Настройки проекта" : "Новый проект";
  elements.projectSave.textContent = project ? "Сохранить" : "Создать";
  elements.projectDelete.hidden = !project;
  elements.projectName.value = project?.name ?? "";
  elements.projectError.hidden = true;
  renderFolder();
  elements.projectDialog.showModal();
  if (project) elements.projectName.focus();
  else void chooseFolder();
}

export function initProjectDialog() {
  elements.projectChooseFolder.addEventListener("click", () => { void chooseFolder(); });
  elements.projectForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void save();
  });
  elements.projectDelete.addEventListener("click", () => { void remove(); });
  for (const button of [elements.projectCancel, elements.projectClose]) {
    button.addEventListener("click", () => elements.projectDialog.close());
  }
}

async function chooseFolder() {
  const chosen = await pickFolder(folder);
  if (!chosen) return;
  folder = chosen;
  // Имя проекта по умолчанию — имя папки, пока пользователь не задал своё.
  if (!elements.projectName.value.trim()) elements.projectName.value = chosen.split(/[\\/]/).filter(Boolean).pop() || chosen;
  renderFolder();
}

function renderFolder() {
  elements.projectFolder.textContent = folder ?? "Не выбрана";
  elements.projectFolder.title = folder ?? "";
}

async function save() {
  const name = elements.projectName.value.trim();
  if (!folder) {
    showDialogError("Выберите папку проекта");
    return;
  }
  elements.projectSave.disabled = true;
  try {
    if (editing) {
      await projectsApi.update(editing.id, { name, workspace: folder });
      elements.projectDialog.close();
      await loadSessions();
    } else {
      const project = await projectsApi.create({ name, workspace: folder });
      elements.projectDialog.close();
      // Новый проект сразу открывается первой сессией: в нём можно работать без лишних кликов.
      await createSession(project.id);
    }
    clearError();
  } catch (error) {
    showDialogError(error.message);
  } finally {
    elements.projectSave.disabled = false;
  }
}

async function remove() {
  if (!editing) return;
  const confirmed = window.confirm(
    `Удалить проект «${editing.name}»? Папка и файлы на диске не трогаются. `
    + "Сессии проекта останутся в разделе «Без проекта», но без доступа к файлам.",
  );
  if (!confirmed) return;
  try {
    await projectsApi.delete(editing.id);
    elements.projectDialog.close();
    await loadSessions();
  } catch (error) {
    showError(error);
  }
}

function showDialogError(message) {
  elements.projectError.textContent = message;
  elements.projectError.hidden = false;
}
