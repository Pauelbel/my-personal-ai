// Мелочи, общие для модулей интерфейса: localStorage без исключений, склонение, высота поля ввода по тексту.

export function remember(key, value) {
  try { localStorage.setItem(key, value); } catch { /* без хранилища просто не запомним */ }
}

export function forget(key) {
  try { localStorage.removeItem(key); } catch { /* нечего забывать */ }
}

export function recall(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

// Форма слова для числа: 1 шаг, 2 шага, 5 шагов, 11 шагов.
export function plural(count, one, few, many) {
  const tens = count % 100;
  const units = count % 10;
  if (tens >= 11 && tens <= 14) return many;
  return units === 1 ? one : units >= 2 && units <= 4 ? few : many;
}

// Сброс высоты в auto перестраивает всю страницу вместе с длинной лентой — делаем его, только когда текст
// стал короче; пока текст растёт, хватает scrollHeight без сброса.
const typedLengths = new WeakMap();
export function fitInput(input, maxShare = 0.4) {
  if (input.value.length < (typedLengths.get(input) ?? 0)) input.style.height = "auto";
  typedLengths.set(input, input.value.length);
  const height = `${Math.min(input.scrollHeight, window.innerHeight * maxShare)}px`;
  if (input.style.height !== height) input.style.height = height;
}

export function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function parse(raw) {
  try {
    const value = typeof raw === "string" ? JSON.parse(raw || "{}") : raw;
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}
