"""autotest-write: автотест в папку проекта по правилам и примерам репозитория qa_harness.

Аргументы — JSON в stdin: {"task": "...", "layer": "ui", "details": "...", "dir": "tests", "harness": "..."}.
Harness — справочник: скилл скачивает его (zip, без git) в MEEPO_STATE_DIR и раз в сутки обновляет; нет сети —
берёт прежнюю копию. Из него берётся только нужное слою: правила AGENTS.md, примеры examples/<слой>/, сводка API
ядра (clients/, checks/, report.py), а недостающая в проекте обвязка копируется в проект (свои файлы проекта
не перезаписываются). Тест пишет модель; скрипт проверяет его по правилам и прогоняет pytest --collect-only,
при ошибке сборки модель правит его один раз. Итог — в stdout, ход работы — в stderr.

Части файла: harness → слой и что взять → обвязка в проекте → запрос к модели → проверка, запись и сборка → запуск.
"""

import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import meepo_skill as ms

DEFAULT_HARNESS = "https://github.com/Pauelbel/qa_harness/archive/refs/heads/main.zip"
REFRESH_SECONDS = 24 * 3600
DOWNLOAD_TIMEOUT = 60
# Слова задачи и данных, по которым выбирается слой, если layer не задан; не нашлось — слой выбирает модель.
# «Таблица» бывает и на странице, и в БД — по ней слой не выбирается; локаторы page.… — это всегда UI.
KEYWORDS = {
    "ui": ("ui", "интерфейс", "страниц", "браузер", "фронт", "кнопк", "форм", "клик", "экран", "меню", "page.", "snapshot"),
    "api": ("api", "апи", "эндпоинт", "endpoint", "rest", "odata", "http", "/api/"),
    "db": ("бд", "база", "базе", "базу", "sql", "postgres", "select ", " db"),
    "dq_reports": ("отчёт", "отчет", "excel", "xlsx", "dq"),
}
# Файлы корня harness, без которых тест не запустится: настройки pytest, окружение, общий conftest.
ROOT_FILES = ("conftest.py", "pytest.ini", ".env.example")
CONTEXT_CHARS = 6000
MAX_ROUNDS = 3

# ---------- harness: скачать или взять копию ----------


def harness_root(source: str) -> tuple[Path, str]:
    """(папка harness, откуда взят). source — адрес zip-архива или папка на диске."""
    local = Path(source).expanduser()
    if not source.startswith(("http://", "https://")):
        local = local if local.is_absolute() else ms.project() / local
        if not (local / "AGENTS.md").is_file():
            raise ValueError(f"В папке {local} нет AGENTS.md — это не qa_harness")
        return local, str(local)
    cache = Path(os.environ.get("MEEPO_STATE_DIR") or ms.project() / ".meepo") / "harness"
    stamp = cache / ".fetched"
    fresh = stamp.is_file() and stamp.read_text(encoding="utf-8") == source \
        and time.time() - stamp.stat().st_mtime < REFRESH_SECONDS
    if fresh:
        return cache, f"{source} (копия от {time.strftime('%d.%m %H:%M', time.localtime(stamp.stat().st_mtime))})"
    ms.progress("Скачиваю qa_harness…")
    try:
        with urllib.request.urlopen(source, timeout=DOWNLOAD_TIMEOUT) as response:
            data = response.read()
        unpack(data, cache)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        if (cache / "AGENTS.md").is_file():
            return cache, f"{source} — не скачался ({type(exc).__name__}), взята прежняя копия"
        raise ValueError(f"Не удалось скачать qa_harness ({exc}). Укажите harness — папку с ним на диске") from None
    stamp.write_text(source, encoding="utf-8")
    return cache, source


def unpack(data: bytes, target: Path) -> None:
    """Архив GitHub/GitLab — одна верхняя папка; её содержимое кладётся в target целиком, разом."""
    staging = target.with_name(target.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            parts = Path(item.filename).parts[1:]
            if item.is_dir() or not parts or ".." in parts:
                continue
            path = staging.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(item))
    if not (staging / "AGENTS.md").is_file():
        raise ValueError("в архиве нет AGENTS.md — это не qa_harness")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)


# ---------- слой и что взять из harness ----------


def layers(harness: Path) -> list[str]:
    folders = (harness / "examples").iterdir()
    return sorted(path.name for path in folders if path.is_dir() and not path.name.startswith(("_", ".")))


def pick_layer(harness: Path, task: str, details: str, wanted: str) -> str:
    known = layers(harness)
    wanted = {"dq": "dq_reports"}.get(wanted, wanted)
    if wanted:
        if wanted not in known:
            raise ValueError(f"layer — один из: {', '.join(known)}")
        return wanted
    if re.search(r"page\.(get_by_\w+|locator)\(", details) and "ui" in known:
        return "ui"
    words = f"{task}\n{details}".lower()
    scores = {name: sum(word in words for word in KEYWORDS.get(name, (name,))) for name in known}
    best = max(scores.values())
    if best and list(scores.values()).count(best) == 1:
        return max(scores, key=scores.get)
    answer = ms.ask_json("Определи слой автотеста по задаче.", f"Задача: {task}\nСлои: {', '.join(known)}",
                         {"type": "object", "properties": {"layer": {"type": "string", "enum": known}}, "required": ["layer"]})
    return answer["layer"]


def modules(harness: Path) -> dict[str, Path]:
    """Модули ядра для import: config, report, clients.http, checks.base… (без examples/, tests/ и conftest)."""
    found = {}
    for path in harness.rglob("*.py"):
        relative = path.relative_to(harness)
        if relative.parts[0] not in ("examples", "tests") and path.name != "conftest.py":
            found[".".join(relative.with_suffix("").parts)] = path
    return found


def imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names |= {node.module, *(f"{node.module}.{alias.name}" for alias in node.names)}
    return names


def closure(harness: Path, start: list[Path]) -> list[Path]:
    """Файлы start и всё, что они импортируют из ядра и examples/, — рекурсивно."""
    core = modules(harness)
    seen: list[Path] = []
    queue = list(start)
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.append(path)
        for name in imports(path):
            target = core.get(name) or harness.joinpath(*name.split(".")).with_suffix(".py")
            if name.split(".")[0] in ("examples", *{key.split(".")[0] for key in core}) and target.is_file():
                queue.append(target)
    return sorted(seen)


# ---------- обвязка в проекте: недостающее берётся из harness ----------


def project_path(relative: str, folder: str) -> str:
    """examples/ui/conftest.py → tests/ui/conftest.py; файлы ядра — на тех же местах."""
    return folder + relative[len("examples"):] if relative.startswith("examples/") else relative


def rename(text: str, folder: str) -> str:
    """Импорты и пути примеров (examples.ui…, examples/ui/…) — на папку тестов проекта."""
    text = re.sub(r"\bexamples\.(?=\w)", folder.replace("/", ".") + ".", text)
    return re.sub(r"\bexamples/", folder + "/", text)


def setup(harness: Path, layer: str, folder: str) -> list[str]:
    """Копирует в проект недостающую обвязку слоя; существующие файлы проекта не трогает. Возвращает скопированное."""
    root = ms.project()
    conftests = [harness / "conftest.py", harness / "examples" / "conftest.py", harness / "examples" / layer / "conftest.py"]
    tests = sorted((harness / "examples" / layer).rglob("test_*.py"))
    needed = closure(harness, conftests + tests)
    # Из examples/ копируется только то, что нужно conftest'ам (страница входа); сами примеры — нет.
    wanted_examples = set(closure(harness, conftests))
    files = [path for path in needed if path.relative_to(harness).parts[0] != "examples" or path in wanted_examples]
    files += [harness / name for name in ROOT_FILES] + sorted((harness / "requirements").glob("*.txt"))
    copied = []
    for source in dict.fromkeys(files):
        if not source.is_file():
            continue
        relative = project_path(source.relative_to(harness).as_posix(), folder)
        target = root / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rename(source.read_text(encoding="utf-8"), folder), encoding="utf-8")
        copied.append(relative)
    return copied


# ---------- запрос к модели ----------


def api_summary(path: Path) -> str:
    """Сводка модуля ядра: docstring, классы и сигнатуры с первой строкой описания — чтобы звать, а не переписывать."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = [(ast.get_docstring(tree) or "").split("\n\n")[0]]
    for node in tree.body:
        items = node.body if isinstance(node, ast.ClassDef) else [node]
        if isinstance(node, ast.ClassDef):
            lines.append(f"class {node.name}({', '.join(ast.unparse(base) for base in node.bases)}):")
        for item in items:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_"):
                doc = (ast.get_docstring(item) or "").split("\n")[0]
                indent = "    " if isinstance(node, ast.ClassDef) else ""
                lines.append(f"{indent}def {item.name}({ast.unparse(item.args)})" + (f"  # {doc}" if doc else ""))
    return "\n".join(line for line in lines if line)


def system_prompt(harness: Path, layer: str, folder: str) -> str:
    examples = sorted((harness / "examples" / layer).rglob("*.py"))
    used = [path for path in closure(harness, examples) if path.relative_to(harness).parts[0] != "examples"]
    shown = "\n\n".join(f"#### {project_path(path.relative_to(harness).as_posix(), folder)}\n```python\n"
                        f"{rename(path.read_text(encoding='utf-8'), folder)}\n```" for path in examples)
    api = "\n\n".join(f"#### {path.relative_to(harness).as_posix()}\n```python\n{api_summary(path)}\n```" for path in used)
    pages = f" Страницы Page Object — в {folder}/{layer}/pages/<страница>_page.py." if layer == "ui" else ""
    locator_rules = UI_RULES if layer == "ui" else ""
    return f"""Ты — QA-автоматизатор. Напиши автотест слоя {layer} строго по правилам репозитория qa_harness.

Правила (AGENTS.md — главное, соблюдай каждое):
{(harness / "AGENTS.md").read_text(encoding="utf-8")}

Как писать в этом проекте:
- Папка примеров examples/ здесь называется {folder}/: файлы — в {folder}/{layer}/,
  импорты — {folder.replace("/", ".")}.{layer}….{pages}
- Обвязка (config.py, report.py, clients/, checks/, conftest.py) уже есть: импортируй её, не переписывай и не копируй.
- Пиши только новые файлы. Существующие страницы можно импортировать; не хватает метода — сделай новую страницу.
- Не выдумывай локаторы, эндпоинты, колонки и SQL: бери их из задачи и данных.
  Чего нет — константа со значением "TODO" и пункт в NOTES.{locator_rules}
- Каждый тест проверяет поведение. Подготовка данных — в фикстуре с yield (как create_order), а не отдельным тестом.
- Вызывай только методы, которые есть в API ядра ниже; модели для check_schema — классы pydantic BaseModel.
- Комментарии, заголовки шагов и @allure.title — по-русски.

Формат ответа — только файлы и заметки:
### FILE: <путь от корня проекта>
```python
<полный текст файла>
```
### NOTES
- что заполнить в .env, какие сделаны допущения, вопросы

Примеры слоя {layer} из qa_harness (оформление копируй; в проекте этих файлов нет):
{shown}

API ядра harness (сигнатуры):
{api}"""


def project_context(folder: str, layer: str) -> str:
    """Что уже есть в папке слоя проекта: список файлов, conftest'ы и страницы целиком (с ограничением)."""
    root = ms.project()
    base = root / folder / layer
    files = sorted(base.rglob("*.py")) if base.is_dir() else []
    lines = [f"Файлы в {folder}/{layer}/: " + (", ".join(path.relative_to(root).as_posix() for path in files) or "пока нет")]
    for path in [root / folder / "conftest.py", *(path for path in files if path.name == "conftest.py" or "pages" in path.parts)]:
        if path.is_file():
            text = path.read_text(encoding="utf-8")[:CONTEXT_CHARS]
            lines.append(f"#### {path.relative_to(root).as_posix()}\n```python\n{text}\n```")
    return "\n\n".join(dict.fromkeys(lines))


# ---------- UI: локаторы из snapshot → константы страницы ----------

UI_RULES = """
- Локаторы страницы — ТОЛЬКО константы из блока «Локаторы страницы» в задаче: скопируй блок в начало модуля
  страницы как есть (имена, значения и комментарии) и используй: self.element("Кнопка «Войти»", LOGIN_BTN).
  Своих селекторов (XPath, CSS, «link: Заказы») не придумывай; нужного элемента нет — константа "TODO".
- Каждый тест открывает страницу сам: page и auth_page приходят пустыми. Первый шаг теста — <страница>.open()
  (или open("/путь")), потом действия и проверки."""
# Вызов локатора в данных: page.get_by_role("link", name="Заказы"), page.locator("#id"), ….nth(2).
LOCATOR_CALL = re.compile(r"page\.((?:get_by_\w+|locator)\((?:[^()]|\([^()]*\))*\)(?:\.nth\(\d+\))?)")
# Строка-селектор в коде страницы: XPath, CSS (#id, .class, [attr]), селекторы Playwright или выдуманное «роль: имя».
SELECTOR = re.compile(r"^(//|\(//|#|\.[a-zA-Z]|\[|internal:|role=|text=|[a-z]+:\s)")
# Имя константы: вид элемента + название латиницей (LINK_ZAKAZY).
KINDS = {"link": "LINK", "button": "BTN", "heading": "TITLE", "textbox": "FIELD", "columnheader": "COLUMN",
         "checkbox": "CHECKBOX", "combobox": "SELECT", "row": "ROW", "cell": "CELL", "tab": "TAB", "menuitem": "MENU"}
TRANSLIT = str.maketrans({**dict(zip("абвгдезийклмнопрстуфхыэ", "abvgdeziyklmnoprstufhye")), "ё": "e", "ж": "zh", "ц": "ts",
                          "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "ъ": "", "ь": ""})


def to_selector(call: str) -> tuple[str, str, str] | None:
    """page.get_by_role("link", name="Заказы") → (вид, название, 'internal:role=link[name="Заказы"i]') —
    та же строка, что строит сам Playwright; None — вызов не разобрать."""
    try:
        node = ast.parse("page." + call, mode="eval").body
        nth = None
        if node.func.attr == "nth":
            nth, node = node.args[0].value, node.func.value
        method, args = node.func.attr, [arg.value for arg in node.args]
        options = {item.arg: item.value.value for item in node.keywords}
    except (SyntaxError, AttributeError, IndexError):
        return None
    flag = "s" if options.get("exact") else "i"
    quoted = json.dumps(args[0], ensure_ascii=False) if args else ""
    if method == "get_by_role":
        name = options.get("name", "")
        selector = f"internal:role={args[0]}" + (f"[name={json.dumps(name, ensure_ascii=False)}{flag}]" if name else "")
        kind, title = KINDS.get(args[0], args[0].upper()), name
    elif method in ("get_by_label", "get_by_placeholder", "get_by_text", "get_by_test_id", "locator") and args:
        selector = {"get_by_label": f"internal:label={quoted}{flag}",
                    "get_by_placeholder": f"internal:attr=[placeholder={quoted}{flag}]",
                    "get_by_text": f"internal:text={quoted}{flag}",
                    "get_by_test_id": f"internal:testid=[data-testid={quoted}s]",
                    "locator": args[0]}[method]
        kind = {"get_by_text": "TEXT", "get_by_label": "FIELD", "get_by_placeholder": "FIELD"}.get(method, "ELEMENT")
        # [data-testid=orders-table], #login → orders-table, login: имя константы без обёртки селектора.
        title = re.sub(r"^\W*(?:data-testid|id|name)?\W*", "", args[0]) if method == "locator" else args[0]
    else:
        return None
    return kind, title, selector + (f" >> nth={nth}" if nth is not None else "")


def locator_block(details: str) -> list[tuple[str, str, str]]:
    """Константы страницы из локаторов данных: (имя, селектор, исходный вызов)."""
    block, names = [], set()
    for call in dict.fromkeys(LOCATOR_CALL.findall(details)):
        parsed = to_selector(call)
        if parsed is None:
            continue
        kind, title, selector = parsed
        word = re.sub(r"[^A-Za-z0-9]+", "_", title.lower().translate(TRANSLIT)).strip("_").upper()[:30]
        name = f"{kind}_{word}".strip("_") or "ELEMENT"
        base, number = name, 2
        while name in names:
            name, number = f"{base}_{number}", number + 1
        names.add(name)
        block.append((name, selector, call))
    return block


def render_block(block: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"{name} = {selector!r}  # page.{call}" for name, selector, call in block)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("'", '"')


def ui_problems(files: dict[str, str], block: list[tuple[str, str, str]]) -> list[str]:
    """UI: константы локаторов — из блока, без своих селекторов; каждый тест начинается с open()."""
    found = []
    # Код без комментариев: локатор, упомянутый только в комментарии, не считается использованным.
    code = compact("\n".join(ast.unparse(ast.parse(text)) for text in files.values()))
    allowed = {compact(selector) for _, selector, _ in block}
    for name, selector, _ in block:
        if compact(repr(selector)) not in code:
            found.append(f"нет константы {name} = {selector!r} — скопируй блок «Локаторы страницы» как есть")
    for path, text in files.items():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SELECTOR.match(node.value.strip()) \
                    and compact(node.value) not in allowed:
                found.append(f"{path}: селектор «{node.value}» не из блока «Локаторы страницы» — своих не придумывай, иначе TODO")
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_") and Path(path).name.startswith("test_"):
                calls = [item.func.attr for item in ast.walk(node)
                         if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)]
                if "open" not in calls:
                    found.append(f"{path}: {node.name} не открывает страницу — первым шагом вызови <страница>.open()")
    return found


FILE = re.compile(r"^###\s*FILE:\s*`?([^\s`]+)`?\s*\n```[a-z]*\s*\n(.*?)\n```", re.M | re.S)
NOTES = re.compile(r"^###\s*NOTES\s*\n(.*)\Z", re.M | re.S)


def parse(answer: str) -> tuple[dict[str, str], str]:
    files = {path.strip().lstrip("./"): body.rstrip() + "\n" for path, body in FILE.findall(answer)}
    notes = NOTES.search(answer)
    return files, notes.group(1).strip() if notes else ""


# ---------- проверка, запись и сборка ----------


@dataclass
class Job:
    """Что пишем: папка тестов, слой, приставка имени теста, константы локаторов (UI), методы ядра harness."""

    folder: str
    layer: str
    prefix: str
    block: list
    methods: set[str]


def core_methods(harness: Path) -> set[str]:
    """Имена методов классов ядра (clients/, checks/): ими проверяется, что тест не зовёт выдуманное."""
    names = set()
    for path in modules(harness).values():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                names |= {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
    return names


def call_problems(path: str, tree: ast.AST, job: Job, defined: set[str]) -> list[str]:
    """Вызовы checker.* и клиентов *_api/*_db — только существующие методы; модели check_schema — BaseModel."""
    found = []
    models = {node.name: [ast.unparse(base) for base in node.bases] for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)):
            continue
        owner, method = node.func.value.id, node.func.attr
        if (owner == "checker" or owner.endswith(("_api", "_db"))) and method not in job.methods | defined:
            known = sorted(name for name in job.methods if name.startswith(("check_", "get_", "fetch", "execute")))
            found.append(f"{path}: метода {owner}.{method} нет в harness. Есть: {', '.join(known)}")
        if method == "check_schema" and len(node.args) > 1 and isinstance(node.args[1], ast.Name):
            bases = models.get(node.args[1].id)
            if bases is not None and "BaseModel" not in bases:
                found.append(f"{path}: {node.args[1].id} для check_schema — наследник pydantic BaseModel")
    return found


def problems(files: dict[str, str], job: Job, ours: set[str]) -> list[str]:
    """Нарушения правил AGENTS.md, которые видно без запуска."""
    root = ms.project()
    if not files:
        return ["Нет ни одного файла в формате ### FILE: путь + блок ```python"]
    found = []
    existing = {path.name for path in root.rglob("test_*.py") if ".venv" not in path.parts}
    for path, text in files.items():
        if not path.startswith(f"{job.folder}/{job.layer}/") or ".." in path.split("/") or not path.endswith(".py"):
            found.append(f"{path}: файлы — только .py внутри {job.folder}/{job.layer}/")
            continue
        if (root / path).exists() and path not in ours:
            found.append(f"{path}: такой файл уже есть — придумай новое имя")
        try:
            ast.parse(text)
        except SyntaxError as exc:
            found.append(f"{path}: синтаксическая ошибка, строка {exc.lineno}: {exc.msg}")
            continue
        if "@allure.step" in text:
            found.append(f"{path}: @allure.step запрещён — @step из report.py")
        name = Path(path).name
        if name.startswith("test_"):
            if not re.fullmatch(rf"test_{job.prefix}_\w+\.py", name):
                found.append(f"{path}: имя теста — test_{job.prefix}_<тема>.py")
            if name in existing and path not in ours:
                found.append(f"{path}: имя {name} уже занято в проекте — имена тестов уникальны")
            for mark in ("@allure.epic", "@allure.feature", "@allure.title", "class Test"):
                if mark not in text:
                    found.append(f"{path}: нет {mark} (метки Allure обязательны, тесты — в классе Test<Тема>)")
    if not found:
        trees = [ast.parse(text) for text in files.values()]
        defined = {node.name for tree in trees for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        for path, text in files.items():
            found += call_problems(path, ast.parse(text), job, defined)
    if job.layer == "ui" and not found:
        found += ui_problems(files, job.block)
    return found


def python_for(root: Path) -> Path:
    """Python проекта (.venv), иначе окружения скилла: там стоят пакеты из requires."""
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def collect(paths: list[str]) -> tuple[bool | None, str]:
    """(собралось ли: None — проверить нельзя из-за окружения, текст)."""
    root = ms.project()
    python = python_for(root)
    command = [str(python), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *paths]
    try:
        run = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"pytest не запустился: {exc}"
    output = (run.stdout + run.stderr).strip()
    if run.returncode == 0:
        count = re.search(r"(\d+) tests? collected", output)
        return True, f"собралось тестов: {count.group(1) if count else '?'} ({python.parent.parent.name}/python)"
    own = {path.split("/")[0] for path in paths} | {"config", "report", "clients", "checks"}
    missing = [name for name in re.findall(r"No module named '([\w.]+)'", output) if name.split(".")[0] not in own]
    if missing or "Не собираются слои" in output:
        return None, ("в окружении нет пакетов: " + ", ".join(sorted(set(missing))) if missing else
                      "слой не собирается без своих пакетов") + " — поставьте профиль из requirements/"
    return False, output[-3000:]


def write(files: dict[str, str]) -> None:
    root = ms.project()
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def generate(system: str, user: str, job: Job) -> tuple[dict[str, str], str, str]:
    """Пишет файлы, пока не пройдут правила и сборку (не больше MAX_ROUNDS запросов). (файлы, заметки, итог сборки)."""
    files: dict[str, str] = {}
    notes, verdict, request = "", "", user
    for round_number in range(1, MAX_ROUNDS + 1):
        ms.progress(f"Пишу тест: запрос {round_number} к модели")
        new, notes = parse(ms.ask_llm(system, request))
        found = problems(new, job, set(files))
        if found and files:
            # Исправление сломало правила: оставляем прежнюю версию файлов.
            break
        if found:
            request = user + "\n\nПрошлый ответ нарушил правила, исправь и пришли все файлы заново:\n- " + "\n- ".join(found)
            continue
        stale = set(files) - set(new)
        for path in stale:
            (ms.project() / path).unlink(missing_ok=True)
        files = new
        write(files)
        ms.progress("Проверяю сборку: pytest --collect-only")
        ok, verdict = collect(sorted(files))
        if ok is not False:
            return files, notes, verdict
        request = (user + "\n\nФайлы записаны, но pytest --collect-only упал. Исправь и пришли все файлы заново.\n"
                   + "\n".join(f"#### {path}\n```python\n{text}```" for path, text in files.items())
                   + f"\n\nВывод pytest:\n{verdict}")
    if not files:
        raise ValueError("Модель не написала тест по правилам:\n- " + "\n- ".join(found))
    return files, notes, verdict or "не проверена"


# ---------- запуск ----------


def main() -> int:
    arguments = ms.args()
    task = str(arguments.get("task") or "").strip()
    if not task:
        print("Нужен task — что проверяет автотест")
        return 2
    folder = str(arguments.get("dir") or "tests").strip().strip("/") or "tests"
    details = str(arguments.get("details") or "")
    try:
        ms.inside(folder, "dir")
        harness, source = harness_root(str(arguments.get("harness") or DEFAULT_HARNESS))
        layer = pick_layer(harness, task, details, str(arguments.get("layer") or "").strip().lower())
    except ValueError as exc:
        print(exc)
        return 2
    examples = sorted((harness / "examples" / layer).rglob("test_*.py"))
    prefix = next((match.group(1) for path in examples if (match := re.match(r"test_([a-z]+)_", path.name))), layer)
    copied = setup(harness, layer, folder)
    job = Job(folder, layer, prefix, locator_block(details) if layer == "ui" else [], core_methods(harness))
    user = f"Задача: {task}\n\nДанные для теста (локаторы, эндпоинт, SQL, колонки):\n{details or 'не даны'}"
    if job.block:
        user += f"\n\nЛокаторы страницы (скопируй в модуль страницы как есть):\n```python\n{render_block(job.block)}\n```"
    user += f"\n\nЧто уже есть в проекте:\n{project_context(folder, layer)}"
    files, notes, verdict = generate(system_prompt(harness, layer, folder), user, job)
    lines = [f"Слой: {layer}. Правила и примеры: qa_harness — {source}.", "Записано:", *(f"- {path}" for path in files)]
    if copied:
        lines += ["Взято из harness (в проекте не было):", *(f"- {path}" for path in copied)]
    lines.append(f"Сборка (pytest --collect-only): {verdict[-1500:]}")
    if notes:
        lines += ["Заметки:", notes]
    test = next((path for path in files if Path(path).name.startswith("test_")), "")
    lines.append(f"\nДальше: заполнить .env по .env.example, запуск — python -m pytest {test}"
                 + (" --headed" if layer == "ui" else "") + " (через run_command, с согласия пользователя).")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ms.Unavailable as exc:
        print(f"Модель недоступна: {exc}")
        sys.exit(1)
    except (ms.ModelError, ValueError) as exc:
        print(str(exc))
        sys.exit(1)
