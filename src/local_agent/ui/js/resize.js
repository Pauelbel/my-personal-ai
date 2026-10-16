// Разделители панелей: потяните — меняется ширина колонки слева от разделителя, двойной щелчок — ширина по умолчанию.
// Ширина хранится в CSS-переменной на контейнере сетки и запоминается в браузере; стрелки двигают разделитель с клавиатуры.

import { forget, recall, remember } from "./util.js";

const STEP = 16;
// Колонке справа всегда оставляем место, чтобы её нельзя было сжать в ноль.
const KEEP_RIGHT = 240;

// variables — CSS-переменные ширины; если их несколько (список и граф вики), pick() выбирает текущую.
export function makeResizable(handle, { grid, variables, pick: variable = () => variables[0], min = 180, max = Infinity }) {
  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", "vertical");
  handle.tabIndex = 0;
  handle.title = "Потяните, чтобы изменить ширину; двойной щелчок — как было";
  const key = (name = variable()) => `width${name}`;
  const clamp = (width) => Math.round(Math.min(Math.max(width, min), max, Math.max(grid.clientWidth - KEEP_RIGHT, min)));
  const apply = (width) => {
    grid.style.setProperty(variable(), `${width}px`);
    remember(key(), String(width));
  };
  const column = () => {
    let node = handle.previousElementSibling;
    while (node && node.hidden) node = node.previousElementSibling;
    return node;
  };

  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const start = event.clientX;
    const width = column()?.getBoundingClientRect().width ?? min;
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing");
    const move = (moved) => apply(clamp(width + moved.clientX - start));
    const end = () => {
      handle.removeEventListener("pointermove", move);
      document.body.classList.remove("resizing");
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end, { once: true });
    handle.addEventListener("pointercancel", end, { once: true });
  });
  handle.addEventListener("dblclick", () => {
    grid.style.removeProperty(variable());
    forget(key());
  });
  handle.addEventListener("keydown", (event) => {
    const delta = { ArrowLeft: -STEP, ArrowRight: STEP }[event.key];
    if (!delta) return;
    event.preventDefault();
    apply(clamp((column()?.getBoundingClientRect().width ?? min) + delta));
  });
  for (const name of variables) {
    const saved = Number(recall(key(name)));
    if (saved) grid.style.setProperty(name, `${saved}px`);
  }
}

