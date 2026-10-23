// Строки дерева в стиле терминала: «├─ ○ имя», группы и папки — ◆ (свёрнутые — ◇), открытый пункт — ●.
// Общие для файлов, вики, памяти и возможностей.
import { el } from "./util.js";

// Отступ строки: по столбику на каждого предка («│» — у предка есть следующие соседи) и ветка к самой строке.
// lines — для каждого уровня предков, остались ли после него соседи; last — последняя ли строка среди соседей.
export function guide(lines, last) {
  if (lines === null) return "";
  return lines.map((more) => (more ? "│  " : "   ")).join("") + (last ? "└─ " : "├─ ");
}

// Продолжение ветки под строкой: для второй строки с описанием.
export function stem(lines, last) {
  if (lines === null) return "  ";
  return lines.map((more) => (more ? "│  " : "   ")).join("") + (last ? "   " : "│  ") + "  ";
}

// group — папка или раздел; open — раскрыт ли он. note — приглушённая подпись справа, extra — кнопки после строки.
// mark и state меняют значок: например, ● с классом running у сессии, в которой идёт ход.
export function treeRow({
  prefix = "", label, note = "", noteClass = "", title = "", group = false, open = true, current = false, onClick,
  extra = [], detail = "", detailPrefix = "", mark = null, state = "", icon = null,
}) {
  const row = el("div", `trow${group ? " group" : ""}${current ? " current" : ""}`);
  const main = el(onClick ? "button" : "div", "trow-main");
  if (onClick) {
    main.type = "button";
    main.addEventListener("click", onClick);
  }
  if (group) main.setAttribute("aria-expanded", String(open));
  main.title = title;
  const sign = mark ?? (group ? (open ? "◆" : "◇") : current ? "●" : "○");
  const marker = el("span", `trow-mark${state ? ` ${state}` : ""}`, icon ? "" : sign);
  if (icon) marker.append(icon);
  main.append(el("span", "trow-guide", prefix), marker, el("span", "trow-label", label));
  if (note) main.append(el("span", `trow-note${noteClass ? ` ${noteClass}` : ""}`, note));
  row.append(main, ...extra.filter(Boolean));
  if (!detail) return row;
  // Описание второй строкой, под той же веткой дерева.
  const block = el("div", "trow-block");
  const more = el("div", "trow-detail");
  more.append(el("span", "trow-guide", detailPrefix), el("span", "trow-text", detail));
  block.append(row, more);
  return block;
}

// Кнопка действия, которая видна при наведении на строку.
export function rowAction(text, title, handler) {
  const button = el("button", "trow-action", text);
  button.type = "button";
  button.title = title;
  button.setAttribute("aria-label", title);
  button.addEventListener("click", handler);
  return button;
}

// Переименование на месте: подпись строки становится полем. Enter или уход фокуса сохраняют, Esc отменяет.
// Поле не кладётся внутрь кнопки строки: на время правки кнопка подменяется блоком с тем же отступом и значком.
export function editLabel(row, value, save) {
  const main = row.querySelector(".trow-main");
  if (!main || row.querySelector(".trow-input")) return;
  const holder = el("div", "trow-main editing");
  for (const part of main.querySelectorAll(".trow-guide, .trow-mark")) holder.append(part.cloneNode(true));
  const input = el("input", "trow-input");
  input.value = value;
  input.maxLength = 100;
  input.setAttribute("aria-label", "Новое название");
  holder.append(input);
  let done = false;
  const finish = async (keep) => {
    if (done) return;
    done = true;
    const text = input.value.trim();
    holder.replaceWith(main);
    if (keep && text && text !== value) {
      main.querySelector(".trow-label").textContent = text;
      await save(text);
    }
  };
  input.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Enter") void finish(true);
    if (event.key === "Escape") void finish(false);
  });
  input.addEventListener("blur", () => void finish(true));
  main.replaceWith(holder);
  input.focus();
  input.select();
}
