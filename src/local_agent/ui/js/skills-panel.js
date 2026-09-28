// Панель навыков: список навыков агента и правка их инструкций.
import { skillsApi } from "./api.js";
import { appendMarkdown } from "./markdown.js";
import { clearError, elements, render, showError, state } from "./state.js";

let skills = [];
let selectedSkillId = null;
let deletingSkill = false;

export function renderSkillsPanel() {
  elements.skillsEmpty.hidden = skills.length > 0;
  elements.skillsList.replaceChildren(...skills.map((skill) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "memory-file-button";
    button.textContent = skill.name;
    button.title = skill.description;
    button.classList.toggle("active", skill.id === selectedSkillId);
    button.addEventListener("click", () => selectSkill(skill.id));
    return button;
  }));
}

export async function showSkills() {
  state.showingTools = false;
  state.showingSettings = false;
  state.showingMemory = false;
  state.showingPrompt = false;
  state.showingSkills = true;
  state.editingTitle = false;
  elements.skillsStatus.textContent = "";
  render();
  try {
    skills = await skillsApi.list();
    selectedSkillId = skills.some((skill) => skill.id === selectedSkillId) ? selectedSkillId : (skills[0]?.id || null);
    render();
    if (selectedSkillId) await selectSkill(selectedSkillId);
  } catch (error) {
    showError(error);
  }
}

async function selectSkill(skillId) {
  selectedSkillId = skillId;
  elements.skillSave.disabled = true;
  elements.skillView.disabled = true;
  elements.skillEdit.disabled = true;
  elements.skillDelete.disabled = true;
  render();
  try {
    const skill = await skillsApi.read(skillId);
    if (selectedSkillId !== skillId) return;
    showSkill(skill);
    clearError();
  } catch (error) {
    showError(error);
  }
}

function showSkill(skill) {
  elements.skillTitle.textContent = skill.name;
  elements.skillDescription.textContent = `${skill.description}. Файл ${skill.id}.md в папке скиллов; название и описание меняются в нём.`;
  elements.skillContent.value = skill.instructions;
  elements.skillContent.disabled = false;
  elements.skillSave.disabled = false;
  elements.skillView.disabled = false;
  elements.skillEdit.disabled = false;
  elements.skillDelete.disabled = false;
  setSkillEditing(false);
}

export function setSkillEditing(editing) {
  elements.skillContent.hidden = !editing;
  elements.skillSave.hidden = !editing;
  elements.skillPreview.hidden = editing;
  elements.skillView.setAttribute("aria-pressed", String(!editing));
  elements.skillEdit.setAttribute("aria-pressed", String(editing));
  if (editing) {
    elements.skillContent.focus();
  } else {
    elements.skillPreview.replaceChildren();
    appendMarkdown(elements.skillPreview, elements.skillContent.value);
  }
}

export async function deleteSkill() {
  if (!selectedSkillId || deletingSkill) return;
  const skillId = selectedSkillId;
  const name = skills.find((skill) => skill.id === skillId)?.name || elements.skillTitle.textContent;
  if (!window.confirm(`Удалить скилл «${name}»? Агент перестанет его использовать. Файл будет сохранён в архиве для восстановления.`)) return;
  deletingSkill = true;
  elements.skillDelete.disabled = true;
  try {
    await skillsApi.delete(skillId);
    skills = skills.filter((skill) => skill.id !== skillId);
    if (selectedSkillId === skillId) {
      selectedSkillId = null;
      elements.skillTitle.textContent = "Выберите скилл";
      elements.skillDescription.textContent = "";
      elements.skillContent.value = "";
      elements.skillContent.disabled = true;
      elements.skillSave.disabled = true;
      elements.skillView.disabled = true;
      elements.skillEdit.disabled = true;
      setSkillEditing(false);
      if (skills.length) await selectSkill(skills[0].id);
    }
    render();
    elements.skillsStatus.textContent = "Скилл удалён. Его файл сохранён в архиве.";
    clearError();
  } catch (error) {
    showError(error);
  } finally {
    deletingSkill = false;
    elements.skillDelete.disabled = !selectedSkillId;
  }
}

export function initSkillDialog() {
  let creating = false;
  elements.skillAdd.addEventListener("click", () => {
    elements.skillCreateError.textContent = "";
    elements.skillDialog.showModal();
    elements.newSkillName.focus();
  });
  const close = () => { if (!creating) elements.skillDialog.close(); };
  elements.skillClose.addEventListener("click", close);
  elements.skillCancel.addEventListener("click", close);
  elements.skillDialog.addEventListener("cancel", (event) => { if (creating) event.preventDefault(); });
  elements.skillForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (creating) return;
    creating = true;
    elements.skillCreate.disabled = true;
    elements.skillClose.disabled = true;
    elements.skillCancel.disabled = true;
    elements.skillCreateError.textContent = "";
    try {
      const skill = await skillsApi.create({
        name: elements.newSkillName.value,
        description: elements.newSkillDescription.value,
        instructions: elements.newSkillInstructions.value,
      });
      skills.push(skill);
      selectedSkillId = skill.id;
      showSkill(skill);
      render();
      elements.skillsStatus.textContent = "Скилл добавлен и доступен агенту со следующего сообщения.";
      elements.skillDialog.close();
      elements.skillForm.reset();
      clearError();
    } catch (error) {
      elements.skillCreateError.textContent = error.message;
    } finally {
      creating = false;
      elements.skillCreate.disabled = false;
      elements.skillClose.disabled = false;
      elements.skillCancel.disabled = false;
    }
  });
  initSkillArchive();
}

function initSkillArchive() {
  let archived = [];
  let busy = false;
  const draw = () => {
    elements.skillArchiveList.replaceChildren(...archived.map((skill) => {
      const row = document.createElement("div");
      row.className = "skill-archive-row";
      const text = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = skill.name;
      const description = document.createElement("p");
      description.textContent = skill.description;
      text.append(name, description);
      const restore = document.createElement("button");
      restore.type = "button";
      restore.textContent = "Восстановить";
      restore.disabled = busy;
      restore.setAttribute("aria-label", `Восстановить скилл «${skill.name}»`);
      restore.addEventListener("click", () => run(async () => {
        const restored = await skillsApi.restore(skill.archive_id);
        skills = [...skills.filter((item) => item.id !== restored.id), restored];
        selectedSkillId = restored.id;
        showSkill(restored);
        render();
        elements.skillsStatus.textContent = "Скилл восстановлен и доступен агенту.";
      }));
      row.append(text, restore);
      return row;
    }));
    elements.skillArchiveClear.disabled = busy || !archived.length;
    elements.skillArchiveClose.disabled = busy;
  };
  const refresh = async () => {
    archived = await skillsApi.archive();
    elements.skillArchiveStatus.textContent = archived.length ? `Скиллов в архиве: ${archived.length}` : "Архив пуст.";
    draw();
  };
  const run = async (action) => {
    if (busy) return;
    busy = true;
    draw();
    try {
      await action();
      await refresh();
    } catch (error) {
      elements.skillArchiveStatus.textContent = error.message;
    } finally {
      busy = false;
      draw();
    }
  };
  elements.skillArchive.addEventListener("click", () => {
    archived = [];
    elements.skillArchiveStatus.textContent = "Загрузка архива…";
    elements.skillArchiveDialog.showModal();
    void run(async () => {});
  });
  elements.skillArchiveClose.addEventListener("click", () => { if (!busy) elements.skillArchiveDialog.close(); });
  elements.skillArchiveDialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  elements.skillArchiveClear.addEventListener("click", () => {
    if (busy || !archived.length) return;
    if (!window.confirm(`Безвозвратно удалить все скиллы из архива (${archived.length})? Восстановить их через приложение будет невозможно.`)) return;
    void run(() => skillsApi.clearArchive());
  });
}

export async function saveSkill() {
  if (!selectedSkillId) return;
  const skillId = selectedSkillId;
  elements.skillSave.disabled = true;
  try {
    const skill = await skillsApi.save(skillId, elements.skillContent.value);
    if (selectedSkillId === skillId) showSkill(skill);
    elements.skillsStatus.textContent = "Скилл сохранён. Он действует со следующего сообщения.";
    clearError();
  } catch (error) {
    elements.skillsStatus.textContent = `Не удалось сохранить: ${error.message}`;
    showError(error);
  } finally {
    elements.skillSave.disabled = false;
  }
}
