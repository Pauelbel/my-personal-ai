// Этот модуль безопасно превращает распространённую Markdown-разметку в элементы чата.
const WORD = /[\p{L}\p{N}]/u;

function appendText(parent, value) {
  parent.append(document.createTextNode(value));
}

function safeLink(value) {
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function appendInline(parent, source) {
  let index = 0;
  while (index < source.length) {
    const lineBreak = /^<br\s*\/?>/i.exec(source.slice(index));
    if (lineBreak) {
      parent.append(document.createElement("br"));
      index += lineBreak[0].length;
      continue;
    }
    // Обратный слеш экранирует только знаки препинания (\*, \_, \|): «C:\new» и «\n» остаются как есть.
    if (source[index] === "\\" && PUNCTUATION.test(source[index + 1] ?? "")) {
      appendText(parent, source[index + 1]);
      index += 2;
      continue;
    }
    if (source[index] === "`") {
      const end = source.indexOf("`", index + 1);
      if (end !== -1) {
        const code = document.createElement("code");
        code.textContent = source.slice(index + 1, end);
        parent.append(code);
        index = end + 1;
        continue;
      }
    }
    if (source.startsWith("![", index)) {
      const match = /^!\[([^\]]*)\]\(([^\s)]+)\)/.exec(source.slice(index));
      if (match) {
        // Адрес картинки задаёт тот, кто показывает текст (renderMarkdown): у заметки вики он свой.
        const image = document.createElement("img");
        image.alt = match[1];
        image.dataset.src = match[2];
        parent.append(image);
        index += match[0].length;
        continue;
      }
    }
    if (source[index] === "[") {
      const match = /^\[([^\]]+)\]\(([^\s)]+)\)/.exec(source.slice(index));
      if (match) {
        const href = safeLink(match[2]);
        if (href) {
          const link = document.createElement("a");
          link.href = href;
          // Исходная ссылка как написана: по ней вики открывает связанную заметку относительно текущей.
          link.dataset.href = match[2];
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          appendInline(link, match[1]);
          parent.append(link);
        } else appendText(parent, match[0]);
        index += match[0].length;
        continue;
      }
    }
    let matched = false;
    for (const [marker, tag] of [["**", "strong"], ["__", "strong"], ["~~", "del"], ["*", "em"], ["_", "em"]]) {
      if (!source.startsWith(marker, index)) continue;
      // Подчёркивание внутри слова (git_log, read_file) — часть имени, а не курсив.
      const intraword = marker[0] === "_";
      if (intraword && WORD.test(source[index - 1] ?? "")) continue;
      let end = source.indexOf(marker, index + marker.length);
      while (intraword && end !== -1 && WORD.test(source[end + marker.length] ?? "")) {
        end = source.indexOf(marker, end + 1);
      }
      if (end <= index + marker.length) continue;
      const element = document.createElement(tag);
      appendInline(element, source.slice(index + marker.length, end));
      parent.append(element);
      index = end + marker.length;
      matched = true;
      break;
    }
    if (matched) continue;
    appendText(parent, source[index]);
    index += 1;
  }
}

function splitTableRow(line) {
  const cells = [];
  let cell = "";
  let escaped = false;
  for (const character of line.trim().replace(/^\|/, "").replace(/\|$/, "")) {
    if (escaped) {
      cell += PUNCTUATION.test(character) ? character : `\\${character}`;
      escaped = false;
    } else if (character === "\\") escaped = true;
    else if (character === "|") {
      cells.push(cell.trim());
      cell = "";
    } else cell += character;
  }
  cells.push(cell.trim());
  return cells;
}

function isTableDivider(line) {
  const cells = splitTableRow(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

// Номер строки исходника у блока: по нему просмотр файла рисует колонку номеров.
function mark(element, line) {
  element.dataset.line = String(line);
  return element;
}

function appendTable(parent, header, divider, rows, first) {
  const wrapper = document.createElement("div");
  wrapper.className = "markdown-table-container";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const heading = mark(document.createElement("tr"), first);
  const alignments = splitTableRow(divider);
  for (const [index, value] of splitTableRow(header).entries()) {
    const cell = document.createElement("th");
    cell.scope = "col";
    if (alignments[index]?.startsWith(":") && alignments[index]?.endsWith(":")) cell.style.textAlign = "center";
    else if (alignments[index]?.endsWith(":")) cell.style.textAlign = "right";
    appendInline(cell, value);
    heading.append(cell);
  }
  head.append(heading);
  table.append(head);
  const body = document.createElement("tbody");
  for (const [number, row] of rows.entries()) {
    const line = mark(document.createElement("tr"), first + 2 + number);
    for (const value of splitTableRow(row)) {
      const cell = document.createElement("td");
      appendInline(cell, value);
      line.append(cell);
    }
    body.append(line);
  }
  table.append(body);
  wrapper.append(table);
  parent.append(wrapper);
}

const PUNCTUATION = /^[!-/:-@[-`{-~]$/;
const LIST_ITEM = /^(\s*)([-+*]|\d+[.)])\s+(.+)$/;

function indentOf(line) {
  return line.length - line.trimStart().length;
}

function isBlockStart(line) {
  return /^\s*(`{3,}|~{3,}|#{1,6}\s|>|[-+*]\s|\d+[.)]\s)/.test(line);
}

// offset — сколько строк исходника выше этого куска (для вложенных цитат).
export function appendMarkdown(parent, source, offset = 0) {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    const start = offset + index + 1;

    const fence = /^\s*(`{3,}|~{3,})/.exec(line);
    if (fence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !lines[index].trimStart().startsWith(fence[1])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = mark(document.createElement("pre"), start);
      const code = document.createElement("code");
      const language = line.trim().slice(fence[1].length).trim().split(/\s+/)[0];
      if (language) code.dataset.language = language;
      code.textContent = codeLines.join("\n");
      pre.append(code);
      parent.append(pre);
      continue;
    }

    const heading = /^\s*(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      const element = mark(document.createElement(`h${heading[1].length}`), start);
      appendInline(element, heading[2]);
      parent.append(element);
      index += 1;
      continue;
    }

    if (index + 1 < lines.length && line.includes("|") && isTableDivider(lines[index + 1])) {
      const rows = [];
      const divider = lines[index + 1];
      index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
        rows.push(lines[index]);
        index += 1;
      }
      appendTable(parent, line, divider, rows, start);
      continue;
    }

    if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      parent.append(mark(document.createElement("hr"), start));
      index += 1;
      continue;
    }

    if (/^\s*>/.test(line)) {
      const quoteLines = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      const quote = document.createElement("blockquote");
      appendMarkdown(quote, quoteLines.join("\n"), start - 1);
      parent.append(quote);
      continue;
    }

    const listMatch = LIST_ITEM.exec(line);
    if (listMatch) {
      const base = listMatch[1].length;
      const ordered = /\d/.test(listMatch[2][0]);
      const list = document.createElement(ordered ? "ol" : "ul");
      // «2. Баг-репорты» после маркированного списка — продолжение нумерации, а не новый пункт 1.
      const first = Number.parseInt(listMatch[2], 10);
      if (ordered && first > 1) list.start = first;
      let item = null;
      while (index < lines.length) {
        const current = lines[index];
        // Строки глубже пунктов списка — вложенный список или текст последнего пункта.
        if (item && current.trim() && indentOf(current) > base) {
          const nested = [];
          const from = index;
          while (index < lines.length && lines[index].trim() && indentOf(lines[index]) > base) nested.push(lines[index++]);
          const shift = Math.min(...nested.map(indentOf));
          appendMarkdown(item, nested.map((text) => text.slice(shift)).join("\n"), offset + from);
          continue;
        }
        const itemMatch = LIST_ITEM.exec(current);
        if (!itemMatch || itemMatch[1].length !== base || /\d/.test(itemMatch[2][0]) !== ordered) break;
        item = mark(document.createElement("li"), offset + index + 1);
        appendInline(item, itemMatch[3]);
        list.append(item);
        index += 1;
      }
      parent.append(list);
      continue;
    }

    const paragraph = [];
    while (index < lines.length && lines[index].trim()) {
      if (paragraph.length && (isBlockStart(lines[index]) || (index + 1 < lines.length && isTableDivider(lines[index + 1])))) break;
      paragraph.push(lines[index].trim());
      index += 1;
    }
    const element = mark(document.createElement("p"), start);
    appendInline(element, paragraph.join(" "));
    parent.append(element);
  }
}
