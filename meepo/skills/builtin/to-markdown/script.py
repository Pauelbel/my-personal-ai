"""to-markdown: PDF, DOCX, XLSX, PPTX, HTML, CSV, JSON, XML и EPUB → файлы .md в проекте (пакет markitdown).

Аргументы — JSON в stdin: {"path": "specs/spec.pdf", "save": "specs/spec.md"}. Первая строка результата — метка:
по ней скрипт узнаёт свои файлы. Чужой .md с тем же именем не перезаписывается, а при обходе папки документ,
у которого перевод новее самого документа, пропускается. Итог — в stdout, ход работы — в stderr.
"""

import os
import sys
from pathlib import Path

import meepo_skill as ms

SUFFIXES = (".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".csv", ".json", ".xml", ".epub")
SKIPPED = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".meepo"}
MARK = "<!-- Markdown из {name}: перевёл скилл to-markdown -->"
MARK_START = "<!-- Markdown из "
MAX_FILES = 200
# Больше этого результат в окно модели целиком не войдёт: подсказываем, как его читать.
LARGE_CHARS = 64 * 1024


class Problem(Exception):
    pass


def main() -> int:
    arguments = ms.args()
    root = ms.project()
    try:
        source = ms.inside(arguments.get("path"))
        save = arguments.get("save")
        target = ms.inside(save, "save") if isinstance(save, str) and save.strip() else None
    except ValueError as exc:
        raise Problem(str(exc)) from None
    try:
        from markitdown import MarkItDown
    except ImportError:
        raise Problem("Нет пакета markitdown: он ставится при запуске скилла из requires в SKILL.md") from None
    converter = MarkItDown(enable_plugins=False)
    results: list[tuple[bool, str, int]] = []
    if source.is_file():
        if source.suffix.lower() not in SUFFIXES:
            raise Problem(f"{source.name}: такие файлы не переводятся. Подходят: {', '.join(SUFFIXES)}")
        if target is not None and target.suffix.lower() != ".md":
            raise Problem("save для документа — файл .md")
        results.append(convert(converter, root, source, target or source.with_suffix(".md"), explicit=target is not None))
    elif source.is_dir():
        documents = find(source)
        if not documents:
            raise Problem(f"В папке {relative(root, source)} нет документов: {', '.join(SUFFIXES)}")
        planned: set[Path] = set()
        for document in documents[:MAX_FILES]:
            base = (target / document.relative_to(source)) if target else document
            output = base.with_suffix(".md")
            if output in planned:
                # spec.pdf и spec.docx в одной папке: второй ложится в spec.docx.md.
                output = base.with_name(base.name + ".md")
            planned.add(output)
            results.append(convert(converter, root, document, output, explicit=False, fresh_skip=True))
        if len(documents) > MAX_FILES:
            results.append((False, f"Документов больше {MAX_FILES}: переведены первые {MAX_FILES}, запустите ещё раз", 0))
    else:
        raise Problem(f"Нет файла или папки {arguments.get('path')}")
    lines = [line for _, line, _ in results]
    if any(size > LARGE_CHARS for _, _, size in results):
        lines.append("\nБольшой результат целиком в окно модели не войдёт: читай его через read_file (оглавление, "
                     "затем куски по offset), а тест-кейсы по нему пиши скиллом testcase-generate.")
    print("\n".join(lines))
    return 0 if any(ok for ok, _, _ in results) else 1


def convert(converter, root: Path, document: Path, output: Path, explicit: bool, fresh_skip: bool = False) -> tuple[bool, str, int]:
    """(получилось, строка итога, символов в результате)."""
    name = relative(root, document)
    if not explicit and output.exists() and not ours(output):
        output = output.with_name(document.name + ".md")
    if fresh_skip and output.exists() and ours(output) and output.stat().st_mtime >= document.stat().st_mtime:
        return True, f"{name}: перевод {relative(root, output)} уже свежий — пропущен", 0
    ms.progress(f"→ {name}")
    try:
        result = converter.convert_local(str(document))
    except Exception as exc:  # noqa: BLE001 — сбой конвертера — строка итога, остальные документы идут дальше
        return False, f"{name}: не переведён — {type(exc).__name__}: {str(exc)[:300]}", 0
    text = (getattr(result, "markdown", None) or getattr(result, "text_content", "") or "").strip()
    if not text:
        return False, f"{name}: текста нет — похоже на скан без текстового слоя, нужен OCR", 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(MARK.format(name=document.name) + "\n\n" + text + "\n", encoding="utf-8")
    return True, f"{name} → {relative(root, output)}: {len(text)} символов, {text.count(chr(10)) + 1} строк", len(text)


def find(folder: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirs, names in os.walk(folder):
        dirs[:] = sorted(name for name in dirs if name not in SKIPPED and not name.startswith("."))
        found += [Path(current) / name for name in sorted(names) if Path(name).suffix.lower() in SUFFIXES]
    return found


def ours(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline().startswith(MARK_START)
    except (OSError, UnicodeError):
        return False


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() or "."


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Problem as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
