"""Каталог возможностей: скиллы и инструменты — создание, правка, архив, заготовки и список для UI.

Всё рабочее — файлы на диске (data/skills, data/tools), которые можно править руками; реестры в памяти обновляются
сразу, без перезапуска. Готовые заготовки лежат в коде рядом со своим видом (skills/library, connectors/library):
«включить» копирует заготовку в data/ — дальше это обычная возможность.
"""

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from local_agent.connectors import scripts
from local_agent.connectors.pyenv import PythonEnv
from local_agent.connectors.secrets import PUBLIC_SUFFIXES, SecretStore
from local_agent.core.registry import ToolRegistry
from local_agent.core.tool import Tool, summary_line
from local_agent.skills import files as skill_files
from local_agent.skills.models import Skill
from local_agent.skills.registry import SkillRegistry
from local_agent.steps.engine import Toolkit, problems

KINDS = ("tool", "skill")
# Шаблоны для «＋ Новый скилл» и для шагов скилла: с ними видно, как устроен файл.
SKILL_TEMPLATE = """---
name: Название скилла
description: Когда он нужен — одной строкой: по ней агент выбирает скилл
---
1. Что сделать первым делом.
2. Как проверить результат.
3. Как оформить ответ.
"""
STEPS_TEMPLATE = """Пара слов о том, что получится в итоге.

## Нужно от вас
- папка — где лежит документация
- заметки — что учесть (необязательно)

## Прочитать и выписать главное
модель: Прочитай документацию и выпиши, что в ней описано: функции, ограничения, ошибки.
файлы: {папка}
сохранить: .meepo/summary.md

## Сделать результат
модель: По выписке составь результат. Учти: {заметки}
файлы: .meepo/summary.md
сохранить: result.md
"""
TOOL_FILES = ("tool.md", "parameters.json", "script.py")


@dataclass(frozen=True)
class CapabilityPaths:
    tools: Path
    skills: Path


class CapabilityService:
    def __init__(
        self, tools: ToolRegistry, skills: SkillRegistry, paths: CapabilityPaths,
        env: PythonEnv | None = None, secrets: SecretStore | None = None,
    ) -> None:
        self.tools = tools
        self.skills = skills
        self.paths = paths
        # Общее окружение песочницы: в нём пакеты, которые пользователь разрешил поставить скриптам.
        self.env = env
        self.secrets = secrets

    def load(self) -> None:
        """Инструменты-скрипты подхватываются при старте."""
        for tool in scripts.load_script_tools(self.paths.tools):
            self.tools.replace(self._bind(tool))

    # ---------- проверки ----------

    def exists(self, kind: str, item_id: str) -> bool:
        if kind == "tool":
            return self.tools.get(item_id) is not None
        return self.skills.get(item_id) is not None

    def toolkit(self, read_only: bool = False) -> Toolkit:
        """Чем пользуются шаги скиллов: все инструменты реестра и окружение песочницы для скриптов."""
        return Toolkit(tool=self.tools.get, tools=self.tools.all, read_only=read_only, env=self.env)

    def is_script_tool(self, tool_id: str) -> bool:
        return isinstance(self.tools.get(tool_id), scripts.ScriptTool)

    def archived(self, kind: str, item_id: str) -> str | None:
        """Самая свежая запись архива для id, если есть."""
        entries = [archive_id for archive_id, archived_id, _ in self._archive(kind) if archived_id == item_id]
        return entries[-1] if entries else None

    # ---------- изменения ----------

    def save_tool(self, tool_id: str, **fields) -> scripts.ScriptTool:
        tool = self._bind(scripts.write_script_tool(self.paths.tools, tool_id, **fields))
        self.tools.replace(tool)
        return tool

    def _bind(self, tool: scripts.ScriptTool) -> scripts.ScriptTool:
        tool.env = self.env
        tool.secret_store = self.secrets
        return tool

    def save_skill(
        self, skill_id: str, name: str, description: str, instructions: str,
        steps: str | None = None, scripts_code: dict[str, str] | None = None,
    ) -> Skill:
        skill = skill_files.write_skill(self.paths.skills, skill_id, name, description, instructions, steps, scripts_code)
        self.skills.replace(skill)
        return skill

    def create(self, kind: str, item_id: str, text: str) -> str:
        """«＋ Новый скилл»: папка с SKILL.md из текста; занятое имя не перезаписывается."""
        if kind != "skill":
            raise ValueError("Так создаются только скиллы")
        if not skill_files.SKILL_ID.fullmatch(item_id):
            raise ValueError("id — строчные латинские буквы, цифры, - и _, например test-cases")
        if self.exists(kind, item_id) or self.archived(kind, item_id) is not None:
            raise FileExistsError(f"{item_id} уже есть (или лежит в архиве) — выберите другое имя")
        self.skills.replace(skill_files.save_files(self.paths.skills, item_id, {skill_files.SKILL_FILE: text}, create=True))
        return item_id

    @staticmethod
    def templates() -> dict[str, str]:
        return {"skill": SKILL_TEMPLATE, "steps": STEPS_TEMPLATE}

    # ---------- готовые заготовки ----------

    def library(self) -> list[dict]:
        """Готовые, но не включённые: скиллы из skills/library и коннекторы из connectors/library."""
        items = [
            {"kind": "skill", **self._skill_view(skill, ready=True)}
            for skill in skill_files.library_skills() if self.skills.get(skill.id) is None
        ]
        items += [
            {"kind": "tool", "id": tool.id, "name": tool.name, "description": summary_line(tool.description, 200)}
            for tool in scripts.library_tools() if self.tools.get(tool.id) is None
        ]
        return items

    def in_library(self, kind: str, item_id: str) -> bool:
        return any(item["kind"] == kind and item["id"] == item_id for item in self.library())

    def enable(self, kind: str, item_id: str) -> str:
        """Включает готовую заготовку: её папка копируется в data/ и дальше правится как своя."""
        if not self.in_library(kind, item_id):
            raise FileNotFoundError("Такой готовой заготовки нет или она уже включена")
        library, root = (skill_files.LIBRARY, self.paths.skills) if kind == "skill" else (scripts.LIBRARY, self.paths.tools)
        target = root / item_id
        if target.exists():
            raise FileExistsError("Возможность с таким id уже есть")
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(library / item_id, target, ignore=shutil.ignore_patterns("__pycache__"))
        if kind == "skill":
            self.skills.replace(skill_files.read_skill(target))
        else:
            self.tools.replace(self._bind(scripts.read_script_tool(target)))
        return item_id

    # ---------- архив ----------

    def archive(self, kind: str, item_id: str) -> None:
        if kind == "tool":
            if not self.is_script_tool(item_id):
                raise ValueError("В архив можно убрать только инструмент-скрипт: встроенные и MCP идут с приложением")
            scripts.archive_script_tool(self.paths.tools, item_id)
            self.tools.unregister(item_id)
            return
        if self.skills.get(item_id) is None:
            raise FileNotFoundError("Скилла нет")
        skill_files.archive_skill(self.paths.skills, item_id)
        self.skills.remove(item_id)

    def restore(self, kind: str, archive_id: str) -> str:
        if kind == "tool":
            tool = self._bind(scripts.restore_script_tool(self.paths.tools, archive_id))
            self.tools.replace(tool)
            return tool.id
        skill = skill_files.restore_skill(self.paths.skills, archive_id)
        self.skills.replace(skill)
        return skill.id

    def purge(self, kind: str, archive_id: str) -> None:
        """Окончательно удаляет запись архива: вернуть её будет нельзя."""
        if not any(entry == archive_id for entry, _, _ in self._archive(kind)):
            raise FileNotFoundError("Записи архива нет")
        root = self.paths.tools if kind == "tool" else self.paths.skills
        shutil.rmtree(root / ".deleted" / archive_id)

    # ---------- файлы ----------

    def source(self, kind: str, item_id: str) -> tuple[Path, dict[str, str]]:
        """Папка возможности и её файлы как есть: их можно открыть в редакторе и поправить руками."""
        if kind == "tool" and self.is_script_tool(item_id):
            folder = self.paths.tools / item_id
            return folder, {name: (folder / name).read_text(encoding="utf-8") for name in TOOL_FILES}
        if kind == "skill" and (skill := self.skills.get(item_id)) is not None and skill.folder is not None:
            return skill.folder, skill_files.files_of(skill.folder)
        raise FileNotFoundError("Возможность не найдена или встроена в приложение")

    def save_source(self, kind: str, item_id: str, files: dict[str, str | None]) -> None:
        """Правка из окна просмотра: файлы сначала проверяются копией, на диск попадают, только если они рабочие."""
        if kind == "tool":
            self._save_tool_source(item_id, files)
            return
        if kind != "skill" or not self.exists(kind, item_id):
            raise FileNotFoundError("Возможность не найдена или встроена в приложение")
        self.skills.replace(skill_files.save_files(self.paths.skills, item_id, files))

    def _save_tool_source(self, tool_id: str, files: dict[str, str | None]) -> None:
        if not self.is_script_tool(tool_id):
            raise FileNotFoundError("Править можно только инструмент-скрипт")
        if not files or not set(files) <= set(TOOL_FILES) or any(text is None for text in files.values()):
            raise ValueError(f"Файлы инструмента: {', '.join(TOOL_FILES)}")
        if "script.py" in files:
            try:
                compile(files["script.py"], "script.py", "exec")
            except SyntaxError as exc:
                raise ValueError(f"script.py, строка {exc.lineno}: {exc.msg}") from None
        folder = self.paths.tools / tool_id
        with tempfile.TemporaryDirectory(dir=self.paths.tools, prefix=".edit-") as scratch:
            draft = Path(scratch) / tool_id
            shutil.copytree(folder, draft)
            for name, text in files.items():
                (draft / name).write_text(text, encoding="utf-8", newline="\n")
            try:
                scripts.read_script_tool(draft, tool_id)
            except (OSError, ValueError) as exc:
                raise ValueError(str(exc).replace(str(draft), str(folder))) from None
            for name in files:
                (draft / name).replace(folder / name)
        self.tools.replace(self._bind(scripts.read_script_tool(folder, tool_id)))

    # ---------- обзор для UI ----------

    def overview(self) -> dict:
        tools = [
            {
                "id": tool.id, "name": tool.name, "description": summary_line(tool.description, 200),
                "origin": self._origin(tool),
                "requires_approval": tool.requires_approval,
                "path": str(tool.folder) if isinstance(tool, scripts.ScriptTool) else None,
                **(self._connector(tool) if self._origin(tool) == "connector" else {}),
            }
            for tool in self.tools.all()
        ]
        archive = [
            {"kind": kind, "archive_id": archive_id, "id": item_id, "name": name}
            for kind in KINDS for archive_id, item_id, name in self._archive(kind)
        ]
        packages = self.env.packages() if self.env else {}
        # Пакет нужен инструменту, если он в его requires или в их зависимостях: иначе у зависимостей
        # markitdown стояло бы «не используется» с кнопкой «удалить».
        users: dict[str, list[str]] = {name: [] for name in packages}
        for tool in self.tools.all():
            if isinstance(tool, scripts.ScriptTool) and tool.requires and self.env:
                for name in self.env.needed(list(tool.requires)) & set(users):
                    users[name].append(tool.id)
        python = {
            "path": str(self.env.path) if self.env else None,
            "packages": [{"name": name, "version": version, "tools": users[name]} for name, version in packages.items()],
        }
        return {
            "skills": [self._skill_view(skill) for skill in self.skills.all()], "tools": tools, "archive": archive,
            "library": self.library(), "python": python,
        }

    def _skill_view(self, skill: Skill, ready: bool = False) -> dict:
        view = {
            "id": skill.id, "name": skill.name, "description": skill.description,
            "path": str(skill.folder) if skill.folder else None,
            "files": list(skill_files.files_of(skill.folder)) if skill.folder else [],
            "steps": skill.steps.view() if skill.steps else None,
        }
        if skill.steps:
            # Заготовку не проверяем: она ещё не включена, проверять её шагам нечего.
            view |= {"effects": skill.steps.effects(), "problems": [] if ready else problems(skill, self.toolkit())}
        return view

    def _origin(self, tool: Tool) -> str:
        """Три группы инструментов: встроенные, коннекторы (скрипты из data/tools) и MCP."""
        if isinstance(tool, scripts.ScriptTool):
            return "connector"
        return "mcp" if getattr(tool, "server", None) else "builtin"

    def _connector(self, tool: scripts.ScriptTool) -> dict:
        """Поля формы «настроить»: какие доступы нужны и заданы ли (у скрипта без доступов полей нет).
        Адрес и логин не тайна — их значения видны."""
        stored = self.secrets.pick([name.removesuffix("?") for name in tool.secrets]) if self.secrets else {}
        fields = []
        for raw in tool.secrets:
            name = raw.removesuffix("?")
            field = {"name": name, "optional": raw.endswith("?"), "set": name in stored}
            if name.endswith(PUBLIC_SUFFIXES + ("_VERIFY_SSL",)) and name in stored:
                field["value"] = stored[name]
            fields.append(field)
        required = [field for field in fields if not field["optional"]]
        return {
            "fields": fields, "configured": all(field["set"] for field in required),
            # Пакеты из requires, которых нет в песочнице: строка коннектора предлагает их установить.
            "missing": self.env.missing(list(tool.requires)) if self.env else list(tool.requires),
            # Полное описание: в списке — первая фраза, в форме настройки — что коннектор умеет целиком.
            "full_description": tool.description,
            "about": tool.reason,
            # Кнопка «Проверить подключение» — если скрипт понимает action check.
            "checkable": 'action == "check"' in tool.script.read_text(encoding="utf-8"),
        }

    def archive_lines(self) -> list[str]:
        """Архив и готовые заготовки одной строкой на элемент: агент может предложить вернуть или включить нужное."""
        names = {"tool": "инструмент", "skill": "скилл"}
        lines = [f"- {names[item['kind']]} {item['id']} — {summary_line(item['description'] or item['name'])} (готовый)"
                 for item in self.library()]
        for kind in KINDS:
            seen = set()
            for _archive_id, item_id, name in reversed(self._archive(kind)):
                if item_id in seen or self.exists(kind, item_id):
                    continue
                seen.add(item_id)
                lines.append(f"- {names[kind]} {item_id} — {summary_line(name)}")
        return lines

    def _archive(self, kind: str) -> list[tuple[str, str, str]]:
        if kind == "tool":
            return [
                (archive_id, item_id, item_id) for archive_id, item_id in scripts.archived_script_tools(self.paths.tools)
            ]
        return [(archive_id, skill.id, skill.name) for archive_id, skill in skill_files.archived_skills(self.paths.skills)]
