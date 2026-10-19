"""testcase-review: ревью Markdown-файлов с тест-кейсами пачками, с продолжением с места остановки.

Аргументы — JSON в stdin: {"cases": "cases/login.md", "docs": "docs/auth.md", "limit": 20}. Каждая пачка кейсов —
отдельный запрос к модели с чистым контекстом. Готовые пачки — в MEEPO_STATE_DIR; итог — <файл>.review.md рядом
с кейсами и вопросы в колонке «Вопросы», если она есть.
"""

import hashlib
import json
import shutil
import sys
from pathlib import Path

import meepo_skill as ms
from parse import batch_text, batches, docs_for, fill_questions, load_docs, normalize, parse

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
SCRIPTS = Path(__file__).resolve().parent
CODE_HASH = hashlib.sha256(b"".join(path.read_bytes() for path in sorted(SCRIPTS.glob("*.py")))).hexdigest()

RULES = """Ты — строгий ревьюер ручных тест-кейсов. Найди ошибки в контенте, структуре и оформлении кейсов этой пачки.
Один тест-кейс = один функциональный блок: валидация полей, ошибки и успешный сценарий — шагами внутри одного кейса.
Номера в колонках «Тестовые данные» и «Ожидаемый результат» — номера шагов, к которым они относятся: это правильный формат."""

WITH_DOCS = RULES + """

Сверь кейсы с фрагментом документации и выпиши замечания со ссылкой на ID кейса:
1. Что из фрагмента документации, относящегося к этим кейсам, не покрыто (case — «покрытие»).
2. Кейсы и шаги, которых нет в документации: выдуманные параметры, пункты меню, значения.
3. Шаги без проверяемого ожидаемого результата или с расплывчатыми ожиданиями («работает корректно», «успешно»).
4. Нарушение группировки (разбитый на части блок) или дубли.
5. Стиль: шаг не начинается с глагола, длинные предложения, канцелярит.
questions — вопросы к требованиям: неясности и противоречия документации, мешающие проверить кейс."""

WITHOUT_DOCS = RULES + """

Документации нет: не суди о полноте покрытия и не выдумывай требования. Выпиши замечания со ссылкой на ID кейса:
1. Шаги без проверяемого ожидаемого результата или с расплывчатыми ожиданиями («работает корректно», «успешно»).
2. Непонятные или неполные предусловия и тестовые данные; шаги, которые нельзя выполнить однозначно.
3. Нарушение группировки (разбитый на части блок) или дубли внутри пачки.
4. Стиль: шаг не начинается с глагола, длинные предложения, канцелярит.
questions — вопросы к требованиям, без ответа на которые кейс нельзя проверить."""

TAIL = "\n\nПиши по-русски, только замечания, без пересказа кейсов. verdict — можно ли отдавать эти кейсы в работу и что поправить в первую очередь."

SCHEMA = {
    "type": "object",
    "properties": {
        "remarks": {"type": "array", "items": {"type": "object", "properties": {
            "case": {"type": "string"}, "issue": {"type": "string"}}, "required": ["case", "issue"]}},
        "questions": {"type": "array", "items": {"type": "object", "properties": {
            "case": {"type": "string"}, "question": {"type": "string"}}, "required": ["case", "question"]}},
        "verdict": {"type": "string"},
    },
    "required": ["remarks", "questions", "verdict"],
}

DUPLICATES = """Ты — ревьюер тест-кейсов. Ниже — список всех кейсов файла: ID и название. Найди дубли и сильные пересечения
(два кейса проверяют один и тот же функциональный блок). Ответ — список строк «ID, ID — почему дубль»; если дублей нет — «Дублей нет». По-русски."""


def review_batch(system: str, text: str, docs: str) -> dict:
    user = (f"<документация>\n{docs}\n</документация>\n\n" if docs else "") + f"<тест-кейсы>\n{text}\n</тест-кейсы>"
    data = ms.ask_json(system, user, SCHEMA)
    return {"remarks": [item for item in data.get("remarks") or [] if isinstance(item, dict)],
            "questions": [item for item in data.get("questions") or [] if isinstance(item, dict)],
            "verdict": str(data.get("verdict") or "")}


def review_file(root: Path, path: Path, docs_path: Path | None, docs: str) -> tuple[str, str, int]:
    """(файл замечаний, вывод, сколько вопросов дописано). Модель недоступна — ms.Unavailable, куски сохранены."""
    relative = path.relative_to(root).as_posix()
    cases = parse(path.read_text(encoding="utf-8"))
    groups = batches(cases)
    system = (WITH_DOCS if docs else WITHOUT_DOCS) + TAIL
    texts = [batch_text(cases, group) for group in groups]
    mark = hashlib.sha256(json.dumps([texts, docs, system, CODE_HASH], ensure_ascii=False).encode("utf-8")).hexdigest()
    state = ms.State(f"review-{relative}")
    if state.get("fingerprint") != mark:
        state.data = {"fingerprint": mark, "batches": [None] * len(groups), "duplicates": None}
        state.save()
    for index, text in enumerate(texts):
        if state.data["batches"][index] is not None:
            continue
        ms.progress(f"{relative}: пачка {index + 1} из {len(texts)}")
        state.data["batches"][index] = review_batch(system, text, docs_for(docs, text))
        state.save()
    if len(groups) > 1 and state.data.get("duplicates") is None:
        ms.progress(f"{relative}: поиск дублей по всему файлу")
        titles = "\n".join(f"{item.id} — {item.text[:160]}" for item in cases.items)
        state.set("duplicates", ms.ask_llm(DUPLICATES, titles))
    report = render(relative, docs_path, root, groups, state.data)
    out = path.with_name(path.stem + ".review.md")
    out.write_text(report, encoding="utf-8")
    questions: dict[str, list[str]] = {}
    for result in state.data["batches"]:
        for item in result["questions"]:
            questions.setdefault(normalize(str(item.get("case", ""))), []).append(str(item.get("question", "")))
    added = fill_questions(cases, questions)
    if added:
        backup = Path(state.path).parent / "backup" / relative
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
        path.write_text("\n".join(cases.lines) + "\n", encoding="utf-8")
    verdicts = [result["verdict"] for result in state.data["batches"] if result["verdict"]]
    return out.relative_to(root).as_posix(), verdicts[-1] if verdicts else "", added


def render(relative: str, docs_path: Path | None, root: Path, groups: list, data: dict) -> str:
    docs = docs_path.relative_to(root).as_posix() if docs_path else "не указана — проверены качество, стиль, проверяемость и дубли"
    lines = [f"# Ревью тест-кейсов: {relative}", "", f"- Тест-кейсы: {relative}", f"- Документация: {docs}", ""]
    for index, (group, result) in enumerate(zip(groups, data["batches"], strict=True), start=1):
        span = group[0].id if len(group) == 1 else f"{group[0].id} – {group[-1].id}"
        lines += [f"## Пачка {index} из {len(groups)} ({span})", ""]
        lines += [f"- **{item.get('case', '?')}**: {item.get('issue', '')}" for item in result["remarks"]] or ["Замечаний нет."]
        if result["questions"]:
            lines += ["", "Вопросы к требованиям:"]
            lines += [f"- **{item.get('case', '?')}**: {item.get('question', '')}" for item in result["questions"]]
        lines += ["", f"Вывод: {result['verdict']}", ""]
    if data.get("duplicates"):
        lines += ["## Дубли по всему файлу", "", data["duplicates"], ""]
    return "\n".join(lines)


def case_files(start: Path) -> list[Path]:
    if start.is_file():
        return [start]
    return sorted(path for path in start.rglob("*.md")
                  if not path.name.endswith(".review.md") and ".meepo" not in path.parts and ".git" not in path.parts)


def main() -> int:
    arguments = ms.args()
    root = ms.project()
    limit = arguments.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, str) and limit.strip().isdigit():
        limit = int(limit)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        print(f"limit — целое число от 1 до {MAX_LIMIT}")
        return 2
    try:
        start = ms.inside(arguments.get("cases"), "cases")
        docs_path = ms.inside(arguments["docs"], "docs") if arguments.get("docs") else None
    except ValueError as exc:
        print(exc)
        return 2
    for name, path in (("cases", start), ("docs", docs_path)):
        if path is not None and not path.exists():
            print(f"Нет {name}: {path.relative_to(root).as_posix()}")
            return 2
    docs = load_docs(docs_path)
    files = case_files(start)
    if not files:
        print("Файлов тест-кейсов (.md) не найдено")
        return 2
    lines, total = [], 0
    try:
        for path in files[:limit]:
            out, verdict, added = review_file(root, path, docs_path, docs)
            total += added
            lines.append(f"- {out} — {verdict or 'вывода нет'}" + (f" (вопросов дописано: {added})" if added else ""))
    except ms.Unavailable as exc:
        print("\n".join(["Ревью остановлено.", *lines, f"{exc}. Готовые пачки сохранены, повторный запуск продолжит."]))
        return 1
    except ms.ModelError as exc:
        print("\n".join(["Ревью не закончено.", *lines, f"Модель не справилась с пачкой: {exc}. Повторите запуск."]))
        return 1
    head = f"Ревью тест-кейсов: файлов {len(lines)}" + (f", ещё ждут: {len(files) - limit}" if len(files) > limit else "") + "."
    print("\n".join([head, *lines]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
