// Страницы вне чата: вики, возможности агента, долговременная память.
// Списки — деревья в стиле терминала (tree.js), возможности разложены по вкладкам.
import { openAgentForm, openMainForm } from "./agents.js";
import { api } from "./api.js";
import { el, renderMarkdown } from "./chat.js";
import { createGraph } from "./graph.js";
import { guide, rowAction, stem, treeRow } from "./tree.js";
import { recall, remember } from "./util.js";

const $ = (selector) => document.querySelector(selector);
const ORIGIN = { connector: "Коннекторы — доступ к рабочим системам", builtin: "Встроенные", agent: "Созданы агентом", extra: "Дополнения" };
const KIND = { tool: "инструмент", skill: "скилл", agent: "агент" };
const ARCHIVE_GROUP = { tool: "Инструменты", skill: "Скиллы", agent: "Агенты" };

// Строки детей одной группы: ветки «├─», у последней — «└─».
function branch(items, lines, build) {
  return items.flatMap((item, index) => build(item, lines, index === items.length - 1));
}

// ---------- вики ----------

const INDEX_PAGE = "index.md";
// Сколько заметок в «Недавних» и сколько тегов видно над списком, пока не нажали «ещё».
const RECENT_SHOWN = 5;
const TAGS_SHOWN = 10;
const UNTAGGED = "без тегов";
// Открытая заметка запоминается: после перезагрузки страницы она открывается снова.
let wikiCurrent = recall("wiki-page");
const wikiClosed = new Set();
// Все заметки с описаниями и тегами: по ним список фильтруется сразу, без запроса к серверу.
let wikiPages = [];
// Выбранный тег сужает список до его заметок; null — все заметки.
let wikiTag = null;
let wikiAllTags = false;
// Последний показанный список: после открытия заметки он перерисовывается без нового запроса
// и без перестановки «Недавних» под курсором.
let wikiShown = { query: "", textHits: [], recent: [] };
// «Список» — заметки по темам, «Граф» — точки и ссылки между ними; читалка справа в обоих видах.
let wikiMode = recall("wiki-mode") === "graph" ? "graph" : "list";
let wikiGraph = null;

export async function showWiki(toast) {
  try {
    const query = $("#wiki-search").value.trim();
    const graphShown = wikiMode === "graph" && !query && !wikiTag;
    renderWikiMode(toast, graphShown);
    wikiPages = await api.wiki();
    renderWikiTags(toast);
    if (graphShown) {
      wikiGraph ??= createGraph($("#wiki-graph"), { onOpen: (path) => openWiki(path, toast) });
      wikiGraph.update(await api.wikiGraph(), wikiCurrent);
    } else {
      const textHits = query ? await api.wikiSearch(query) : [];
      renderWikiList({ query, textHits, recent: recentPages() }, toast);
    }
    if (wikiCurrent && !query) await openWiki(wikiCurrent, toast, false);
  } catch (error) {
    toast(error);
  }
}

// Стрелки ходят по списку, Enter в поиске открывает первую найденную заметку, Esc очищает поиск.
export function wikiKeys(event, toast) {
  const search = $("#wiki-search");
  const rows = [...$("#wiki-list").querySelectorAll("button.trow-main")];
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const next = rows.indexOf(document.activeElement) + (event.key === "ArrowDown" ? 1 : -1);
    (next < 0 ? search : rows[Math.min(next, rows.length - 1)])?.focus();
  } else if (event.key === "Enter" && event.target === search) {
    event.preventDefault();
    rows.find((row) => !row.closest(".group"))?.click();
  } else if (event.key === "Escape" && (search.value || wikiTag)) {
    // Esc здесь только очищает поиск: ответ в основном чате он не останавливает.
    event.stopPropagation();
    search.value = "";
    wikiTag = null;
    search.focus();
    void showWiki(toast);
  }
}

// Поиск и выбранный тег всегда показывают список: граф по ним не фильтруется.
function renderWikiMode(toast, graphShown) {
  $("#wiki-split").classList.toggle("graph-mode", graphShown);
  $("#wiki-list").hidden = graphShown;
  $("#wiki-graph").hidden = !graphShown;
  for (const tab of $("#wiki-tabs").querySelectorAll("button")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.mode === wikiMode));
    tab.onclick = () => {
      wikiMode = tab.dataset.mode;
      remember("wiki-mode", wikiMode);
      $("#wiki-search").value = "";
      wikiTag = null;
      void showWiki(toast);
    };
  }
}

// Теги над списком — самые частые первыми, с числом заметок.
function renderWikiTags(toast) {
  const tags = tagCounts();
  const shown = wikiAllTags ? tags : tags.slice(0, TAGS_SHOWN);
  if (wikiTag && !shown.some(([tag]) => tag === wikiTag)) shown.push([wikiTag, 0]);
  const chips = shown.map(([tag, count]) => tagChip(tag, toast, count));
  if (tags.length > TAGS_SHOWN) {
    const more = el("button", "chip", wikiAllTags ? "свернуть" : `ещё ${tags.length - TAGS_SHOWN}`);
    more.type = "button";
    more.addEventListener("click", () => {
      wikiAllTags = !wikiAllTags;
      renderWikiTags(toast);
    });
    chips.push(more);
  }
  $("#wiki-tags").replaceChildren(...chips);
}

function tagCounts() {
  const counts = new Map();
  for (const page of wikiPages) for (const tag of page.tags) counts.set(tag, (counts.get(tag) ?? 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ru"));
}

// Тег над списком и под заметкой: щелчок оставляет в списке только его заметки, повторный — снимает фильтр.
function tagChip(tag, toast, count = 0) {
  const chip = el("button", `chip${tag === wikiTag ? " on" : ""}`, `#${tag}`);
  chip.type = "button";
  chip.title = tag === wikiTag ? "Показать все заметки" : `Только заметки с тегом «${tag}»`;
  if (count) chip.append(el("span", "chip-count", ` ${count}`));
  chip.addEventListener("click", () => {
    wikiTag = tag === wikiTag ? null : tag;
    void showWiki(toast);
  });
  return chip;
}

// Недавние: заметки по последнему касанию — открыли здесь или изменили (вы или агент), что позже.
// Только что созданная заметка поэтому сразу наверху, а не за открытыми раньше.
function recentPages() {
  const opened = openedAt();
  const touched = (page) => Math.max(opened[page.path] ?? 0, Date.parse(page.updated_at) || 0);
  return [...wikiPages].sort((a, b) => touched(b) - touched(a)).slice(0, RECENT_SHOWN);
}

// Когда заметки открывали здесь: { путь: время }. Старая запись — просто список путей, без времени.
function openedAt() {
  try {
    const stored = JSON.parse(recall("wiki-opened"));
    if (stored && typeof stored === "object" && !Array.isArray(stored)) return stored;
  } catch {
    // Испорченная запись — просто без открытых.
  }
  return {};
}

// Без поиска — «Недавние» и заметки по темам (главный тег — самый частый в вики из тегов заметки);
// с поиском — совпадения в названии, описании и тегах (сразу, без сервера), ниже — найденное в тексте.
function renderWikiList(shown, toast) {
  wikiShown = shown;
  const { query, textHits, recent } = shown;
  const root = $("#wiki-list");
  if (!wikiPages.length) {
    root.replaceChildren(el("p", "hint", "Заметок пока нет."));
    return;
  }
  const pages = wikiTag ? wikiPages.filter((page) => page.tags.includes(wikiTag)) : wikiPages;
  // Длинное название в узкой колонке обрезано: целиком — с описанием — во всплывающей подсказке.
  const item = (page, detail = "") => ({ path: page.path, title: page.title, hint: [page.title, page.description].join("\n"), detail });
  let sections;
  if (query) {
    const words = query.toLowerCase().split(/\s+/);
    const named = pages.filter((page) => {
      const text = [page.title, page.description, ...page.tags].join(" ").toLowerCase();
      return words.every((word) => text.includes(word));
    });
    const skip = new Set(named.map((page) => page.path));
    const allowed = new Set(pages.map((page) => page.path));
    const inText = [...new Map(textHits.map((hit) => [hit.path, hit])).values()]
      .filter((hit) => allowed.has(hit.path) && !skip.has(hit.path));
    sections = [
      ["В названии и тегах", named.map((page) => item(page, page.description))],
      ["В тексте", inText.map((hit) => ({ path: hit.path, title: hit.title, hint: hit.path, detail: hit.text }))],
    ];
  } else if (wikiTag) {
    sections = [[`#${wikiTag}`, byTitle(pages).map((page) => item(page))]];
  } else {
    const counts = new Map(tagCounts());
    const topics = new Map();
    for (const page of pages) {
      const topic = page.tags.reduce((best, tag) => (counts.get(tag) > (counts.get(best) ?? 0) ? tag : best), null);
      const name = topic ? `#${topic}` : UNTAGGED;
      topics.set(name, [...(topics.get(name) ?? []), page]);
    }
    const ordered = [...topics].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0], "ru"));
    sections = [
      ["Недавние", recent.map((page) => item(page))],
      ...ordered.map(([name, group]) => [name, byTitle(group).map((page) => item(page))]),
    ];
  }
  const rows = sections.filter(([, items]) => items.length).flatMap(([name, items]) => {
    // Найденное не сворачивается: свёрнутая группа спрятала бы результат поиска.
    const opened = Boolean(query) || !wikiClosed.has(name);
    const head = treeRow({
      label: `${name} · ${items.length}`, group: true, open: opened,
      onClick: query ? undefined : () => {
        if (!wikiClosed.delete(name)) wikiClosed.add(name);
        renderWikiList(wikiShown, toast);
      },
    });
    return opened ? [head, ...branch(items, [], (entry, lines, last) => treeRow({
      prefix: guide(lines, last), label: entry.title, title: entry.hint, current: entry.path === wikiCurrent,
      onClick: () => openWiki(entry.path, toast), detail: entry.detail, detailPrefix: stem(lines, last),
    }))] : [head];
  });
  if (!query && !wikiTag) {
    rows.push(treeRow({
      label: INDEX_PAGE, note: "индекс", current: wikiCurrent === INDEX_PAGE, onClick: () => openWiki(INDEX_PAGE, toast),
      title: "Индекс: строка на каждую заметку, агент обновляет его сам",
    }));
  }
  root.replaceChildren(...(rows.length ? rows : [el("p", "hint", "Ничего не нашлось.")]));
}

function byTitle(pages) {
  return [...pages].sort((a, b) => a.title.localeCompare(b.title, "ru"));
}

// Заметка по пути — из чата вики: пути в ответах агента открывают её здесь.
export function openNote(path, toast) {
  void openWiki(path, toast);
}

async function openWiki(path, toast, refreshList = true) {
  try {
    const page = await api.wikiPage(path);
    wikiCurrent = path;
    remember("wiki-page", path);
    if (path !== INDEX_PAGE) {
      const opened = Object.entries({ ...openedAt(), [path]: Date.now() }).sort((a, b) => b[1] - a[1]);
      remember("wiki-opened", JSON.stringify(Object.fromEntries(opened.slice(0, RECENT_SHOWN))));
    }
    const reader = $("#wiki-page");
    reader.replaceChildren(el("p", "hint", `data/wiki/${path}`));
    // Теги — как в списке: варианты одного слова там уже сведены к одному тегу.
    const tags = wikiPages.find((item) => item.path === path)?.tags ?? page.meta?.tags.map((tag) => tag.toLowerCase().trim());
    if (page.meta) reader.append(noteMeta(page.meta, tags, toast));
    // Картинки заметки лежат в вики рядом с ней: адрес — от папки заметки.
    renderMarkdown(reader, page.body, true, (src) => {
      const file = wikiPath(path, src);
      return file ? `/api/wiki/file?path=${encodeURIComponent(file)}` : null;
    });
    // Ссылки на другие заметки открываются здесь же, а не переходом браузера.
    for (const link of reader.querySelectorAll("a[data-href]")) {
      const target = wikiTarget(path, link.dataset.href);
      if (!target) continue;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        void openWiki(target, toast);
      });
    }
    wikiGraph?.setCurrent(path);
    if (refreshList && !$("#wiki-list").hidden) renderWikiList(wikiShown, toast);
  } catch (error) {
    if (refreshList) {
      toast(error);
      return;
    }
    // Запомненной заметки больше нет (удалили или слили): забываем её без ошибки.
    wikiCurrent = null;
    remember("wiki-page", "");
    $("#wiki-page").replaceChildren(el("p", "hint", "Выберите страницу."));
  }
}

// Поля OKF над текстом: тип, теги (щелчок — заметки с этим тегом) и описание.
function noteMeta(meta, tags, toast) {
  const box = el("div", "note-meta");
  const chips = el("div", "note-chips");
  if (meta.type) chips.append(el("span", "chip type", meta.type));
  for (const tag of tags) chips.append(tagChip(tag, toast));
  if (chips.childElementCount) box.append(chips);
  if (meta.description) box.append(el("p", "note-description", meta.description));
  return box;
}

// Путь заметки по относительной ссылке из заметки from; внешние ссылки и не .md — null.
function wikiTarget(from, href) {
  const path = wikiPath(from, href);
  return path?.endsWith(".md") ? path : null;
}

// Путь файла вики по относительной ссылке из заметки from; внешние ссылки — null.
function wikiPath(from, href) {
  if (!href || /^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("/") || href.startsWith("#")) return null;
  const clean = decodeURIComponent(href.split("#")[0]);
  const parts = from.split("/").slice(0, -1);
  for (const part of clean.split("/")) {
    if (part === "..") parts.pop();
    else if (part && part !== ".") parts.push(part);
  }
  return parts.join("/");
}

// ---------- возможности ----------

const CAP_TABS = [["tools", "Инструменты"], ["skills", "Скиллы"], ["agents", "Агенты"], ["mcp", "MCP"], ["python", "Пакеты"], ["library", "Готовые"], ["archive", "Архив"]];
let capTab = recall("capabilities-tab") ?? "tools";
let capData = null;

export async function showCapabilities(toast) {
  try {
    capData = await api.capabilities();
    renderCapabilities(toast);
  } catch (error) {
    toast(error);
  }
}

function renderCapabilities(toast) {
  const data = capData;
  const reload = () => showCapabilities(toast);
  const counts = {
    tools: data.tools.filter((tool) => tool.origin !== "mcp").length, skills: data.skills.length,
    agents: data.agents.length + 1, mcp: data.mcp.length, python: data.python.packages.length,
    library: data.library.length, archive: data.archive.length,
  };
  $("#cap-tabs").replaceChildren(...CAP_TABS.map(([id, label]) => {
    const tab = el("button", "", label);
    tab.type = "button";
    tab.setAttribute("aria-pressed", String(id === capTab));
    tab.append(el("span", "count", String(counts[id])));
    tab.addEventListener("click", () => {
      capTab = id;
      remember("capabilities-tab", id);
      renderCapabilities(toast);
    });
    return tab;
  }));
  const views = {
    tools: () => groups(Object.entries(ORIGIN).map(([origin, title]) => [title, data.tools.filter((tool) => tool.origin === origin)]),
      (tool, lines, last) => capRow(tool.id, tool.description, [
        tool.origin === "connector" ? (tool.configured ? "настроен" : "нужно настроить")
          : tool.requires_approval ? "с подтверждением" : "без подтверждения",
      ], [
        tool.origin === "connector" && link("настроить", () => openConnector(tool, reload, toast)),
        ["agent", "connector"].includes(tool.origin) && link("код", () => showSource("tool", tool.id, reload, toast)),
        ["agent", "connector", "extra"].includes(tool.origin) && link("в архив", () => archive("tool", tool.id, reload, toast)),
      ], lines, last)),
    skills: () => groups([["data/skills", data.skills]], (skill, lines, last) => capRow(skill.id, skill.description, [], [
      link("текст", () => showSource("skill", skill.id, reload, toast)),
      link("в архив", () => archive("skill", skill.id, reload, toast)),
    ], lines, last), "Скиллов пока нет — агент предложит их, когда задачи начнут повторяться."),
    // Основной агент — первым и отдельно: его промпт встроен, смотреть можно, править — только свои правила.
    agents: () => [
      ...groups([["Основной агент · встроен", [{ id: "main", name: "Основной агент" }]]], (agent, lines, last) => {
        const row = capRow(agent.name, "Общается с вами, распределяет задачи между агентами, помнит вас и наращивает возможности",
          ["руководитель"], [
            link("промпт", () => showSource("agent", "main", reload, toast)),
            link("правила", () => editAgent("main", reload, toast)),
          ], lines, last);
        row.classList.add("main-agent");
        return row;
      }),
      ...groups([["data/agents", data.agents]], (agent, lines, last) => capRow(agent.name, agent.description, [
      agent.tools.length ? agent.tools.join(", ") : "без инструментов",
    ], [
      link("править", () => editAgent(agent.id, reload, toast)),
      link("промпт", () => showSource("agent", agent.id, reload, toast)),
      link("в архив", () => archive("agent", agent.id, reload, toast)),
    ], lines, last), "Других агентов пока нет — основной предложит их для повторяющихся направлений."),
    ],
    mcp: () => mcpRows(data),
    python: () => groups([[data.python.path ?? "песочница", data.python.packages]], (item, lines, last) => capRow(
      `${item.name} ${item.version}`, "", [item.tools.length ? `нужен: ${item.tools.join(", ")}` : "не используется"], [
        link("удалить", () => uninstall(item, reload, toast)),
      ], lines, last),
    "Пакетов нет: скрипты агента видят только стандартную библиотеку Python. Агент попросит установить нужное — с вашего согласия."),
    // Готовые заготовки из кода: скиллы, коннекторы, дополнения. «Включить» — и они в работе, дальше их можно править.
    library: () => groups(Object.entries(ARCHIVE_GROUP).map(([kind, title]) => [title, data.library.filter((item) => item.kind === kind)]),
      (item, lines, last) => capRow(item.id, item.description, [], [
        link("включить", async () => {
          try {
            await api.enable(item.kind, item.id);
            await reload();
          } catch (error) {
            toast(error);
          }
        }),
      ], lines, last), "Все готовые заготовки уже включены."),
    archive: () => groups(Object.entries(ARCHIVE_GROUP).map(([kind, title]) => [title, data.archive.filter((item) => item.kind === kind)]),
      (item, lines, last) => capRow(item.id, item.name === item.id ? "" : item.name, [], [
        link("вернуть", async () => {
          try {
            await api.restore(item.kind, item.archive_id);
            await reload();
          } catch (error) {
            toast(error);
          }
        }),
        link("удалить", async () => {
          if (!window.confirm(`Удалить ${KIND[item.kind]} ${item.id} из архива навсегда? Вернуть его будет нельзя.`)) return;
          try {
            await api.purge(item.kind, item.archive_id);
            await reload();
          } catch (error) {
            toast(error);
          }
        }),
      ], lines, last), "Архив пуст."),
  };
  $("#capabilities").replaceChildren(...(views[capTab] ?? views.tools)());
}

// Непустые группы: заголовок «◆ группа», под ним ветки.
function groups(list, build, empty = "Пусто.") {
  const filled = list.filter(([, items]) => items.length);
  if (!filled.length) return [el("p", "hint", empty)];
  return filled.flatMap(([title, items]) => [
    treeRow({ label: title, group: true, note: String(items.length) }),
    ...branch(items, [], build),
  ]);
}

function mcpRows(data) {
  if (!data.mcp.length) return [el("p", "hint", "MCP выключен (MCP_ENABLED=false) или серверов нет в data/mcp.json.")];
  const described = new Map(data.tools.map((tool) => [tool.id, tool.description]));
  return data.mcp.flatMap((server) => {
    const head = treeRow({ label: server.name, group: true, note: server.status, title: server.description });
    head.classList.toggle("failed", server.status !== "работает");
    const tools = server.tools ?? [];
    return [
      head,
      ...(tools.length ? branch(tools, [], (id, lines, last) => capRow(id, described.get(id) ?? "", [], [], lines, last))
        : [treeRow({ prefix: guide([], true), label: server.description })]),
    ];
  });
}

function capRow(name, description, badges, actions, lines, last) {
  return treeRow({
    prefix: guide(lines, last), label: name, note: badges.filter(Boolean).join(" · "), title: description,
    extra: actions.filter(Boolean), detail: description, detailPrefix: stem(lines, last),
  });
}

// Кнопка действия, видная всегда; «в архив» при наведении красная — убирает возможность.
function link(text, handler) {
  const button = rowAction(text, text, handler);
  button.classList.add("shown");
  button.classList.toggle("danger", ["в архив", "удалить"].includes(text));
  return button;
}

export async function reloadCapabilities(toast) {
  try {
    await api.reloadCapabilities();
    await showCapabilities(toast);
  } catch (error) {
    toast(error);
  }
}

async function archive(kind, id, reload, toast) {
  if (!window.confirm(`Убрать ${KIND[kind]} ${id} в архив? Файлы останутся, вернуть можно во вкладке «Архив».`)) return;
  try {
    await api.archive(kind, id);
    await reload();
  } catch (error) {
    toast(error);
  }
}

// ---------- настройка коннектора ----------

// Поле формы по имени доступа: JIRA_URL → «Адрес», JIRA_TOKEN → «Токен» и т. д. Имя видно под полем мелко:
// по нему скрипт читает значение (os.environ).
function fieldMeta(name) {
  if (name.endsWith("_URL")) return { label: "Адрес", type: "url", placeholder: "https://jira.company.ru" };
  if (/_(EMAIL|USER|LOGIN)$/.test(name)) return { label: "Логин или почта", type: "text", placeholder: "me@company.ru" };
  if (name.endsWith("_PASSWORD")) return { label: "Пароль", type: "password" };
  if (/_(TOKEN|KEY)$/.test(name)) return { label: "Токен", type: "password" };
  if (name.endsWith("_VERIFY_SSL")) return { label: "Проверять сертификат HTTPS", type: "checkbox" };
  return { label: name, type: "password" };
}

// Подсказки для известных коннекторов: где взять значение.
const FIELD_HINTS = {
  JIRA_URL: "Адрес, по которому вы открываете Jira в браузере, без /browse/…",
  JIRA_TOKEN: "Jira Server / Data Center: аватар → Профиль → Personal Access Tokens → Создать. "
    + "Jira Cloud (*.atlassian.net): id.atlassian.com → Security → API tokens.",
  JIRA_EMAIL: "Только для Jira Cloud — почта вашей учётной записи. Для корпоративной Jira оставьте пустым.",
  JIRA_VERIFY_SSL: "Снимите, если Jira на корпоративном (самоподписанном) сертификате и проверка пишет про CERTIFICATE.",
};

let connectorDialog = null;

function openConnector(tool, reload, toast) {
  connectorDialog?.remove();
  const dialog = connectorDialog = el("dialog", "connector-dialog");
  const form = el("form", "connector");
  form.method = "dialog";
  const about = el("div", "connector-about");
  about.append(
    el("p", "", tool.full_description ?? tool.description),
    el("p", "hint", "Заполните доступы — и агент сам будет вызывать этот инструмент, когда вы попросите "
      + "(например: «покажи мои задачи в Jira»). Значения хранятся только у вас (data/secrets.json); "
      + "агент и чат их не видят. Токен и пароль после сохранения не показываются — только «задан»."),
  );
  form.append(el("h3", "", `Настройка: ${tool.name}`), about);
  // Порядок как в обычной форме входа: адрес, логин, токен или пароль, галочки — в конце.
  const order = { url: 0, text: 1, password: 2, checkbox: 3 };
  const fields = [...tool.fields].sort((a, b) => order[fieldMeta(a.name).type] - order[fieldMeta(b.name).type]);
  const inputs = fields.map((field) => {
    const meta = fieldMeta(field.name);
    const label = el("label", meta.type === "checkbox" ? "check" : "");
    const input = el("input");
    input.type = meta.type;
    input.name = field.name;
    if (meta.type === "checkbox") {
      input.checked = (field.value ?? "true").toLowerCase() !== "false";
      label.append(input, el("span", "", meta.label));
    } else {
      input.autocomplete = meta.type === "password" ? "new-password" : "off";
      input.spellcheck = false;
      input.value = meta.type === "password" ? "" : field.value ?? "";
      input.placeholder = meta.type === "password" && field.set ? "задан — оставьте пустым, чтобы не менять" : meta.placeholder ?? "";
      const title = el("span", "", `${meta.label}${field.optional ? " (необязательно)" : ""}`);
      if (field.set && meta.type === "password") title.append(el("span", "connector-set", " · задан"));
      label.append(title, input);
    }
    const hint = FIELD_HINTS[field.name];
    label.append(el("span", "connector-name", `${hint ? `${hint} · ` : ""}${field.name}`));
    form.append(label);
    return { field, meta, input };
  });
  const status = el("span", "connector-status");
  status.setAttribute("role", "status");
  const actions = el("div", "dialog-actions");
  const check = el("button", "", "Проверить подключение");
  check.type = "button";
  check.hidden = !tool.checkable;
  const close = el("button", "", "Закрыть");
  close.type = "button";
  const save = el("button", "primary", "Сохранить");
  save.type = "submit";
  actions.append(status, check, close, save);
  form.append(actions);
  dialog.append(form);
  document.body.append(dialog);

  const show = (text, ok) => {
    status.textContent = text;
    status.classList.toggle("ok", ok === true);
    status.classList.toggle("failed", ok === false);
  };
  // Запоминаем, что уже записано: повторное «Проверить» не пишет то же самое и не теряет снятую галочку.
  const put = async (field, value) => {
    await api.setSecret(field.name, value);
    Object.assign(field, { set: true, value });
  };
  const drop = async (field) => {
    await api.deleteSecret(field.name);
    Object.assign(field, { set: false, value: undefined });
  };
  const store = async () => {
    for (const { field, meta, input } of inputs) {
      if (meta.type === "checkbox") {
        // Проверка включена по умолчанию: храним только «выключено».
        if (!input.checked && field.value !== "false") await put(field, "false");
        else if (input.checked && field.set) await drop(field);
        continue;
      }
      const value = input.value.trim();
      if (meta.type === "password") {
        if (value) {
          await put(field, value);
          input.value = "";
          input.placeholder = "задан — оставьте пустым, чтобы не менять";
        }
      } else if (value !== (field.value ?? "")) {
        if (value) await put(field, value);
        else if (field.set) await drop(field);
      }
    }
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await store();
      dialog.close();
      await reload();
    } catch (error) {
      show(error.message, false);
    }
  });
  check.addEventListener("click", async () => {
    show("Сохраняю и проверяю…");
    try {
      await store();
      show((await api.checkConnector(tool.id)).message, true);
      void reload();
    } catch (error) {
      show(error.message, false);
    }
  });
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  dialog.showModal();
}

async function uninstall(item, reload, toast) {
  const users = item.tools.length ? ` Он нужен инструментам: ${item.tools.join(", ")} — они перестанут работать.` : "";
  if (!window.confirm(`Удалить пакет ${item.name} из песочницы?${users}`)) return;
  try {
    await api.uninstallPackage(item.name);
    await reload();
  } catch (error) {
    toast(error);
  }
}

// Форме нужен агент целиком (промпт и скиллы), а в обзоре возможностей — только строка списка.
async function editAgent(id, reload, toast) {
  try {
    const agent = (await api.agents()).find((item) => item.id === id);
    if (agent?.builtin) openMainForm(agent, reload);
    else if (agent) await openAgentForm(agent, reload);
  } catch (error) {
    toast(error);
  }
}

// Поле — во весь текст вместе с рамкой: иначе оно на пару пикселей ниже текста и прокручивается внутри себя.
function fitEditor(editor) {
  // Сброс высоты на миг укорачивает список файлов — прокрутку списка возвращаем, чтобы он не прыгал при наборе.
  const list = $("#source-files");
  const scrolled = list.scrollTop;
  editor.style.height = "auto";
  editor.style.height = `${editor.scrollHeight + editor.offsetHeight - editor.clientHeight}px`;
  editor.scrollTop = 0;
  list.scrollTop = scrolled;
}

// Файлы возможности — прямо в окне: строки переносятся, окно не выходит за экран, «Сохранить» проверяет файл
// и записывает его, только если он рабочий.
async function showSource(kind, id, reload, toast) {
  try {
    const source = await api.capabilitySource(kind, id);
    $("#source-title").textContent = `${KIND[kind]} ${id}`;
    $("#source-path").textContent = source.path;
    const editors = Object.entries(source.files).map(([name, text]) => {
      const editor = el("textarea", "source-editor");
      Object.assign(editor, { value: text, spellcheck: false });
      editor.setAttribute("aria-label", name);
      editor.dataset.name = name;
      editor.dataset.original = text;
      editor.readOnly = Boolean(source.readonly);
      editor.addEventListener("input", () => {
        fitEditor(editor);
        sync();
      });
      return editor;
    });
    const save = $("#source-save");
    const sync = () => { save.disabled = !editors.some((editor) => editor.value !== editor.dataset.original); };
    $("#source-files").replaceChildren(...editors.flatMap((editor) => [el("h4", "", editor.dataset.name), editor]));
    $("#source-status").textContent = source.readonly ? "Только просмотр" : "";
    save.hidden = Boolean(source.readonly);
    sync();
    save.onclick = async () => {
      const files = Object.fromEntries(editors.filter((editor) => editor.value !== editor.dataset.original)
        .map((editor) => [editor.dataset.name, editor.value]));
      save.disabled = true;
      try {
        await api.saveSource(kind, id, files);
        for (const editor of editors) editor.dataset.original = editor.value;
        $("#source-status").textContent = "Сохранено";
        await reload();
      } catch (error) {
        $("#source-status").textContent = error.message;
      }
      sync();
    };
    $("#source-dialog").showModal();
    for (const editor of editors) fitEditor(editor);
  } catch (error) {
    toast(error);
  }
}


// ---------- память ----------

let memoryCurrent = null;
let memoryDocuments = [];

export async function showMemory(toast) {
  try {
    memoryDocuments = await api.memory();
    memoryCurrent ??= (memoryDocuments.find((item) => item.name === "profile.md") ?? memoryDocuments[0])?.name ?? null;
    renderMemoryList(toast);
    if (memoryCurrent) await openMemory(memoryCurrent, toast);
  } catch (error) {
    toast(error);
  }
}

function renderMemoryList(toast) {
  $("#memory-list").replaceChildren(treeRow({ label: "data/memory", group: true }), ...branch(memoryDocuments, [], (document, lines, last) => treeRow({
    prefix: guide(lines, last), label: document.title, note: document.name, title: document.name,
    current: document.name === memoryCurrent, onClick: () => openMemory(document.name, toast),
  })));
}

async function openMemory(name, toast) {
  try {
    const document = await api.memoryDocument(name);
    memoryCurrent = name;
    $("#memory-text").value = document.content;
    $("#memory-status").textContent = `data/memory/${name}`;
    renderMemoryList(toast);
  } catch (error) {
    toast(error);
  }
}

export async function saveMemory(toast) {
  if (!memoryCurrent) return;
  try {
    await api.saveMemory(memoryCurrent, $("#memory-text").value);
    $("#memory-status").textContent = "Сохранено";
  } catch (error) {
    toast(error);
  }
}
