// Значки агентов и проектов: буква и постоянный цвет — одинаковые в сайдбаре, кружках сессии и карточках.

export function initial(name) {
  return (name.trim()[0] ?? "•").toUpperCase();
}

// Цвет значка — из палитры кластеров по id: у проекта и агента он постоянный и не зависит от порядка в списке.
export function tone(id) {
  let hash = 0;
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return `var(--cluster-${(hash % 8) + 1})`;
}
