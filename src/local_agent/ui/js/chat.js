// Чат: история сессии и печатающийся ответ. Вызовы инструментов — строки в общей рамке,
// под ответом — сводка хода и «Копировать», результаты саб-агентов выделены, код в ответах подсвечен.
import { renderDiagrams } from "./diagrams.js";
import { highlightCode } from "./highlight.js";
import { appendMarkdown } from "./markdown.js";
import { plural } from "./util.js";

const FLUSH_MS = 80;
const STEP_ICON = { done: "✓", failed: "✕", pending: "◌" };
// Длинный отчёт саб-агента в основном чате свёрнут; развёрнутые остаются такими после перерисовки.
const REPORT_FOLD_CHARS = 300;
const REPORT_FOLD_LINES = 5;
const unfolded = new Set();

// greeting — заголовок и строка пустого разговора; по умолчанию здоровается основной агент.
// Разговор идёт ходами: реплика пользователя (или отчёт саб-агента), под ней свёрнутый «ход работы» — весь процесс:
// рассуждения, инструменты, промежуточные реплики модели, — затем строки событий (кому поручено, кто что выбрал),
// готовые результаты этапов и итоговый ответ.
// Пока ход идёт (live — печатающийся ответ этой вкладки, busy — ход идёт на сервере, waiting — ход ждёт решения
// по карточке), в чате только одна строка хода: результатов ещё нет, и наружу ничего не выходит.
export function renderMessages(
  container, messages, greeting = ["Привет! Я Meepo.", "Расскажите, как вас зовут и чем займёмся."],
  { live = null, waiting = false, busy = false } = {},
) {
  container.replaceChildren();
  if (!messages.length) {
    const empty = el("div", "empty");
    const mascot = el("img");
    mascot.src = "/ui/assets/meepo-mascot.png";
    mascot.alt = "";
    empty.append(mascot, el("strong", "", greeting[0]), greeting[1]);
    container.append(empty);
    return;
  }
  const results = new Map(messages.filter((item) => item.role === "tool").map((item) => [item.tool_call_id, item]));
  const all = turns(messages, results);
  const ongoing = { live, active: Boolean(live) || busy || waiting, label: waiting ? "ждёт решения" : "идёт" };
  all.forEach((turn, index) => renderTurn(container, turn, results, index === all.length - 1, ongoing));
}

// Имена агентов для строк «Поручено …»: в вызове инструмента — только id.
let agentNames = new Map();
export function setAgentNames(agents) {
  agentNames = new Map(agents.map((agent) => [agent.id, agent.name]));
}

// Служебные вызовы основного агента — не в рамку инструментов, а строками событий.
const EVENT_TOOLS = new Set(["delegate", "choose_route"]);
const openedWork = new Map();

// Ход начинается репликой пользователя или отчётом саб-агента. Ответ на карточку — часть того же хода:
// ход стоял на вызове инструмента, и ответ сохраняется вместе с результатом вызова. Реплика после оборванного
// хода (у вызова нет результата или он сильно раньше реплики) начинает новый ход.
const REPLY_GAP_MS = 2000;

function turns(messages, results) {
  const list = [];
  let turn = null;
  let calls = [];
  for (const message of messages) {
    if (message.role === "tool") continue;
    const reply = message.role === "user" && !message.sender && calls.some((call) => {
      const result = results.get(call.id);
      return result && Math.abs(Date.parse(result.created_at) - Date.parse(message.created_at)) <= REPLY_GAP_MS;
    });
    if (message.role === "user" && !reply) {
      list.push(turn = { head: message, items: [] });
      calls = [];
      continue;
    }
    if (!turn) list.push(turn = { head: null, items: [] });
    turn.items.push(message);
    if (message.role === "assistant") calls = message.tool_calls ?? [];
  }
  return list;
}

function planDone(message) {
  return message?.tool_calls?.find((call) => call.name === "plan" && parse(call.arguments).action === "done") ?? null;
}

// Реплика модели без вызовов инструментов — кандидат в результат: итог хода или итог этапа.
function isAnswer(item) {
  return item?.role === "assistant" && !item.tool_calls?.length && Boolean(item.content?.trim());
}

function renderTurn(container, turn, results, last, ongoing) {
  if (turn.head) container.append(turn.head.sender ? reportNode(turn.head) : userNode(turn.head));
  // Ход идёт — итогов нет: всё, что модель пишет по дороге, остаётся внутри строки хода.
  const running = last && ongoing.active;
  // Итог хода — последняя реплика модели без вызовов; итоги этапов — реплики, после которых модель
  // отметила пункт плана сделанным.
  const tail = [...turn.items].reverse().find((item) => item.role !== "team");
  const final = !running && isAnswer(tail) ? tail : null;
  const stages = running ? new Map() : stageResults(turn.items, results);
  const work = [];
  const events = [];
  const answered = new Set();
  for (const [index, item] of turn.items.entries()) {
    if (item === final) {
      // Итог — в чат, а рассуждения перед ним — в ход работы, на своё место.
      if (item.reasoning) work.push({ thought: item });
      continue;
    }
    if (item.role === "team") {
      events.push(teamNode(item));
      continue;
    }
    if (item.role === "user") {
      // Ответ на карточку: выбор исполнителя уже показан строкой «Кто сделает», остальные — своей строкой.
      if (!answered.has(item)) events.push(replyLine(item));
      continue;
    }
    if (item.reasoning) work.push({ thought: item });
    // Текст итога этапа — в чат результатом; его вызовы (отметка этапа и что ещё было) остаются шагами хода.
    if (item.content?.trim() && !stages.has(item)) work.push({ note: item });
    const calls = (item.tool_calls ?? []).filter((call) => !EVENT_TOOLS.has(call.name));
    if (calls.length) work.push({ calls, message: item });
    for (const call of item.tool_calls ?? []) {
      if (call.name === "delegate" && results.has(call.id)) events.push(delegateLine(call, results.get(call.id)));
      if (call.name === "choose_route" && results.has(call.id)) {
        const reply = turn.items.slice(index + 1).find((next) => next.role === "user");
        if (reply) answered.add(reply);
        events.push(routeLine(call, reply));
      }
    }
  }
  // Идущий ход этой вкладки — одна строка печатающегося ответа: шаги из истории уходят в неё.
  if (running && ongoing.live) ongoing.live.adopt(work, results);
  else if (work.length) {
    // Ход без итога, который не идёт и не ждёт решения, оборвался: ошибка модели или «Стоп».
    const label = running ? ongoing.label : last && !final ? "прервано" : "";
    container.append(workNode(work, results, label));
  }
  container.append(...events);
  for (const [message, title] of stages) if (message !== final) container.append(assistantNode(message, false, title));
  if (final) container.append(assistantNode(final, last));
  if (!running) {
    const files = filesNode(turn.items, results);
    if (files) container.append(files);
  }
}

// Файлы, которые агент создал или изменил за ход, — в конце хода одной строкой; щелчок открывает файл.
const FILE_TOOLS = new Set(["write_file", "edit_file"]);
function filesNode(items, results) {
  const paths = [...new Set(items.flatMap((item) => (item.tool_calls ?? [])
    .filter((call) => FILE_TOOLS.has(call.name) && results.get(call.id) && !results.get(call.id).is_error)
    .map((call) => String(parse(call.arguments).path ?? "")).filter(Boolean)))];
  if (!paths.length) return null;
  const line = el("div", "turn-files");
  line.append(el("span", "turn-files-label", "Файлы:"));
  for (const path of paths) {
    const chip = el("code", "", path);
    markPath(chip, path);
    line.append(chip);
  }
  return line;
}

// Итоги этапов. Какой текст — итог этапа, решает сервер, когда модель отмечает этап сделанным (plan с action done):
// выбор лежит в meta результата этого вызова. Догадка по тексту ниже — только для ходов до этой записи.
function stageResults(items, results) {
  const byId = new Map(items.map((item) => [item.id, item]));
  const decided = new Map();
  let recorded = false;
  for (const item of items) {
    for (const call of item.tool_calls ?? []) {
      const meta = call.name === "plan" ? results.get(call.id)?.meta : null;
      if (!meta) continue;
      recorded = true;
      const message = meta.result ? byId.get(meta.result) : null;
      if (message && !decided.has(message)) decided.set(message, `Этап ${meta.stage} · ${meta.title}`);
    }
  }
  return recorded ? decided : guessedStages(items, results);
}

// Старые ходы: текст модели, в том же ответе которого этап отмечен сделанным, или реплика без вызовов,
// за которой такая отметка идёт следующим ответом. Название — из плана в результате вызова.
function guessedStages(items, results) {
  const stages = new Map();
  items.forEach((item, index) => {
    if (item.role !== "assistant" || !item.content?.trim()) return;
    const next = item.tool_calls?.length ? null : items.slice(index + 1).find((later) => later.role === "assistant");
    const call = planDone(item) ?? planDone(next);
    if (!call) return;
    const step = Number(parse(call.arguments).step);
    const line = (results.get(call.id)?.content ?? "").split("\n").find((text) => text.startsWith(`${step}. [x] `));
    const name = line ? line.slice(`${step}. [x] `.length).split(" — ")[0] : "";
    stages.set(item, `Этап ${step}${name ? ` · ${name}` : ""}`);
  });
  return stages;
}

// Шаги хода человеческими словами: что сделано. group — чем модель занята, для заголовка строки хода
// («Работал в браузере», «Искал в интернете»); null — служебный шаг (план, подключение), заголовок по нему не строится.
const TOOL_STEPS = {
  list_files: ["Список файлов", "files"], read_file: ["Прочитан файл", "files"], search_files: ["Поиск по файлам", "files"],
  write_file: ["Записан файл", "files"], edit_file: ["Изменён файл", "files"], search_docs: ["Поиск по документации", "docs"],
  generate_test_cases: ["Генерация тест-кейсов", "files"], git: ["История git", "git"], fetch_url: ["Прочитана страница", "web"],
  jira: ["Запрос в Jira", "jira"], wiki_read: ["Прочитана заметка", "wiki"], wiki_search: ["Поиск по вики", "wiki"],
  wiki_match: ["Сверка с вики", "wiki"], wiki_save_note: ["Заметка сохранена", "wiki"], wiki_lint: ["Проверка вики", "wiki"],
  wiki_propose_edits: ["Правки вики", "wiki"], memory_search: ["Поиск в памяти", "memory"], remember: ["Запомнено", "memory"],
  delegate: ["Поручено агенту", "team"], ask_agent: ["Вопрос агенту", "team"], handoff: ["Передано основному", "team"],
  ask_user: ["Вопрос вам", null], request_tools: ["Запрос инструментов", null], choose_route: ["Выбор исполнителя", null],
  plan: ["План задачи", null], activate: ["Подключены инструменты", null], use_skill: ["Скилл", null],
  propose: ["Предложение возможности", null], archive: ["Архив возможностей", null], install_packages: ["Установка пакетов", null],
  learn: ["Урок записан", null],
};
const BROWSER_STEPS = {
  navigate: "Открыта страница", navigate_back: "Назад", click: "Нажатие", type: "Ввод текста", fill_form: "Заполнение формы",
  select_option: "Выбор в списке", snapshot: "Снимок страницы", take_screenshot: "Скриншот", hover: "Наведение",
  press_key: "Нажата клавиша", wait_for: "Ожидание", tabs: "Вкладки", drag: "Перетаскивание", file_upload: "Загрузка файла",
  handle_dialog: "Диалог на странице", console_messages: "Консоль страницы", network_requests: "Сетевые запросы",
};
const SEARCH_STEPS = { search: "Поиск в интернете", fetch_content: "Прочитана страница", expand_link: "Раскрыта ссылка" };
// Чем занят: [сейчас, итог хода].
const ACTIVITY = {
  browser: ["Работаю в браузере", "Работал в браузере"], web: ["Ищу в интернете", "Искал в интернете"],
  files: ["Работаю с файлами", "Работал с файлами"], docs: ["Ищу в документации", "Искал в документации"],
  git: ["Смотрю историю git", "Смотрел историю git"], jira: ["Смотрю Jira", "Смотрел Jira"],
  wiki: ["Работаю с вики", "Работал с вики"], memory: ["Вспоминаю", "Обращался к памяти"],
  team: ["Работаю с агентами", "Работал с агентами"], other: ["Выполняю шаги", "Выполнил шаги"],
};

// [что сделано, группа] по id инструмента; инструменты MCP — «браузер_действие» и «search_действие».
function describe(name) {
  if (TOOL_STEPS[name]) return TOOL_STEPS[name];
  if (name.startsWith("browser_")) return [BROWSER_STEPS[name.replace(/^browser_(browser_)?/, "")] ?? "Действие в браузере", "browser"];
  const search = /^search_(.+)$/.exec(name);
  if (search && SEARCH_STEPS[search[1]]) return [SEARCH_STEPS[search[1]], "web"];
  return [name, "other"];
}

// Заголовок строки хода по вызовам: чем модель была занята больше всего. Без вызовов — «Думал».
function activityOf(names, now) {
  const counts = new Map();
  for (const name of names) {
    const group = describe(name)[1];
    if (group) counts.set(group, (counts.get(group) ?? 0) + 1);
  }
  if (!counts.size) return names.length ? ACTIVITY.other[now ? 0 : 1] : now ? "Думаю" : "Думал";
  const [top] = [...counts].sort((a, b) => b[1] - a[1])[0];
  return ACTIVITY[top][now ? 0 : 1];
}

// Ход работы одной строкой: «◇ Работал в браузере · 18 шагов · 3 мин 5 с ›». Свёрнут, пока его не раскроют;
// раскрытое остаётся раскрытым после перерисовки. state — «идёт», «ждёт решения», «прервано» или пусто.
function workNode(work, results, state) {
  const key = (work[0].thought ?? work[0].note ?? work[0].message).id;
  const node = el("details", "work");
  node.open = openedWork.get(key) ?? false;
  node.addEventListener("toggle", () => openedWork.set(key, node.open));
  const { steps, first, last } = workStats(work, results);
  const span = first != null && last > first ? (last - first) / 1000 : null;
  const names = work.flatMap((part) => (part.calls ?? []).map((call) => call.name));
  const head = el("summary", "work-head");
  head.append(
    el("span", "work-icon", "◇"),
    el("span", "work-title", activityOf(names, false)),
    el("span", "work-meta", meta([steps && `${steps} ${plural(steps, "шаг", "шага", "шагов")}`,
      span != null && span >= 1 && duration(round(span)), state])),
    el("span", "work-caret", "▾"),
  );
  node.append(head, workBody(work, results));
  return node;
}

// Сколько шагов в ходе и когда он начался и закончился (по времени сообщений и результатов).
function workStats(work, results) {
  const steps = work.reduce((sum, part) => sum + (part.calls?.length ?? 0), 0);
  const times = work.flatMap((part) => [
    Date.parse((part.thought ?? part.note ?? part.message).created_at),
    ...(part.calls ?? []).map((call) => results.get(call.id)).filter(Boolean).map((result) => Date.parse(result.created_at)),
  ]).filter(Number.isFinite);
  return { steps, first: times.length ? Math.min(...times) : null, last: times.length ? Math.max(...times) : null };
}

// Тело хода — одна рамка со строками по порядку: размышления, промежуточные реплики, шаги с инструментами.
// Каждая строка — одна фраза; раскрыть — подробности.
function workBody(work, results) {
  const body = el("div", "work-body");
  const steps = el("div", "steps");
  for (const part of work) {
    if (part.thought) steps.append(thoughtNode(part.thought));
    else if (part.note) steps.append(noteNode(part.note));
    else {
      // Шаги одного ответа модели идут друг за другом: время шага — от конца предыдущего до его результата.
      let previous = Date.parse(part.message.created_at);
      for (const call of part.calls) {
        const result = results.get(call.id);
        const finished = result && Date.parse(result.created_at);
        const seconds = finished && previous ? Math.max(finished - previous, 0) / 1000 : null;
        if (finished) previous = finished;
        steps.append(stepNode(call, result, seconds));
      }
    }
  }
  if (steps.childElementCount) body.append(steps);
  return body;
}

// Строка подписи: «2 мин 18 с · идёт» → «· 2 мин 18 с · идёт», чтобы она читалась продолжением заголовка.
function meta(parts) {
  const text = parts.filter(Boolean).join(" · ");
  return text ? `· ${text}` : "";
}

function preview(text, limit = 110) {
  // Разметка в одну строку не нужна: заголовки, выделение, таблицы («|---|») и цитаты убираются.
  const line = text.replace(/[#*_`>|]|-{3,}|:-+:?/g, " ").replace(/\s+/g, " ").trim();
  return line.length > limit ? `${line.slice(0, limit - 1)}…` : line;
}

// Размышление модели одной строкой: «∴ Размышление · начало текста…»; раскрыть — целиком.
function thoughtNode(message) {
  const text = message.reasoning.trim();
  const node = el("details", "step thought");
  remembered(node, `thought:${message.id}`);
  const summary = el("summary");
  summary.append(
    el("span", "step-icon", "∴"), el("span", "step-name", "Размышление"),
    el("span", "step-target", preview(text)), el("span", "step-meta", `${compact(text.length)} симв.`),
  );
  node.append(summary, el("pre", "", text));
  return node;
}

// Промежуточная реплика модели (что делает, что нашла) — строкой; раскрыть — текст целиком.
function noteNode(message) {
  const node = el("details", "step note");
  remembered(node, `note:${message.id}`);
  const summary = el("summary");
  summary.append(el("span", "step-icon", "›"), el("span", "step-name", "Заметка"), el("span", "step-target", preview(message.content)),
    el("span", "step-meta", ""));
  const body = el("div", "text step-text");
  renderMarkdown(body, message.content);
  node.append(summary, body);
  return node;
}

// Раскрытые строки шагов остаются раскрытыми, когда ход перерисовывается на каждом событии.
const openedRows = new Set();
function remembered(node, key) {
  node.open = openedRows.has(key);
  node.addEventListener("toggle", () => {
    if (node.open) openedRows.add(key);
    else openedRows.delete(key);
  });
}

// «→ Поручено Тестировщику: задача» — кому и что ушло; щелчок по строке показывает задачу целиком.
function delegateLine(call, result) {
  const args = parse(call.arguments);
  const name = agentNames.get(args.agent) ?? args.agent ?? "агенту";
  const task = String(args.task ?? "").replace(/\s+/g, " ").trim();
  const line = el("div", `event-line${result.is_error ? " failed" : ""}`);
  line.append(
    el("span", "event-route", result.is_error ? `✕ Не удалось поручить: ${name}` : `→ Поручено: ${name}`),
    el("span", "event-text", result.is_error ? result.content : task),
  );
  line.title = task;
  return line;
}

function replyLine(message) {
  const line = el("div", "event-line");
  line.append(el("span", "event-route", "↳ Ваш ответ"), el("span", "event-text", message.content));
  line.title = message.content;
  return line;
}

// Выбор в карточке «Кто сделает»: что выбрал пользователь и что это запомнено для такого типа задач.
function routeLine(call, reply) {
  const args = parse(call.arguments);
  const text = reply?.content ?? "";
  const who = text.startsWith("Поручить: ") ? text.slice("Поручить: ".length)
    : text === "Сделай сам" ? "Основной сам" : text ? `«${text}»` : "решает Основной";
  const line = el("div", "event-line");
  const remembered = (text.startsWith("Поручить: ") || text === "Сделай сам") && args.kind;
  line.append(
    el("span", "event-route", `◆ Кто сделает: ${who}`),
    el("span", "event-text", remembered ? `ваш выбор, запомнен для «${String(args.kind).trim().toLowerCase()}»` : "ваш ответ"),
  );
  return line;
}

// Раскрыл пользователь строку хода — следующие ходы тоже показываются раскрытыми.
let liveOpen = false;

// Идущий ход — одна строка «◌ Ищу в интернете · этап 2/3: Баг-репорты · 4 шага · 1 мин 17 с ›». Весь процесс внутри
// неё: шаги из истории, размышление и текст текущего запроса, журнал. В чат попадает только итог — когда ход закончится.
export class StreamingMessage {
  constructor(container) {
    this.container = container;
    this.node = el("article", "msg assistant streaming");
    this.live = el("details", "work live");
    this.live.open = liveOpen;
    this.live.addEventListener("toggle", () => { liveOpen = this.live.open; });
    this.status = el("summary", "work-head live-head");
    // Части строки создаются один раз: пересоздание каждую секунду перезапускало бы вращение колеса.
    this.statusTitle = el("span", "work-title");
    this.statusMeta = el("span", "work-meta");
    this.status.append(el("span", "spinner"), this.statusTitle, this.statusMeta, el("span", "work-caret", "▾"));
    this.stage = "";
    // Чем модель занята сейчас: thinking, reasoning, writing, retry или tool (с именами вызванных инструментов).
    this.activity = "thinking";
    this.tools = [];
    this.history = el("div", "live-history");
    this.reasoningText = el("pre", "work-thought live-reasoning");
    this.reasoningText.hidden = true;
    this.textNode = el("div", "work-note text live-text");
    this.textNode.hidden = true;
    this.log = el("details", "live-log");
    this.logText = el("pre");
    this.log.append(el("summary", "", "журнал"), this.logText);
    const body = el("div", "work-body");
    body.append(this.history, this.reasoningText, this.textNode, this.log);
    this.live.append(this.status, body);
    this.node.append(this.live);
    this.text = "";
    this.timer = null;
    this.steps = 0;
    // С какого момента идёт ход: время на строке, чтобы долгая работа не выглядела зависшей.
    this.started = Date.now();
    this.ticker = setInterval(() => this.#showStatus(), 1000);
    this.#showStatus();
    container.append(this.node);
    scrollDown(container);
  }

  // Шаги хода из истории: перерисовка чата отдаёт их сюда, а не отдельным блоком «ход работы».
  adopt(work, results) {
    this.history.replaceChildren(...(work.length ? [...workBody(work, results).childNodes] : []));
    const { steps, first } = workStats(work, results);
    this.steps = steps;
    if (first != null && first < this.started) this.started = first;
    this.#showStatus();
  }

  append(text) {
    this.text += text;
    if (this.activity !== "writing") this.setStatus("writing");
    this.timer ??= setTimeout(() => this.#flush(), FLUSH_MS);
  }

  addReasoning(text) {
    if (this.activity !== "reasoning") this.setStatus("reasoning");
    this.reasoningText.hidden = false;
    this.reasoningText.textContent += text;
    if (this.live.open) this.reasoningText.scrollTop = this.reasoningText.scrollHeight;
  }

  addLog(text) {
    this.logText.textContent += `${text}\n`;
    if (this.log.open) this.logText.scrollTop = this.logText.scrollHeight;
  }

  // Модель позвала инструмент или запрос повторяется: напечатанное и рассуждения этого запроса уже в истории
  // (или выброшены при повторе) — здесь начинаем следующий запрос с чистого места.
  reset(activity, tools = []) {
    clearTimeout(this.timer);
    this.timer = null;
    this.text = "";
    this.textNode.replaceChildren();
    this.textNode.hidden = true;
    this.reasoningText.textContent = "";
    this.reasoningText.hidden = true;
    this.setStatus(activity, tools);
  }

  setStatus(activity, tools = []) {
    this.activity = activity;
    this.tools = tools;
    this.#showStatus();
  }

  attach() {
    if (!this.node.isConnected) this.container.append(this.node);
  }

  finish() {
    clearTimeout(this.timer);
    clearInterval(this.ticker);
    this.node.remove();
  }

  // Текущий этап плана задачи («этап 2/3: Баг-репорты»), если агент ведёт план; пусто — без этапа.
  setStage(text) {
    if (text === this.stage) return;
    this.stage = text;
    this.#showStatus();
  }

  #showStatus() {
    const seconds = Math.floor((Date.now() - this.started) / 1000);
    this.statusTitle.textContent = this.activity === "tool" ? activityOf(this.tools, true)
      : { writing: "Пишу ответ", retry: "Повторяю запрос" }[this.activity] ?? "Думаю";
    this.statusMeta.textContent = meta([
      this.stage, this.steps && `${this.steps} ${plural(this.steps, "шаг", "шага", "шагов")}`, seconds && duration(seconds),
    ]);
  }

  #flush() {
    this.timer = null;
    this.textNode.hidden = !this.text.trim();
    this.textNode.replaceChildren();
    renderMarkdown(this.textNode, this.text, false);
    if (this.live.open) {
      const near = this.container.scrollHeight - this.container.scrollTop - this.container.clientHeight < 80;
      if (near) scrollDown(this.container);
    }
  }
}

// images — адрес картинки по ссылке из текста; по умолчанию показываются только картинки самого приложения.
export function renderMarkdown(target, text, withTools = true, images = (src) => (src.startsWith("/api/") ? src : null)) {
  appendMarkdown(target, text);
  for (const image of target.querySelectorAll("img[data-src]")) {
    const url = images(image.dataset.src);
    if (!url) {
      // Чужие адреса не грузим: картинка из интернета выдала бы, что страницу открыли.
      image.replaceWith(document.createTextNode(`[картинка: ${image.alt || image.dataset.src}]`));
      continue;
    }
    const link = el("a", "md-image");
    Object.assign(link, { href: url, target: "_blank", rel: "noopener" });
    Object.assign(image, { src: url, loading: "lazy" });
    image.replaceWith(link);
    link.append(image);
  }
  for (const code of target.querySelectorAll("pre > code")) {
    highlightCode(code, code.dataset.language);
    if (withTools) code.parentElement.append(copyButton("copy", () => code.textContent));
  }
  if (!withTools) return;
  // Диаграммы — только в готовом тексте: пока ответ печатается, блок ещё не дописан.
  renderDiagrams(target);
  // Пути в `коде` открываются во вкладке «Файлы»: щелчок ловит app.js по классу path-link.
  for (const code of target.querySelectorAll(":not(pre) > code")) markPath(code, code.textContent);
}

// Похоже ли на путь: с «/» — любое, без — имя файла с известным расширением. «:42» в конце — строка.
const FILE_EXTENSIONS = new Set(("md markdown txt py js mjs cjs ts tsx jsx json yaml yml toml ini cfg conf env xml html htm css scss "
  + "sh bash ps1 sql csv log go rs java kt c h cpp hpp cs rb php vue lock").split(" "));

function pathOf(text) {
  const value = text.trim();
  if (!value || value.length > 300 || /[\n<>{}()=,;|*?"'`]|:\/\//.test(value) || value.startsWith("-")) return null;
  const [, raw, line] = /^(.*?)(?::(\d+))?(?::\d+)?$/.exec(value);
  const path = raw.replace(/\/+$/, "");
  if (!path) return null;
  if (!path.includes("/")) {
    const extension = /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase();
    if (/\s/.test(path) || !FILE_EXTENSIONS.has(extension)) return null;
  }
  return { path, line: line ? Number(line) : null };
}

function markPath(node, text) {
  const found = pathOf(text);
  if (!found) return;
  node.classList.add("path-link");
  node.dataset.path = found.path;
  if (found.line) node.dataset.line = String(found.line);
  node.title = `Открыть во вкладке «Файлы»${found.line ? `, строка ${found.line}` : ""}`;
}

export function scrollDown(container) {
  container.scrollTop = container.scrollHeight;
}

// Сводка хода одной строкой: «12,4 с · вход 2,9k · выход 312 · 165 ток/с».
function statsText(stats) {
  return [
    duration(stats.seconds),
    stats.input_tokens != null && `вход ${compact(stats.input_tokens)}`,
    stats.output_tokens != null && `выход ${compact(stats.output_tokens)}`,
    stats.tokens_per_second && `${Math.round(stats.tokens_per_second)} ток/с`,
    stats.rounds > 1 && `запросов: ${stats.rounds}`,
  ].filter(Boolean).join(" · ");
}

function compact(value) {
  const short = (number, unit) => `${number.toFixed(1).replace(/\.0$/, "").replace(".", ",")}${unit}`;
  if (value < 1000) return String(value);
  if (value < 1e6) return value < 1e4 ? short(value / 1e3, "k") : `${Math.round(value / 1e3)}k`;
  return short(value / 1e6, "M");
}

function duration(seconds) {
  if (seconds < 60) return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1).replace(".", ",")} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${Math.round(seconds - minutes * 60)} с`;
}

function round(seconds) {
  return seconds < 10 ? Math.max(Math.round(seconds * 10) / 10, 0.1) : Math.round(seconds);
}


// Длинная реплика пользователя (задание целиком) свёрнута до нескольких строк, развернуть — кнопкой.
const USER_FOLD_CHARS = 600;
const USER_FOLD_LINES = 10;

function userNode(message) {
  const node = el("article", "msg user");
  const bubble = el("div", "bubble");
  const text = el("div", "bubble-text", message.content);
  bubble.append(text);
  if (message.attachments?.length) bubble.prepend(imagesNode(message.attachments));
  node.append(bubble);
  if (message.content.length > USER_FOLD_CHARS || message.content.split("\n").length > USER_FOLD_LINES) {
    const fold = el("button", "msg-action fold");
    fold.type = "button";
    const sync = () => {
      const open = unfolded.has(message.id);
      node.classList.toggle("folded", !open);
      fold.textContent = open ? "Свернуть" : "Показать полностью";
    };
    fold.addEventListener("click", () => {
      if (!unfolded.delete(message.id)) unfolded.add(message.id);
      sync();
    });
    sync();
    bubble.append(fold);
  }
  return node;
}

// Картинки реплики: миниатюры, щелчок открывает картинку целиком.
function imagesNode(names) {
  const box = el("div", "msg-images");
  for (const name of names) {
    const link = el("a");
    Object.assign(link, { href: `/api/attachments/${name}`, target: "_blank", rel: "noopener" });
    const image = el("img");
    Object.assign(image, { src: link.href, alt: name, loading: "lazy" });
    link.append(image);
    box.append(link);
  }
  return box;
}

// Подвал (время, токены, «Копировать») виден у последнего ответа, у остальных — при наведении.
// Отметка «ход остановлен» — не ответ: копировать и считать в ней нечего.
const STOPPED_TEXT = "_(ход остановлен)_";

// stage — подпись итога этапа: «Этап 1 · Тест-кейсы».
function assistantNode(message, last = true, stage = "") {
  const node = el("article", `msg assistant${stage ? " stage" : ""}`);
  if (stage) node.append(el("div", "stage-head", `✓ ${stage}`));
  const body = el("div", "text");
  renderMarkdown(body, message.content);
  node.append(body);
  if (message.content.trim() !== STOPPED_TEXT) node.append(footNode(message, [], !last));
  return node;
}

// Строка ленты команды: саб-агент обратился к другому или ответил ему. Свёрнута до одной строки;
// раскрытая — весь текст и ссылка на разговор того агента, где это было.
const openedTeamLines = new Set();

function teamNode(item) {
  const key = `${item.session_id}:${item.at}`;
  const node = el("details", `team-line ${item.kind}`);
  node.open = openedTeamLines.has(key);
  node.addEventListener("toggle", () => {
    if (node.open) openedTeamLines.add(key);
    else openedTeamLines.delete(key);
  });
  const summary = el("summary");
  const preview = item.text.replace(/\s+/g, " ").trim();
  summary.append(
    el("span", "team-route", `◆ ${item.from} → ${item.to}${item.kind === "answer" ? " (ответ)" : ""}`),
    el("span", "team-preview", preview.length > 140 ? `${preview.slice(0, 139)}…` : preview),
  );
  const body = el("div", "text");
  renderMarkdown(body, item.text);
  const open = el("button", "msg-action team-open", "Открыть разговор");
  open.type = "button";
  open.dataset.session = item.session_id;
  node.append(summary, body, open);
  return node;
}

// Отчёт саб-агента: заголовок рамки — кто и какая задача, текст — сам результат, длинный свёрнут.
const REPORT_PREFIX = /^Результат задачи «([^\n]*)» от саб-агента [^\n]*:\n+/;

function reportNode(message) {
  const node = el("article", "msg report");
  const match = message.content.match(REPORT_PREFIX);
  const text = match ? message.content.slice(match[0].length) : message.content;
  const body = el("div", "text");
  renderMarkdown(body, text);
  const extra = [];
  if (text.length > REPORT_FOLD_CHARS || text.split("\n").length > REPORT_FOLD_LINES) {
    const fold = el("button", "msg-action fold");
    fold.type = "button";
    const sync = () => {
      const open = unfolded.has(message.id);
      node.classList.toggle("folded", !open);
      fold.textContent = open ? "Свернуть" : "Показать полностью";
    };
    fold.addEventListener("click", () => {
      if (!unfolded.delete(message.id)) unfolded.add(message.id);
      sync();
    });
    sync();
    extra.push(fold);
  }
  // Отчёт агента — в рамке, как вызовы инструментов: имя и задача на верхней кромке.
  const head = el("div", "report-head");
  const label = el("span", "", `◆ ${message.sender}${match ? ` · ${match[1]}` : ""}`);
  if (match) label.title = match[1];
  head.append(label);
  node.append(head, body, footNode(message, extra));
  return node;
}

// Подвал ответа: сводка хода, если она сохранена, и копирование всего ответа в Markdown.
function footNode(message, extra = [], quiet = false) {
  const foot = el("div", `msg-foot${(message.stats || extra.length) && !quiet ? "" : " bare"}`);
  foot.append(...extra);
  if (message.stats) {
    const stats = el("span", "stats", statsText(message.stats));
    stats.title = "Время работы модели и инструментов без ожидания ваших решений. "
      + "Вход — размер последнего запроса к модели, выход — всё, что она написала за ход.";
    foot.append(stats);
  }
  foot.append(copyButton("msg-action push", () => message.content));
  return foot;
}

function stepNode(call, result, seconds) {
  const state = !result ? "pending" : result.is_error ? "failed" : "done";
  const node = el("details", `step ${state}`);
  remembered(node, `call:${call.id}`);
  const args = parse(call.arguments);
  // Браузер: element — что за элемент, text — что ввели, target — ссылка на элемент из снимка.
  const target = args.path || args.query || args.url || args.jql || args.key || args.name || args.agent
    || (Array.isArray(args.names) ? args.names.join(", ") : "") || (Array.isArray(args.steps) ? args.steps.join(" · ") : "")
    || [args.element, args.text && `«${args.text}»`].filter(Boolean).join(" ← ") || args.target || args.action || "";
  const summary = el("summary");
  const icon = el("span", "step-icon", STEP_ICON[state]);
  icon.title = { done: "Готово", failed: "Ошибка", pending: "Выполняется" }[state];
  const label = el("span", "step-target", target ? String(target) : "");
  if (args.path) markPath(label, String(args.path));
  // План задачи: «План задачи · Тест-кейсы · Баг-репорты» и «Этап завершён · 1 — в чате».
  const plan = call.name === "plan" && args.action === "done";
  const name = el("span", "step-name", plan ? "Этап завершён" : describe(call.name)[0]);
  if (plan) label.textContent = [args.step, args.note].filter(Boolean).join(" — ");
  name.title = call.name;
  const metaText = state === "failed" ? "ошибка" : state === "pending" ? "…" : seconds != null ? duration(round(seconds)) : "";
  summary.append(icon, name, label, el("span", "step-meta", metaText));
  const body = el("pre");
  body.textContent = `${call.name} ${JSON.stringify(args, null, 2)}\n\n→ ${result?.content ?? "—"}`;
  node.append(summary, body);
  return node;
}

function copyButton(className, text) {
  const button = el("button", className, "Копировать");
  button.type = "button";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text());
      button.textContent = "Скопировано";
    } catch {
      button.textContent = "Не удалось";
    }
    setTimeout(() => { button.textContent = "Копировать"; }, 1500);
  });
  return button;
}

export function parse(raw) {
  try {
    const value = JSON.parse(raw || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

export function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}
