// Видимый курсор для браузера агента (Playwright MCP, --init-script): Playwright двигает мышь и кликает
// настоящими событиями, но курсора не рисует. Скрипт показывает точку там, где мышь агента, круг на месте клика
// и подсвечивает поле, в которое агент печатает. Слой не перехватывает события и не влияет на страницу.
(() => {
  if (window.top !== window || window.__agentCursor) return;
  window.__agentCursor = true;

  const host = document.createElement("agent-cursor");
  const root = host.attachShadow({ mode: "closed" });
  root.innerHTML = `<style>
    :host { all: initial; position: fixed; inset: 0; z-index: 2147483647; pointer-events: none; }
    .dot {
      position: fixed; left: 0; top: 0; width: 18px; height: 18px; margin: -9px 0 0 -9px; border-radius: 50%;
      background: rgb(255 59 48 / .85); border: 2px solid #fff; box-shadow: 0 0 0 1px rgb(0 0 0 / .4), 0 2px 8px rgb(0 0 0 / .35);
      transition: translate .35s cubic-bezier(.3, .7, .4, 1), scale .12s ease-out; opacity: 0;
    }
    .dot.shown { opacity: 1; }
    .dot.down { scale: .7; }
    .ring {
      position: fixed; width: 44px; height: 44px; margin: -22px 0 0 -22px; border-radius: 50%;
      border: 3px solid rgb(255 59 48); animation: ring .6s ease-out forwards;
    }
    @keyframes ring { from { transform: scale(.3); opacity: 1; } to { transform: scale(1.6); opacity: 0; } }
    .box { position: fixed; border: 3px solid rgb(255 179 0); border-radius: 6px; animation: box 1.2s ease-out forwards; }
    @keyframes box { 0%, 60% { opacity: 1; } to { opacity: 0; } }
  </style><div class="dot"></div>`;
  const dot = root.querySelector(".dot");

  const attach = () => {
    if (!host.isConnected && document.documentElement) document.documentElement.append(host);
  };
  // Playwright переносит мышь одним скачком — точка доезжает до места плавно, чтобы был виден путь.
  // Первый раз точка появляется сразу на месте, а не выезжает из угла.
  const move = (x, y) => {
    attach();
    const first = !dot.classList.contains("shown");
    if (first) dot.style.transition = "none";
    dot.style.translate = `${x}px ${y}px`;
    dot.classList.add("shown");
    if (first) {
      dot.getBoundingClientRect();
      dot.style.transition = "";
    }
    // Клик по ссылке перезагружает страницу: позиция переживает переход, и курсор не пропадает.
    try { sessionStorage.setItem("__agentCursor", JSON.stringify([x, y])); } catch { /* без хранилища — до первого движения */ }
  };
  const ring = (x, y) => {
    const node = document.createElement("div");
    node.className = "ring";
    node.style.left = `${x}px`;
    node.style.top = `${y}px`;
    root.append(node);
    setTimeout(() => node.remove(), 700);
  };
  // Поле, в которое печатают: рамка вокруг него, не чаще раза в секунду на одно поле.
  let boxed = null;
  const box = (element) => {
    if (!(element instanceof Element) || element === boxed) return;
    boxed = element;
    setTimeout(() => { if (boxed === element) boxed = null; }, 1000);
    const rect = element.getBoundingClientRect();
    const node = document.createElement("div");
    node.className = "box";
    Object.assign(node.style, {
      left: `${rect.left - 4}px`, top: `${rect.top - 4}px`, width: `${rect.width + 2}px`, height: `${rect.height + 2}px`,
    });
    attach();
    root.append(node);
    setTimeout(() => node.remove(), 1300);
  };

  const options = { capture: true, passive: true };
  addEventListener("mousemove", (event) => move(event.clientX, event.clientY), options);
  addEventListener("mousedown", (event) => {
    move(event.clientX, event.clientY);
    dot.classList.add("down");
    ring(event.clientX, event.clientY);
  }, options);
  addEventListener("mouseup", () => dot.classList.remove("down"), options);
  addEventListener("input", (event) => box(event.target), options);
  addEventListener("keydown", (event) => box(event.target), options);
  // Страница могла перерисовать документ целиком — возвращаем слой на место.
  new MutationObserver(attach).observe(document, { childList: true });
  try {
    const saved = JSON.parse(sessionStorage.getItem("__agentCursor") || "null");
    if (saved) move(...saved);
  } catch { /* нет сохранённой позиции */ }
})();
