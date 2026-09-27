// Панель навыков: список навыков агента и правка их инструкций.
import { skillsApi } from "./api.js";
import { clearError, elements, render, showError, state } from "./state.js";

let skills = [];
let selectedSkillId = null;

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
  elements.skillDescription.textContent = `${skill.description}. Файл ${skill.id}.md в папке навыков; название и описание меняются в нём.`;
  elements.skillContent.value = skill.instructions;
  elements.skillContent.disabled = false;
  elements.skillSave.disabled = false;
}

export async function saveSkill() {
  if (!selectedSkillId) return;
  const skillId = selectedSkillId;
  elements.skillSave.disabled = true;
  try {
    const skill = await skillsApi.save(skillId, elements.skillContent.value);
    if (selectedSkillId === skillId) showSkill(skill);
    elements.skillsStatus.textContent = "Навык сохранён. Он действует со следующего сообщения.";
    clearError();
  } catch (error) {
    elements.skillsStatus.textContent = `Не удалось сохранить: ${error.message}`;
    showError(error);
  } finally {
    elements.skillSave.disabled = false;
  }
}
