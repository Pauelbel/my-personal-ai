// Шаги агента на экране: по порядку — на карточке агента, в карточке запуска и в предложении агента.
// Шаг раскрывается: задание модели (или команда, скрипт), что он читает и куда пишет, что делать при ошибке.
import { el, kindBadge } from "./chat.js";

export function stepList(plan) {
  const list = el("ol", "steps-list");
  plan.steps.forEach((step, index) => {
    const item = el("li", `steps-step kind-${step.kind}`);
    const row = el("details");
    const summary = el("summary");
    summary.append(el("span", "rs-num", String(index + 1)), kindBadge(step.kind), el("span", "rs-title", step.title));
    if (step.on_error === "retry") {
      const flag = el("span", "rs-flag", `↺ ${step.attempts}`);
      flag.title = `При ошибке вернуться к шагу «${step.retry_to}» — до ${step.attempts} раз`;
      summary.append(flag);
    }
    row.append(summary, stepDetail(step));
    item.append(row);
    list.append(item);
  });
  return list;
}

function stepDetail(step) {
  const box = el("div", "rs-detail");
  if (step.kind === "command") box.append(el("pre", "rs-code", step.text));
  else if (step.kind === "tool" || step.kind === "script") {
    box.append(el("p", "rs-prompt", step.kind === "tool" ? `Вызывает инструмент ${step.text}` : `Запускает скрипт scripts/${step.text}`));
    if (Object.keys(step.params ?? {}).length) box.append(el("pre", "rs-code", JSON.stringify(step.params, null, 2)));
  } else box.append(el("p", "rs-prompt", step.text));
  const facts = [
    step.files.length && ["читает", step.files.join(", ")],
    step.parts && ["по частям", step.parts],
    step.save && ["пишет итог в", step.save],
    step.tools.length && ["инструменты", step.tools.join(", ")],
    step.on_error === "retry" && ["при ошибке", `вернуться к «${step.retry_to}», до ${step.attempts} раз`],
    step.on_error === "continue" && ["при ошибке", "идти дальше"],
  ].filter(Boolean);
  if (facts.length) {
    const list = el("dl", "rs-facts");
    for (const [name, value] of facts) list.append(el("dt", "", name), el("dd", "", value));
    box.append(list);
  }
  return box;
}

// {имя} → значение поля: так в карточке видно, какую команду шаги запустят с этими данными.
export function fill(text, values) {
  return text.replace(/\{(\p{L}[\p{L}\p{N}_-]*)\}/gu, (whole, name) => (values[name] ? values[name] : whole));
}

// Что шаги будут делать с проектом: команды, скрипты и пути — кодом, с подставленными данными.
export function effectsList(effects, values) {
  const list = el("ul", "steps-effects");
  const codes = [];
  for (const effect of effects) {
    const item = el("li");
    item.append(el("span", "", effect.text));
    if (effect.code) {
      const code = el("pre", "rs-code", fill(effect.code, values));
      codes.push([code, effect.code]);
      item.append(code);
    }
    list.append(item);
  }
  // Данные поменяли в карточке — команды пересчитываются.
  list.refresh = (current) => {
    for (const [code, template] of codes) code.textContent = fill(template, current);
  };
  return list;
}
