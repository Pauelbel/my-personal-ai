// Формы блока «Агенты»: саб-агент — создание и правка вручную (из сайдбара и со вкладки «Возможности»),
// основной агент — только дополнительные инструкции поверх встроенного промпта.
import { api } from "./api.js";
import { el } from "./chat.js";

const $ = (selector) => document.querySelector(selector);
let editing = null;
let saved = null;

// agent — правка существующего, null — новый; onSaved(agent) — после сохранения.
export async function openAgentForm(agent, onSaved) {
  editing = agent;
  saved = onSaved;
  let catalog;
  try {
    catalog = await api.capabilities();
  } catch (error) {
    $("#agent-error").textContent = error.message;
    catalog = { tools: [], skills: [] };
  }
  $("#agent-dialog-title").textContent = agent ? `Агент ${agent.name}` : "Новый агент";
  $("#agent-id-label").hidden = Boolean(agent);
  $("#agent-id").required = !agent;
  $("#agent-id").value = "";
  $("#agent-name").value = agent?.name ?? "";
  $("#agent-description").value = agent?.description ?? "";
  $("#agent-prompt").value = agent?.system_prompt ?? "";
  $("#agent-error").textContent = "";
  // Инструменты MCP подключаются у сервера, а не у агента: в списке — свои, дополнительные и созданные агентом.
  const tools = catalog.tools.filter((tool) => tool.origin !== "mcp");
  $("#agent-tools").replaceChildren(...checks("tool", tools, new Set(agent?.tools ?? [])));
  // К кому обращаться: пустой список в файле — ко всем агентам команды, и к будущим тоже.
  let team = [];
  try {
    team = (await api.agents()).filter((item) => !item.builtin && item.id !== agent?.id);
  } catch {
    // Без списка агентов останется «ко всем».
  }
  $("#agent-agents").replaceChildren(...checks("agent", team, new Set(agent?.agents ?? []), (item) => item.name));
  if (!team.length) $("#agent-agents").append(el("p", "hint", "Других агентов пока нет."));
  $("#agent-all-agents").checked = !agent?.agents?.length;
  syncAgents();
  $("#agent-skills").replaceChildren(...checks("skill", catalog.skills, new Set(agent?.skills ?? [])));
  if (!catalog.skills.length) $("#agent-skills").append(el("p", "hint", "Скиллов пока нет."));
  $("#agent-all-skills").checked = Boolean(agent) && agent.skills === null;
  syncSkills();
  $("#agent-dialog").showModal();
}

export function initAgentForm() {
  $("#agent-form").addEventListener("submit", save);
  $("#agent-cancel").addEventListener("click", () => $("#agent-dialog").close());
  $("#agent-all-skills").addEventListener("change", syncSkills);
  $("#agent-all-agents").addEventListener("change", syncAgents);
  $("#main-form").addEventListener("submit", saveMain);
  $("#main-cancel").addEventListener("click", () => $("#main-dialog").close());
}

export function openMainForm(agent, onSaved) {
  saved = onSaved;
  $("#main-instructions").value = agent.instructions ?? "";
  $("#main-error").textContent = "";
  $("#main-dialog").showModal();
}

async function saveMain(event) {
  event.preventDefault();
  try {
    await api.saveMainInstructions($("#main-instructions").value);
    $("#main-dialog").close();
    await saved?.();
  } catch (error) {
    $("#main-error").textContent = error.message;
  }
}

function checks(kind, items, selected, label = (item) => item.id) {
  return items.map((item) => {
    const row = el("label", "check");
    const box = el("input");
    box.type = "checkbox";
    box.value = item.id;
    box.dataset.kind = kind;
    box.checked = selected.has(item.id);
    row.title = item.description ?? "";
    row.append(box, el("span", "", label(item)));
    return row;
  });
}

// «Ко всем агентам» — без списка: агент обращается к любому, включая тех, кто появится потом.
function syncAgents() {
  const all = $("#agent-all-agents").checked;
  for (const box of $("#agent-agents").querySelectorAll("input")) box.disabled = all;
}

// «Все скиллы» — без списка: агенту доступны все, включая те, что появятся потом.
function syncSkills() {
  const all = $("#agent-all-skills").checked;
  for (const box of $("#agent-skills").querySelectorAll("input")) box.disabled = all;
}

function picked(root) {
  return [...root.querySelectorAll("input:checked")].map((box) => box.value);
}

async function save(event) {
  event.preventDefault();
  const fields = {
    name: $("#agent-name").value.trim(),
    description: $("#agent-description").value.trim(),
    system_prompt: $("#agent-prompt").value.trim(),
    tools: picked($("#agent-tools")),
    skills: $("#agent-all-skills").checked ? null : picked($("#agent-skills")),
    agents: $("#agent-all-agents").checked ? [] : picked($("#agent-agents")),
  };
  // Пустой список в файле значит «ко всем»: ограничение без единого агента так не записать.
  if (!$("#agent-all-agents").checked && !fields.agents.length) {
    $("#agent-error").textContent = "Отметьте, к кому агент может обращаться, или включите «ко всем агентам».";
    return;
  }
  try {
    const agent = editing
      ? await api.updateAgent(editing.id, fields)
      : await api.createAgent({ id: $("#agent-id").value.trim(), ...fields });
    $("#agent-dialog").close();
    await saved?.(agent);
  } catch (error) {
    $("#agent-error").textContent = error.message;
  }
}
