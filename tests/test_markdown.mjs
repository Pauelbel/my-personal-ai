// Этот тест проверяет таблицы и безопасность локального Markdown-рендерера без браузера.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

class MockNode {
  constructor(tag, value = "") {
    this.tag = tag;
    this.value = value;
    this.children = [];
    this.style = {};
  }

  append(child) { this.children.push(child); }
  set textContent(value) { this.children = []; this.value = value; }
  get textContent() { return this.value + this.children.map((child) => child.textContent).join(""); }
}

globalThis.document = {
  createElement: (tag) => new MockNode(tag),
  createTextNode: (value) => new MockNode("#text", value),
};
globalThis.window = { location: { href: "http://localhost:8000/" } };

const source = readFileSync(new URL("../src/local_agent/ui/js/markdown.js", import.meta.url), "utf8");
const { appendMarkdown } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

function find(node, tag) {
  return node.children.flatMap((child) => [
    ...(child.tag === tag ? [child] : []),
    ...find(child, tag),
  ]);
}

const root = new MockNode("div");
appendMarkdown(root, `## Список покупок

| Продукт | Цена |
| --- | ---: |
| Яблоки | **150 ₽**<br>Длинное описание шага |

- Первый пункт
- Второй пункт

\`\`\`python
print("ok")
\`\`\`

<script>alert(1)</script> [опасная](javascript:alert) [ссылка](https://example.com)`);

assert.equal(find(root, "table").length, 1);
assert.equal(find(root, "th").length, 2);
assert.equal(find(root, "td").length, 2);
assert.equal(find(root, "strong")[0].textContent, "150 ₽");
assert.equal(find(root, "br").length, 1);
assert.match(find(root, "td")[1].textContent, /Длинное описание шага/);
assert.equal(find(root, "li").length, 2);
assert.equal(find(root, "pre").length, 1);
assert.equal(find(root, "script").length, 0);
assert.equal(find(root, "a").length, 1);
assert.equal(find(root, "a")[0].href, "https://example.com/");
assert.match(root.textContent, /<script>alert\(1\)<\/script>/);

// Подчёркивание внутри имени — не курсив, а по краям слова — курсив.
const names = new MockNode("div");
appendMarkdown(names, "Вызови read_file и git_log, затем _важно_ и __жирно__.");
assert.equal(find(names, "em").length, 1);
assert.equal(find(names, "em")[0].textContent, "важно");
assert.equal(find(names, "strong")[0].textContent, "жирно");
assert.match(names.textContent, /read_file и git_log/);
