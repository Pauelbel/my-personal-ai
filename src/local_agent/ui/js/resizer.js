// Разделитель панелей: ширина левой панели тянется мышью или стрелками и запоминается в этом браузере.
const KEY_STEP = 20;

export function initResizer({ handle, container, variable, storageKey, defaultWidth, min, reserve }) {
  // Правой части всегда оставляем не меньше `reserve` пикселей, иначе холст схлопнется.
  const clamp = (width) => Math.round(Math.min(
    Math.max(width, min), Math.max(min, container.getBoundingClientRect().width - reserve),
  ));
  const apply = (width, save) => {
    const value = clamp(width);
    container.style.setProperty(variable, `${value}px`);
    handle.setAttribute("aria-valuenow", String(value));
    if (!save) return;
    try {
      localStorage.setItem(storageKey, String(value));
    } catch {
      // Без хранилища ширина просто не запомнится.
    }
  };
  const current = () => handle.previousElementSibling.getBoundingClientRect().width;

  handle.setAttribute("aria-valuemin", String(min));
  let saved = null;
  try {
    saved = Number(localStorage.getItem(storageKey)) || null;
  } catch {
    saved = null;
  }
  if (saved) container.style.setProperty(variable, `${saved}px`);

  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("dragging");
    container.classList.add("resizing");
    const start = { x: event.clientX, width: current() };
    const move = (moved) => apply(start.width + moved.clientX - start.x, false);
    const stop = () => {
      handle.removeEventListener("pointermove", move);
      handle.classList.remove("dragging");
      container.classList.remove("resizing");
      apply(current(), true);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", stop, { once: true });
    handle.addEventListener("pointercancel", stop, { once: true });
  });
  handle.addEventListener("keydown", (event) => {
    const delta = { ArrowLeft: -KEY_STEP, ArrowRight: KEY_STEP }[event.key];
    if (!delta) return;
    event.preventDefault();
    apply(current() + delta, true);
  });
  handle.addEventListener("dblclick", () => apply(defaultWidth, true));
}
