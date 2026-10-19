// Диаграммы Mermaid: блок ```mermaid в ответе, заметке или файле рисуется схемой.
// Библиотека лежит в проекте (ui/vendor, без CDN) и грузится, только когда диаграмма встретилась впервые.
const LIBRARY = "/ui/vendor/mermaid.min.js";
let loading = null;
let drawn = 0;

export function renderDiagrams(target) {
  for (const code of target.querySelectorAll('pre > code[data-language="mermaid"]')) {
    void draw(code.parentElement, code.textContent);
  }
}

function library() {
  loading ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = LIBRARY;
    script.onload = () => resolve(window.mermaid);
    script.onerror = () => {
      loading = null;
      reject(new Error("Не удалось загрузить mermaid"));
    };
    document.head.append(script);
  });
  return loading;
}

async function draw(pre, source) {
  const box = document.createElement("div");
  box.className = "diagram";
  pre.before(box);
  pre.hidden = true;
  const id = `diagram-${++drawn}`;
  try {
    const mermaid = await library();
    mermaid.initialize({
      startOnLoad: false, securityLevel: "strict",
      theme: document.documentElement.dataset.theme === "dark" ? "dark" : "neutral",
    });
    // Ошибку синтаксиса ловим до отрисовки: иначе mermaid оставит в странице свою картинку с ошибкой.
    await mermaid.parse(source);
    const { svg } = await mermaid.render(id, source);
    box.innerHTML = svg;
  } catch {
    // Модель написала диаграмму с ошибкой — показываем её текстом, как обычный код.
    document.getElementById(`d${id}`)?.remove();
    box.remove();
    pre.hidden = false;
    return;
  }
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "diagram-source";
  toggle.textContent = "‹› код";
  toggle.title = "Показать текст диаграммы";
  toggle.addEventListener("click", () => {
    pre.hidden = !pre.hidden;
  });
  box.append(toggle);
}
