"""Скиллы: read_skill (без карточки), create_skill и update_skill (группа skill_edit), run_skill (группа skill_run).

Сервисы — в ctx.services: registry (SkillRegistry), env (SkillsEnv), secrets (SecretStore), history (SkillHistory),
llm_info() — адрес, модель и ключ для MEEPO_LLM_*. Проверки идут до карточки: кривой вызов пользователь
не видит, его исправляет модель.
"""

import re
import sys

from meepo.agent.tools.base import Tool, ToolContext, ToolResult
from meepo.agent.tools.files import diff
from meepo.skills import runner
from meepo.skills.registry import check_files, parse_skill_md, set_header

# После стольких правок скилла подряд без удачного запуска агент останавливается и спрашивает пользователя.
MAX_FIXES = 3
SKILL_TEMPLATE = """---
name: имя-скилла
description: Что делает и когда применять — одной строкой.
requires: []
secrets: []
timeout: 600
---
# Что делает
...
# Аргументы (JSON)
- path — файл или папка в проекте (обязательно)
# Результат
...
# Примеры
{"path": "docs/auth.md"}"""


def _registry(ctx: ToolContext):
    return ctx.services["registry"]


def _skill(ctx: ToolContext, name: str):
    skill = _registry(ctx).get(name)
    if skill is None:
        names = ", ".join(item.name for item in _registry(ctx).list()) or "нет ни одного"
        raise ValueError(f"Скилла «{name}» нет. Есть: {names}")
    return skill



# ---------- read_skill ----------

def read_skill(args: dict, ctx: ToolContext) -> ToolResult:
    skill = _skill(ctx, args["name"])
    if file := str(args.get("file") or "").strip().lstrip("./"):
        if file not in skill.files():
            raise ValueError(f"В скилле {skill.name} нет файла {file}. Есть: {', '.join(skill.files())}")
        return ToolResult(f"{skill.name}/{file}:\n{(skill.folder / file).read_text(encoding='utf-8', errors='replace')}")
    missing = ctx.services["env"].missing(skill.requires)
    secrets = ctx.services["secrets"].status(skill.name, skill.secrets)
    lines = [f"Скилл {skill.name}" + (f" — СЛОМАН: {skill.error}" if skill.error else ""),
             f"Файлы: {', '.join(skill.files())}",
             f"Пакеты: {', '.join(skill.requires) or 'нет'}" + (f" (не установлены: {', '.join(missing)} — "
                                                                  "поставятся при запуске)" if missing else "")]
    if secrets:
        lines.append("Секреты: " + ", ".join(f"{name} — {'задан' if ok else 'НЕ ЗАДАН'}" for name, ok in secrets.items()))
    return ToolResult("\n".join(lines) + "\n\n" + skill.text)


# ---------- create_skill ----------

def _new_files(args: dict) -> dict[str, str]:
    """Все файлы нового скилла; requires и secrets в заголовке SKILL.md — из аргументов вызова."""
    files = {path.strip().lstrip("./"): content for path, content in args["files"].items()}
    try:
        skill_md = set_header(args["skill_md"], requires=args.get("requires", []), secrets=args.get("secrets", []))
    except ValueError as exc:
        raise ValueError(f"{exc}. Шаблон SKILL.md:\n{SKILL_TEMPLATE}") from None
    return {"SKILL.md": skill_md, **files}


def _check_new(args: dict, ctx: ToolContext) -> dict[str, str]:
    name = args["name"]
    if _registry(ctx).get(name) is not None:
        raise ValueError(f"Скилл {name} уже есть: прочитай его через read_skill и правь через update_skill")
    files = _new_files(args)
    if problem := check_files(name, files):
        raise ValueError(problem)
    return files


def create_preview(args: dict, ctx: ToolContext) -> dict:
    files = _check_new(args, ctx)
    return {"title": f"Создать скилл {args['name']}", "text": args["reason"],
            "skill": {"files": files, "requires": args.get("requires", []), "secrets": args.get("secrets", [])}}


async def create_skill(args: dict, ctx: ToolContext) -> ToolResult:
    files = _check_new(args, ctx)
    installed = await _install(ctx, args.get("requires", []))
    skill = _registry(ctx).write(args["name"], files, replace=False)
    return ToolResult(f"Скилл {skill.name} создан." + (f" Установлены пакеты: {installed}." if installed else "")
                      + _secrets_note(ctx, skill) + " Теперь запусти его на задаче пользователя через run_skill.")


def _secrets_note(ctx: ToolContext, skill) -> str:
    empty = [name for name, ok in ctx.services["secrets"].status(skill.name, skill.secrets).items() if not ok]
    if not empty:
        return ""
    return (f" Секреты не заданы: {', '.join(empty)}. Попроси пользователя заполнить их в разделе «Настройки» → "
            f"«Скиллы» → {skill.name} и дождись ответа; значения в чат не пиши и не проси.")


async def _install(ctx: ToolContext, requirements: list[str]) -> str:
    missing = ctx.services["env"].missing(requirements)
    if not missing:
        return ""
    ctx.emit({"type": "progress", "text": f"Установка пакетов: {', '.join(missing)}"})
    try:
        return await ctx.services["env"].install(missing)
    except RuntimeError as exc:
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        tail = str(exc)[-1500:]
        raise ValueError(f"Не удалось установить пакеты {', '.join(missing)}. Окружение скиллов — Python {version}: "
                         f"бери версии, которые его поддерживают (обычно свежие, через >=). Вывод установщика:\n{tail}") from None


# ---------- update_skill ----------

def _updated(args: dict, ctx: ToolContext) -> tuple[object, dict[str, str], dict[str, str]]:
    """(скилл, старые файлы, новые файлы)."""
    skill = _skill(ctx, args["name"])
    fixes = (ctx.sessions.get(ctx.session_id).get("skill_fixes") or {}).get(skill.name, 0)
    if fixes >= MAX_FIXES:
        raise ValueError(f"{MAX_FIXES} правки скилла {skill.name} подряд не помогли. Больше не правь: опиши "
                         "пользователю, что не получается, и спроси, как быть.")
    before = _registry(ctx).read_files(skill.name)
    changed = {path.strip().lstrip("./"): content for path, content in args["files"].items()}
    after = {**before, **changed}
    header = {key: args[key] for key in ("requires", "secrets") if key in args}
    if header:
        after["SKILL.md"] = set_header(after["SKILL.md"], **header)
    if after == before:
        raise ValueError("Правка ничего не меняет")
    if problem := check_files(skill.name, after):
        raise ValueError(problem)
    return skill, before, after


def update_preview(args: dict, ctx: ToolContext) -> dict:
    skill, before, after = _updated(args, ctx)
    changes = "\n".join(diff(before.get(path, ""), after[path], path) for path in after if before.get(path) != after[path])
    return {"title": f"Изменить скилл {skill.name}", "text": args["reason"], "diff": changes}


async def update_skill(args: dict, ctx: ToolContext) -> ToolResult:
    skill, _, after = _updated(args, ctx)
    installed = await _install(ctx, parse_skill_md(after["SKILL.md"])[0]["requires"])
    version = ctx.services["history"].snapshot(skill.folder)
    updated = _registry(ctx).write(skill.name, after, replace=True)
    fixes = dict(ctx.sessions.get(ctx.session_id).get("skill_fixes") or {})
    fixes[skill.name] = fixes.get(skill.name, 0) + 1
    ctx.sessions.update(ctx.session_id, skill_fixes=fixes)
    return ToolResult(f"Скилл {updated.name} изменён, прежняя версия — в истории ({version})."
                      + (f" Установлены пакеты: {installed}." if installed else "") + _secrets_note(ctx, updated))


# ---------- run_skill ----------

def _arguments(skill) -> tuple[str, list[str]]:
    """Раздел «# Аргументы» из SKILL.md и обязательные из него: строки «- имя — … (обязательно)»."""
    section = re.search(r"(?ms)^# Аргументы[^\n]*\n(.*?)(?=^# |\Z)", skill.text)
    text = section.group(1).strip() if section else ""
    return text, re.findall(r"(?m)^- (\w+) — .*\(обязательно\)", text)


def _runnable(args: dict, ctx: ToolContext):
    skill = _skill(ctx, args["name"])
    if skill.error:
        raise ValueError(f"Скилл {skill.name} сломан: {skill.error}. Прочитай его и исправь через update_skill")
    section, required = _arguments(skill)
    given = args.get("args") or {}
    if missing := [name for name in required if given.get(name) in (None, "", [], {})]:
        raise ValueError(f"Не хватает обязательных аргументов скилла {skill.name}: {', '.join(missing)}. "
                         f"Вызови run_skill снова, args — JSON-объект. Аргументы скилла:\n{section}")
    empty = [name for name, ok in ctx.services["secrets"].status(skill.name, skill.secrets).items() if not ok]
    if empty:
        raise ValueError(f"Скилл {skill.name} не настроен: не заданы {', '.join(empty)}. Попроси пользователя заполнить "
                         f"их в разделе «Настройки» → «Скиллы» → {skill.name}; значения в чат не пиши и не проси.")
    return skill


def run_preview(args: dict, ctx: ToolContext) -> dict:
    skill = _runnable(args, ctx)
    missing = ctx.services["env"].missing(skill.requires)
    return {"title": f"Запустить скилл {skill.name}",
            "text": f"Перед запуском поставятся пакеты: {', '.join(missing)}." if missing else ""}


async def run_skill(args: dict, ctx: ToolContext) -> ToolResult:
    skill = _runnable(args, ctx)
    await ctx.services["env"].ensure()
    await _install(ctx, skill.requires)
    values = ctx.services["secrets"].values(skill.name)
    secrets = {name: values[name] for name in skill.secrets if name in values}
    env = runner.environment(ctx.project, skill, ctx.services["llm_info"](), secrets)
    text, failed = await runner.run(skill, args.get("args") or {}, project=ctx.project.resolve(),
                                    python=ctx.services["env"].python, env=env,
                                    on_progress=lambda line: ctx.emit({"type": "progress", "text": line[:200]}))
    text = ctx.services["secrets"].mask(text, secrets)
    if failed:
        return ToolResult(f"{text}\n\nЕсли ошибка в коде скилла — прочитай его и предложи исправление через "
                          "update_skill; если в аргументах — вызови снова с другими.", True)
    fixes = dict(ctx.sessions.get(ctx.session_id).get("skill_fixes") or {})
    if fixes.pop(skill.name, None):
        ctx.sessions.update(ctx.session_id, skill_fixes=fixes)
    return ToolResult(text)


NAME = {"type": "string", "description": "Имя скилла (имя папки)"}
FILES = {"type": "object", "description": "Файлы скилла: SKILL.md и/или script.py → полный текст, например "
         "{\"script.py\": \"…\"}. Других файлов у скилла нет: весь код — в одном script.py"}
PACKAGES = {"type": "array", "items": {"type": "string"},
            "description": "Пакеты PyPI с версиями, например [\"pypdf==5.1.0\"]; без ссылок и флагов"}
SECRETS = {"type": "array", "items": {"type": "string"}, "description": "Имена секретов, например [\"JIRA_TOKEN\"]"}

TOOLS = [
    Tool("read_skill", "Прочитать скилл: SKILL.md (что делает, аргументы, примеры), список файлов, пакеты, секреты. "
         "Читай перед первым запуском скилла. С file=script.py — код скилла (перед правкой через update_skill).",
         {"type": "object", "properties": {
             "name": NAME, "file": {"type": "string", "description": "Файл скилла: script.py или SKILL.md"},
         }, "required": ["name"], "additionalProperties": False},
         read_skill),
    Tool("create_skill", "Создать новый скилл, когда для задачи нет подходящего: ровно два файла — SKILL.md и "
         "script.py, весь код в одном script.py (аргументы — JSON из stdin, результат — в stdout, прогресс — строками "
         "в stderr). Секреты (токены, "
         "пароли) — не аргументы: их значения скрипт получает переменными окружения с теми же именами, "
         "например os.environ[\"JIRA_TOKEN\"]; в secrets перечисли имена. "
         "Большие объёмы обрабатывай по кускам и сохраняй прогресс в meepo_skill.State — повторный запуск продолжит "
         "с места остановки. Пользователь подтверждает карточкой. Шаблон SKILL.md:\n" + SKILL_TEMPLATE,
         {"type": "object", "properties": {
             "name": {"type": "string", "description": "Имя: латиница в нижнем регистре, цифры, дефис"},
             "reason": {"type": "string", "description": "Зачем скилл: какой задачи пользователя без него не решить"},
             "skill_md": {"type": "string", "description": "Полный текст SKILL.md по шаблону"},
             "files": FILES, "requires": PACKAGES, "secrets": SECRETS,
         }, "required": ["name", "reason", "skill_md", "files"], "additionalProperties": False},
         create_skill, "skill_edit", create_preview),
    Tool("update_skill", "Изменить скилл: передай полный новый текст каждого изменяемого файла. Перед записью прежняя "
         "версия уходит в историю. Пользователь подтверждает каждую правку.",
         {"type": "object", "properties": {
             "name": NAME, "reason": {"type": "string", "description": "Что и зачем меняется"},
             "files": FILES, "requires": PACKAGES, "secrets": SECRETS,
         }, "required": ["name", "reason", "files"], "additionalProperties": False},
         update_skill, "skill_edit", update_preview),
    Tool("run_skill", "Запустить скилл с аргументами из его SKILL.md. Пока он идёт, пользователь видит прогресс.",
         {"type": "object", "properties": {
             "name": NAME, "args": {"type": "object", "description": "Аргументы скилла JSON-объектом"},
         }, "required": ["name"], "additionalProperties": False},
         run_skill, "skill_run", run_preview),
]
