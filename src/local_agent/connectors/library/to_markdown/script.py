"""Документ → Markdown: PDF, DOCX, XLSX, PPTX, HTML, CSV, JSON, XML и EPUB переводятся в файлы .md в проекте.

Аргументы — JSON-объект в stdin:
- path — документ или папка проекта (папка — со всеми документами внутри, кроме скрытых и служебных папок);
- save — куда записать: для документа — файл .md, для папки — папка; по умолчанию — рядом, с тем же именем и .md.
Переводит пакет markitdown из окружения песочницы. Первая строка результата — метка: по ней скрипт узнаёт свои файлы.
Чужой .md с тем же именем не перезаписывается (результат ложится в <документ>.<расширение>.md), а при обходе папки
документ, у которого перевод новее самого документа, пропускается. Итог — в stdout, ход работы — в stderr.
"""

import json
import os
import sys
from pathlib import Path

SUFFIXES = (".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".csv", ".json", ".xml", ".epub")
SKIPPED = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".meepo"}
MARK = "<!-- Markdown из {name}: перевёл коннектор to_markdown -->"
MARK_START = "<!-- Markdown из "
MAX_FILES = 200
# Больше этого результат в окно модели целиком не войдёт: подсказываем, как его читать.
LARGE_CHARS = 64 * 1024


class Problem(Exception):
    pass


def main() -> int:
    arguments = json.load(sys.stdin)
    root = Path.cwd().resolve()
    source = inside(root, arguments.get("path"), "path")
    save = arguments.get("save")
    target = inside(root, save, "save") if isinstance(save, str) and save.strip() else None
    try:
        from markitdown import MarkItDown
    except ImportError:
        raise Problem("Нет пакета markitdown: его нужно установить через install_packages — markitdown[pdf,docx,xlsx,pptx]") from None
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
        lines.append("\nБольшой результат целиком в окно модели не войдёт: читай его через read_file (оглавление, затем "
                     "куски по offset), а работу по всему документу делай через process_document.")
    print("\n".join(lines))
    return 0 if any(ok for ok, _, _ in results) or not results else 1


def convert(converter, root: Path, document: Path, output: Path, explicit: bool, fresh_skip: bool = False) -> tuple[bool, str, int]:
    """(получилось, строка итога, символов в результате)."""
    name = relative(root, document)
    if not explicit and output.exists() and not ours(output):
        output = output.with_name(document.name + ".md")
    if fresh_skip and output.exists() and ours(output) and output.stat().st_mtime >= document.stat().st_mtime:
        return True, f"{name}: перевод {relative(root, output)} уже свежий — пропущен", 0
    print(f"→ {name}", file=sys.stderr, flush=True)
    try:
        result = converter.convert_local(str(document))
    except Exception as exc:  # noqa: BLE001 — любой сбой конвертера — строка итога, остальные документы идут дальше
        return False, f"{name}: не переведён — {type(exc).__name__}: {str(exc)[:300]}", 0
    text = (getattr(result, "markdown", None) or getattr(result, "text_content", "") or "").strip()
    if not text:
        return False, f"{name}: текста нет — похоже на скан без текстового слоя, нужен OCR", 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(MARK.format(name=document.name) + "\n\n" + text + "\n", encoding="utf-8")
    lines = text.count("\n") + 1
    return True, f"{name} → {relative(root, output)}: {len(text)} символов, {lines} строк", len(text)


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


def inside(root: Path, value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise Problem(f"Нужен {name}: путь относительно папки проекта")
    path = Path(value.strip().strip("`"))
    if path.is_absolute():
        raise Problem(f"{name} — путь относительно папки проекта, а не абсолютный")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise Problem(f"{name} выходит за пределы папки проекта")
    return resolved


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() or "."


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Problem as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
