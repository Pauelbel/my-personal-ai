"""testcase-generate: тест-кейсы по Markdown-документации, по кускам страниц, с продолжением с места остановки.

Аргументы — JSON в stdin: {"path": "docs/auth", "limit": 20, "force": false}. Итог — в stdout, ход работы
(«docs/a.md: кусок 3 из 12») — в stderr. Состояние каждой страницы (готовые куски) — в MEEPO_STATE_DIR:
повторный запуск берёт только недоделанные куски. Если страница, правила проекта или сам скрипт изменились,
страница генерируется заново.
"""

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import meepo_skill as ms
from cases import CONFIG_DIR, MAX_CASES, SCHEMA, lint, system_prompt, table, valid

OUTPUT_DIR = f"{CONFIG_DIR}/test_cases"
# Кусок около 6–7 тыс. токенов: вместе с промптом и ответом помещается даже в окно 16K.
CHUNK_CHARS = 20000
MIN_CHARS = 400
# Файл больше этого в папке — обычно сводный (вся документация одним файлом): его пропускаем.
# Файл, указанный путём напрямую, обрабатывается при любом размере — по кускам.
MAX_PAGE_BYTES = 1024 * 1024
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
SKIPPED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".meepo"}
SCRIPTS = Path(__file__).resolve().parent
CODE_HASH = hashlib.sha256(b"".join(path.read_bytes() for path in sorted(SCRIPTS.glob("*.py")))).hexdigest()


@dataclass(frozen=True)
class Page:
    relative: str
    title: str
    location: str
    source: str
    text: str


@dataclass
class Result:
    relative: str
    output: str = ""
    cases: int = 0
    chunks: int = 0
    style_issues: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: str = ""


def find_pages(root: Path, start: Path) -> list[Path]:
    """Markdown-страницы раздела по порядку обхода; папка testgen/ и служебные папки пропускаются."""
    if start.is_file():
        return [start]
    config = (root / CONFIG_DIR).resolve()
    pages: list[Path] = []
    for directory, dirs, files in os.walk(start):
        dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS and not name.startswith(".")
                         and (Path(directory) / name).resolve() != config)
        pages += [Path(directory) / name for name in sorted(files) if Path(name).suffix.lower() in {".md", ".markdown"}]
    return pages


def parse_page(path: Path, root: Path) -> Page:
    text = path.read_text(encoding="utf-8")
    relative = path.relative_to(root).as_posix()
    meta: dict[str, str] = {}
    front = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if front:
        for line in front.group(1).splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"')
        text = text[front.end():]
    heading = re.search(r"(?m)^# (.+)$", text)
    title = meta.get("title") or (heading.group(1).strip() if heading else path.stem)
    # Картинки модели не видны, оглавления и списки ссылок не несут тестируемого содержания.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"(?m)^[ \t]*[-*][ \t]*\[[^\]]*\]\([^)]*\)[ \t]*$\n?", "", text)
    text = re.sub(r"(?m)^# .*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Page(relative, title, meta.get("path") or relative, meta.get("source", ""), text)


def fingerprint(page: Page, system: str) -> str:
    """Всё, от чего зависят кейсы страницы: если что-то поменялось, прежние куски не годятся."""
    value = json.dumps({"page": page.__dict__, "system": system, "chunk": CHUNK_CHARS, "code": CODE_HASH},
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ask_cases(system: str, page: Page, chunk: str, index: int, total: int) -> list[dict]:
    user = (f"Страница: {page.title}\nПуть в документации: {page.location}\n"
            + (f"Фрагмент {index} из {total}.\n" if total > 1 else "")
            + f"\n<документация>\n{chunk}\n</документация>\n\nСоставь тест-кейсы (не более {MAX_CASES}) по этому фрагменту.")
    data = ms.ask_json(system, user, SCHEMA)
    return [case for case in data.get("test_cases") or [] if valid(case)]


def render(page: Page, cases: list[dict]) -> str:
    lines = [f"# Тест-кейсы: {page.title}", "", f"- Документация: {page.location}",
             *([f"- Источник: {page.source}"] if page.source else []), f"- Количество кейсов: {len(cases)}", "",
             table(cases)]
    return "\n".join(lines) + "\n"


def generate_page(root: Path, path: Path, system: str, *, force: bool, whole: bool) -> Result:
    relative = path.relative_to(root).as_posix()
    result = Result(relative)
    if not whole and path.stat().st_size > MAX_PAGE_BYTES:
        result.skipped = "файл больше 1 МБ — похоже на сводный; чтобы обработать его, укажите путь к нему"
        return result
    page = parse_page(path, root)
    if len(page.text) < MIN_CHARS:
        result.skipped = "мало текста"
        return result
    parts = ms.chunks(page.text, CHUNK_CHARS)
    result.chunks = len(parts)
    state = ms.State(f"page-{relative}")
    mark = fingerprint(page, system)
    if force or state.get("fingerprint") != mark or len(state.get("chunks") or []) != len(parts):
        state.data = {"fingerprint": mark, "chunks": [None] * len(parts)}
        state.save()
    for index, part in enumerate(parts):
        if state.data["chunks"][index] is not None:
            continue
        ms.progress(f"{relative}: кусок {index + 1} из {len(parts)}")
        try:
            state.data["chunks"][index] = ask_cases(system, page, part, index + 1, len(parts))
        except ms.ModelError as exc:
            result.errors.append(f"кусок {index + 1}/{len(parts)}: {exc}")
            continue
        state.save()
    # Страница с потерянными кусками не записывается: иначе она считалась бы готовой без части кейсов.
    if result.errors:
        return result
    cases = [case for chunk in state.data["chunks"] for case in chunk]
    out = root / OUTPUT_DIR / relative
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(page, cases), encoding="utf-8")
    issues = lint(cases)
    report = out.with_name(out.stem + ".style.txt")
    if issues:
        report.write_text("\n".join(issues) + "\n", encoding="utf-8")
    else:
        report.unlink(missing_ok=True)
    state.set("published", mark)
    result.output, result.cases, result.style_issues = out.relative_to(root).as_posix(), len(cases), len(issues)
    return result


def pending(root: Path, path: Path, system: str, whole: bool) -> bool:
    """Странице нужны кейсы: файла нет или он сделан по другой версии страницы, правил или скрипта."""
    if not whole and path.stat().st_size > MAX_PAGE_BYTES:
        return False
    page = parse_page(path, root)
    if len(page.text) < MIN_CHARS:
        return False
    relative = path.relative_to(root).as_posix()
    published = ms.State(f"page-{relative}").get("published")
    return not (root / OUTPUT_DIR / relative).is_file() or published != fingerprint(page, system)


def summary(root: Path, start: Path, pages: list[Path], results: list[Result], system: str, whole: bool) -> str:
    remaining = sum(1 for path in pages if pending(root, path, system, whole))
    section = start.relative_to(root).as_posix() or "."
    lines = [f"Раздел: {section}. Страниц: {len(pages)}, осталось без тест-кейсов: {remaining}."]
    done = [page for page in results if page.output]
    failed = [page for page in results if page.errors]
    skipped = [page for page in results if page.skipped]
    if done:
        lines.append("Готово:")
        lines += [f"- {page.output}: кейсов {page.cases}, кусков {page.chunks}, замечаний по стилю {page.style_issues}"
                  for page in done]
    if failed:
        lines.append("Не записано из-за ошибок (повторите запуск — готовые куски сохранены):")
        lines += [f"- {page.relative}: {'; '.join(page.errors)}" for page in failed]
    if skipped:
        lines.append(f"Пропущено страниц: {len(skipped)} ({'; '.join(sorted({page.skipped for page in skipped}))}).")
    if not done and not failed:
        lines.append("Новых страниц для генерации нет." if not remaining else "Ничего не сгенерировано.")
    if done:
        target = done[0].output if len(done) == 1 else f"{OUTPUT_DIR}"
        # Ревью — отдельное решение пользователя: модель его только предлагает, а не запускает сама.
        lines.append("\nПредложи пользователю проверить кейсы скиллом testcase-review с аргументами "
                     f"{json.dumps({'cases': target, 'docs': section}, ensure_ascii=False)} — запускай, только если он согласится.")
    return "\n".join(lines)


def main() -> int:
    arguments = ms.args()
    root = ms.project()
    limit, force = arguments.get("limit", DEFAULT_LIMIT), arguments.get("force", False) is True
    if isinstance(limit, str) and limit.strip().isdigit():
        limit = int(limit)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        print(f"limit — целое число от 1 до {MAX_LIMIT}")
        return 2
    try:
        start = ms.inside(arguments.get("path"))
    except ValueError as exc:
        print(exc)
        return 2
    if not start.exists():
        print(f"Нет папки или файла {arguments.get('path')} в проекте")
        return 2
    whole = start.is_file()
    pages = find_pages(root, start)
    system = system_prompt(root)
    results: list[Result] = []
    processed = 0
    try:
        for path in pages:
            if processed >= limit:
                break
            if not force and not pending(root, path, system, whole):
                continue
            result = generate_page(root, path, system, force=force, whole=whole)
            results.append(result)
            if result.output:
                ms.progress(f"+ {result.output}: кейсов {result.cases}")
            if not result.skipped:
                processed += 1
    except ms.Unavailable as exc:
        print(f"{summary(root, start, pages, results, system, whole)}\n{exc}. Готовые куски сохранены, "
              "повторный запуск продолжит с места остановки.")
        return 1
    print(summary(root, start, pages, results, system, whole))
    attempted = [page for page in results if not page.skipped]
    return 0 if all(page.output and not page.errors for page in attempted) else 1


if __name__ == "__main__":
    sys.exit(main())
