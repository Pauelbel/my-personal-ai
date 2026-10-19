// Диалог проекта: название и папка; папку выбирают в списке папок сервера (браузер не отдаёт странице путь).
import { api } from "./api.js";
import { el } from "./util.js";

const $ = (selector) => document.querySelector(selector);
// hooks — что сделать после сохранения ({saved(project, created)}) и удаления ({deleted()}).
const dialog = { project: null, autoName: null, hooks: null };

export function initProjects(hooks) {
  dialog.hooks = hooks;
  $("#project-form").addEventListener("submit", saveProject);
  $("#project-cancel").addEventListener("click", () => $("#project-dialog").close());
  $("#project-delete").addEventListener("click", deleteProject);
}

const baseName = (path) => path.split(/[\\/]/).filter(Boolean).pop() ?? path;

export function openProject(project = null) {
  dialog.project = project;
  dialog.autoName = project && project.name === baseName(project.path) ? project.name : null;
  $("#project-dialog-title").textContent = project ? "Настройки проекта" : "Новый проект";
  $("#project-name").value = project?.name ?? "";
  $("#project-path").value = project?.path ?? "";
  $("#project-error").textContent = "";
  $("#project-delete").hidden = !project;
  $("#project-dialog").showModal();
  void browse(project?.path ?? "");
}

async function browse(path) {
  try {
    const listing = await api.folders(path);
    $("#folder-current").textContent = listing.path ?? "Корень";
    $("#folder-up").disabled = !listing.path;
    $("#folder-up").onclick = () => browse(listing.parent ?? "");
    $("#folder-list").replaceChildren(...listing.folders.map((folder) => {
      const item = el("li");
      const button = el("button", "", `📁 ${folder.name}`);
      button.type = "button";
      button.addEventListener("click", () => {
        $("#project-path").value = folder.path;
        const input = $("#project-name");
        if (!input.value.trim() || input.value === dialog.autoName) input.value = dialog.autoName = baseName(folder.path);
        void browse(folder.path);
      });
      item.append(button);
      return item;
    }));
  } catch (error) {
    $("#project-error").textContent = error.message;
  }
}

async function saveProject(event) {
  event.preventDefault();
  const name = $("#project-name").value.trim();
  const path = $("#project-path").value.trim();
  try {
    const created = !dialog.project;
    const project = created ? await api.createProject(name, path) : await api.updateProject(dialog.project.id, name, path);
    $("#project-dialog").close();
    await dialog.hooks.saved(project, created);
  } catch (error) {
    $("#project-error").textContent = error.message;
  }
}

async function deleteProject() {
  const project = dialog.project;
  if (!project || !window.confirm(`Удалить проект «${project.name}» вместе с его сессиями? Папка на диске останется.`)) return;
  try {
    await api.deleteProject(project.id);
    $("#project-dialog").close();
    await dialog.hooks.deleted();
  } catch (error) {
    $("#project-error").textContent = error.message;
  }
}
