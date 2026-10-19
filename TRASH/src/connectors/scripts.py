"""Инструменты-скрипты: папка data/tools/<id>/ — их пишет агент или они включены из library/.

- tool.md — заголовок (name, description, requires_approval, network, timeout, requires, secrets), затем:
  текст для людей — что это и зачем; «## Параметры» — список «- имя — что это» с пометками «(число)»
  и «(необязательно)», из него строится схема вызова; «## Подсказка модели» — как пользоваться инструментом
  (шаблоны запросов, что делать при ошибке): модель получает её вместе со схемой.
  requires — пакеты PyPI через запятую из окружения песочницы; secrets — имена секретов через запятую:
  скрипт получит их значения переменными окружения (JIRA_EMAIL? — необязательный: без него инструмент
  запускается, переменной просто нет);
- script.py — сам скрипт: аргументы приходят JSON-объектом в stdin, результат — в stdout.

Архив — data/tools/.deleted/<id>.<uuid>/, как у навыков и агентов.
"""

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from connectors.pyenv import PythonEnv, check_requirements
from connectors.sandbox import DEFAULT_TIMEOUT_SECONDS, SandboxPolicy, run_script
from connectors.secrets import SecretStore, check_secret_names
from core.tool import ToolResult
from shared.params import parameter_schema, parse_items

logger = logging.getLogger(__name__)

# Готовые коннекторы рядом с кодом: connectors/library/<id>/ — те же три файла.
LIBRARY = Path(__file__).resolve().parent / "library"
# Где пользователь задаёт доступы инструмента.
WHERE = "«Возможности → Инструменты → Коннекторы → настроить»"
TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
ARCHIVED = re.compile(r"(?P<id>[a-z][a-z0-9_]{2,40})\.[0-9a-f]{32}")
TOOL_KEYS = {"name", "description", "requires_approval", "network", "timeout", "requires", "secrets"}
MAX_TIMEOUT = 300
PARAMETERS_SECTION = "Параметры"
HINT_SECTION = "Подсказка модели"
# Подсказка уходит модели с каждым запросом, пока инструмент подключён: длинная съела бы окно.
MAX_HINT_CHARS = 1500
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass
class ScriptTool:
    id: str
    name: str
    description: str
    parameters: dict
    requires_approval: bool
    network: bool
    timeout: int
    folder: Path
    # Текст для людей: что это и зачем — в форме настройки и карточке.
    reason: str = ""
    # Подсказка модели: как пользоваться инструментом; модель получает её вместе со схемой.
    hint: str = ""
    requires: tuple[str, ...] = ()
    # Окружение песочницы задаёт CapabilityService; без него скрипт видит только стандартную библиотеку.
    env: PythonEnv | None = None
    secrets: tuple[str, ...] = ()
    # Хранилище секретов задаёт CapabilityService; значения в модель не попадают — их видит только скрипт.
    secret_store: SecretStore | None = None

    @property
    def script(self) -> Path:
        return self.folder / "script.py"

    async def execute(self, arguments: dict[str, object], workspace: Path | None) -> ToolResult:
        policy = SandboxPolicy(self.timeout, self.network, writable_workspace=self.requires_approval)
        if self.requires:
            missing = self.env.missing(list(self.requires)) if self.env else list(self.requires)
            if missing:
                return ToolResult(
                    f"Инструменту {self.id} не хватает пакетов: {', '.join(missing)}. Попроси пользователя "
                    "установить их через install_packages и вызови инструмент снова.", is_error=True,
                )
        names = [name.removesuffix("?") for name in self.secrets]
        values = self.secret_store.pick(names) if self.secret_store else {}
        if absent := [name for name in self.secrets if not name.endswith("?") and name not in values]:
            return ToolResult(
                f"Инструмент {self.id} не настроен: нет {', '.join(absent)}. Попроси пользователя заполнить форму "
                f"{WHERE}; адрес, логин и токен в чат не пиши и не проси.", is_error=True,
            )
        python = self.env.python if self.env and self.env.exists else None
        result = await run_script(self.script, arguments, workspace, policy, python, values)
        if self.secret_store and values:
            # Скрипт мог напечатать токен: модель его не увидит.
            result = ToolResult(self.secret_store.mask(result.content), result.is_error)
        return result


def library_tools() -> list[ScriptTool]:
    """Готовые коннекторы из connectors/library: включаются кнопкой «включить» — папка копируется в data/tools."""
    return load_script_tools(LIBRARY) if LIBRARY.is_dir() else []


def load_script_tools(root: Path) -> list[ScriptTool]:
    """Битая папка инструмента не роняет приложение: она пропускается с предупреждением в логе."""
    root.mkdir(parents=True, exist_ok=True)
    tools = []
    for folder in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
        try:
            tools.append(read_script_tool(folder))
        except (OSError, ValueError) as exc:
            logger.warning("Инструмент %s не загружен: %s", folder.name, exc)
    return tools


def read_script_tool(folder: Path, tool_id: str | None = None) -> ScriptTool:
    tool_id = tool_id or folder.name
    if not TOOL_ID.fullmatch(tool_id):
        raise ValueError("id инструмента — латиница в нижнем регистре, цифры и _, от 3 символов")
    text = (folder / "tool.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n") or "\n---\n" not in text[3:]:
        raise ValueError("tool.md должен начинаться с блока --- параметры ---")
    header, body = text[4:].split("\n---\n", 1)
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or key.strip() not in TOOL_KEYS:
            raise ValueError(f"неизвестный параметр «{line.strip()}»")
        fields[key.strip()] = value.strip()
    if not fields.get("description"):
        raise ValueError("нет description")
    about, sections = _sections(body)
    parameters = _parameters(folder, sections.get(PARAMETERS_SECTION))
    hint = "\n".join(sections.get(HINT_SECTION, [])).strip()
    if len(hint) > MAX_HINT_CHARS:
        raise ValueError(f"«## {HINT_SECTION}» длиннее {MAX_HINT_CHARS} символов — сократите: она уходит модели с каждым запросом")
    if not (folder / "script.py").is_file():
        raise ValueError("нет script.py")
    timeout = int(fields.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    requires = _list(fields.get("requires", ""))
    if requires and (problem := check_requirements(requires)):
        raise ValueError(f"requires: {problem}")
    secrets = _list(fields.get("secrets", ""))
    if secrets and (problem := check_secret_names(secrets)):
        raise ValueError(f"secrets: {problem}")
    return ScriptTool(
        id=tool_id,
        name=fields.get("name") or tool_id,
        description=fields["description"],
        parameters=parameters,
        requires_approval=fields.get("requires_approval", "true").lower() != "false",
        network=fields.get("network", "false").lower() == "true",
        timeout=max(1, min(timeout, MAX_TIMEOUT)),
        folder=folder,
        reason=about,
        hint=hint,
        requires=tuple(requires),
        secrets=tuple(secrets),
    )


def _sections(body: str) -> tuple[str, dict[str, list[str]]]:
    """Текст для людей и разделы «## Параметры», «## Подсказка модели». Другие заголовки «## » — часть текста
    для людей; в блоках ``` заголовков нет."""
    about: list[str] = []
    sections: dict[str, list[str]] = {}
    current = about
    fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        title = line[3:].strip().casefold() if not fence and line.startswith("## ") else ""
        known = next((name for name in (PARAMETERS_SECTION, HINT_SECTION) if title == name.casefold()), None)
        if known is not None:
            if known in sections:
                raise ValueError(f"раздел «## {known}» указан дважды")
            current = sections[known] = []
        else:
            current.append(line)
    return "\n".join(about).strip(), sections


def _parameters(folder: Path, lines: list[str] | None) -> dict:
    """Схема вызова из «## Параметры»; без раздела — вызов без параметров."""
    if (folder / "parameters.json").exists():
        raise ValueError(f"parameters.json больше не читается — перенесите параметры в tool.md, раздел «## {PARAMETERS_SECTION}»")
    if lines is None:
        return dict(EMPTY_SCHEMA)
    return parameter_schema(parse_items(lines, "tool.md", PARAMETERS_SECTION), "tool.md")


def write_script_tool(
    root: Path, tool_id: str, *, name: str, description: str, reason: str, code: str,
    requires_approval: bool, network: bool, parameters: str = "", hint: str = "",
    timeout: int = DEFAULT_TIMEOUT_SECONDS, requires: list[str] | None = None, secrets: list[str] | None = None,
) -> ScriptTool:
    """Создаёт или перезаписывает папку инструмента: tool.md (с «## Параметры» и «## Подсказка модели») и script.py.
    Файлы пишутся во временную папку, проверяются и подменяют старую разом; старая уходит в архив."""
    if not TOOL_ID.fullmatch(tool_id):
        raise ValueError("Недопустимый id инструмента")
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{tool_id}.{uuid4().hex}"
    staging.mkdir()
    header = [
        f"name: {_one_line(name)}", f"description: {_one_line(description)}",
        f"requires_approval: {'true' if requires_approval else 'false'}",
        f"network: {'true' if network else 'false'}", f"timeout: {timeout}",
    ]
    if requires:
        header.append(f"requires: {', '.join(requires)}")
    if secrets:
        header.append(f"secrets: {', '.join(secrets)}")
    body = [reason.strip()]
    if parameters.strip():
        body.append(f"## {PARAMETERS_SECTION}\n{parameters.strip()}")
    if hint.strip():
        body.append(f"## {HINT_SECTION}\n{hint.strip()}")
    text = "---\n" + "\n".join(header) + "\n---\n" + "\n\n".join(part for part in body if part) + "\n"
    (staging / "tool.md").write_text(text, encoding="utf-8")
    (staging / "script.py").write_text(code.rstrip() + "\n", encoding="utf-8")
    try:
        read_script_tool(staging, tool_id)
    except (OSError, ValueError):
        shutil.rmtree(staging)
        raise
    target = root / tool_id
    if target.exists():
        archive_script_tool(root, tool_id)
    staging.rename(target)
    return read_script_tool(target)


def archive_script_tool(root: Path, tool_id: str) -> None:
    if not TOOL_ID.fullmatch(tool_id) or not (root / tool_id).is_dir():
        raise FileNotFoundError("Инструмент не найден")
    archive = root / ".deleted"
    archive.mkdir(exist_ok=True)
    (root / tool_id).rename(archive / f"{tool_id}.{uuid4().hex}")


def archived_script_tools(root: Path) -> list[tuple[str, str]]:
    """(id записи архива, id инструмента)."""
    archive = root / ".deleted"
    if not archive.is_dir():
        return []
    return [
        (path.name, match["id"]) for path in sorted(archive.iterdir())
        if path.is_dir() and (match := ARCHIVED.fullmatch(path.name))
    ]


def restore_script_tool(root: Path, archive_id: str) -> ScriptTool:
    match = ARCHIVED.fullmatch(archive_id)
    source = root / ".deleted" / archive_id
    if match is None or not source.is_dir():
        raise FileNotFoundError("Записи архива нет")
    target = root / match["id"]
    if target.exists():
        raise FileExistsError("Инструмент с таким id уже есть")
    source.rename(target)
    return read_script_tool(target)


def _list(value: str) -> list[str]:
    """Список через запятую; запятые в скобках — часть пакета: markitdown[pdf,docx] — один пакет."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [item.strip() for item in re.split(r",(?![^\[]*\])", value) if item.strip()]


def _one_line(value: str) -> str:
    return " ".join(value.split())
