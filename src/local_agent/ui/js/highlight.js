// Лёгкая подсветка синтаксиса для просмотра файлов: без библиотек и сборки, текст попадает в DOM только через textContent.
const words = (list) => `\\b(?:${list.split(" ").join("|")})\\b`;

const COMMENT_HASH = ["comment", "#[^\\n]*"];
const COMMENT_C = ["comment", "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?(?:\\*\\/|(?![\\s\\S]))"];
const NUMBER = ["number", "\\b\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b|\\b0x[\\da-fA-F]+\\b"];
const STRING = ["string", "\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'"];
const LITERAL = ["keyword", words("true false null True False None")];

const LANGUAGES = {
  python: [
    COMMENT_HASH,
    ["string", "[rbfRBF]{0,2}(?:\"\"\"[\\s\\S]*?(?:\"\"\"|(?![\\s\\S]))|'''[\\s\\S]*?(?:'''|(?![\\s\\S])))|[rbfRBF]{0,2}(?:\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*')"],
    ["meta", "@[\\w.]+"],
    ["keyword", words("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield self cls True False None match case")],
    ["func", "\\b[A-Za-z_]\\w*(?=\\()"],
    NUMBER,
  ],
  script: [
    COMMENT_C,
    ["string", "`(?:\\\\[\\s\\S]|[^`\\\\])*`|\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'"],
    ["keyword", words("as async await break case catch class const continue default delete do else enum export extends finally for from function if implements import in instanceof interface let new of private protected public readonly return static super switch this throw try type typeof var void while yield true false null undefined")],
    ["func", "\\b[A-Za-z_$][\\w$]*(?=\\()"],
    NUMBER,
  ],
  json: [STRING, LITERAL, NUMBER],
  css: [
    ["comment", "\\/\\*[\\s\\S]*?(?:\\*\\/|(?![\\s\\S]))"],
    STRING,
    ["keyword", "@[\\w-]+"],
    ["func", "--[\\w-]+|[\\w-]+(?=\\s*:)"],
    ["number", "#[\\da-fA-F]{3,8}\\b|\\b\\d+(?:\\.\\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg)?\\b"],
  ],
  html: [
    ["comment", "<!--[\\s\\S]*?(?:-->|(?![\\s\\S]))"],
    ["keyword", "<\\/?[A-Za-z][\\w:-]*|\\/?>"],
    STRING,
    ["func", "\\b[\\w:-]+(?==)"],
  ],
  shell: [COMMENT_HASH, STRING, ["keyword", words("if then else elif fi for while do done case esac function in return export local echo cd")], ["func", "\\$\\{?\\w+\\}?"], NUMBER],
  config: [COMMENT_HASH, STRING, ["func", "^[ \\t]*[\\w.-]+(?=\\s*[:=])|^\\[[^\\]\\n]+\\]"], LITERAL, NUMBER],
};

const BY_EXTENSION = {
  py: "python", js: "script", mjs: "script", cjs: "script", ts: "script", tsx: "script", jsx: "script",
  java: "script", go: "script", rs: "script", c: "script", h: "script", cpp: "script", cs: "script",
  json: "json", css: "css", html: "html", htm: "html", xml: "html", svg: "html",
  sh: "shell", bash: "shell", ps1: "shell", toml: "config", yaml: "config", yml: "config", ini: "config", env: "config",
};

const compiled = new Map();

function tokenizer(language) {
  if (!compiled.has(language)) {
    const rules = LANGUAGES[language];
    compiled.set(language, {
      classes: rules.map(([name]) => name),
      pattern: new RegExp(rules.map(([, source]) => `(${source})`).join("|"), "gm"),
    });
  }
  return compiled.get(language);
}

export function languageOf(path) {
  const name = path.split("/").pop().toLowerCase();
  const extension = name.includes(".") ? name.split(".").pop() : "";
  return BY_EXTENSION[extension] ?? (name === ".env" || name.startsWith(".env.") ? "config" : null);
}

// Заполняет <pre> текстом файла, оборачивая распознанные части в <span class="tok-…">.
export function highlightInto(pre, text, path) {
  const language = languageOf(path);
  if (!language) {
    pre.textContent = text;
    return;
  }
  const { classes, pattern } = tokenizer(language);
  const fragment = document.createDocumentFragment();
  let last = 0;
  pattern.lastIndex = 0;
  for (let match = pattern.exec(text); match; match = pattern.exec(text)) {
    if (match[0] === "") {
      pattern.lastIndex += 1;
      continue;
    }
    if (match.index > last) fragment.append(text.slice(last, match.index));
    const span = document.createElement("span");
    span.className = `tok-${classes[match.slice(1).findIndex((group) => group !== undefined)]}`;
    span.textContent = match[0];
    fragment.append(span);
    last = match.index + match[0].length;
  }
  fragment.append(text.slice(last));
  pre.replaceChildren(fragment);
}
