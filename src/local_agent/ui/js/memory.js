// Этот модуль отображает список Markdown-файлов долговременной памяти.
export function renderMemoryFiles(container, documents, selectedName, onSelect) {
  container.replaceChildren();
  for (const document of documents) {
    const button = documentNode("button", document.title);
    button.type = "button";
    button.className = "memory-file-button";
    button.classList.toggle("active", document.name === selectedName);
    button.addEventListener("click", () => onSelect(document.name));
    container.append(button);
  }
}

const descriptions = {
  "profile.md": "Устойчивые сведения о пользователе. Например: имя — Алексей; работает Python-разработчиком.",
  "preferences.md": "Предпочтения в общении и работе. Например: отвечать по-русски; вносить изменения небольшими этапами.",
  "projects.md": "Долгосрочные проекты и их состояние. Например: разрабатывает Meepo Agent; проект использует локальную LLM.",
  "decisions.md": "Принятые решения и договорённости, которые важно соблюдать дальше. Например: хранить историю диалогов в JSONL; не удалять данные без подтверждения.",
};

export function memoryDocumentDescription(name) {
  return descriptions[name]
    || "Дополнительная категория долговременной памяти пользователя.";
}

function documentNode(tag, text) {
  const node = window.document.createElement(tag);
  node.textContent = text;
  return node;
}
