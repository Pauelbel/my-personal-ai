"""Jira только на чтение: задачи по запросу JQL. Нашлась одна — она целиком: описание, связи, комментарии;
несколько — списком. Шаблоны запросов («мои задачи», «тикет по ключу») — в подсказке модели в tool.md.

Аргументы приходят JSON-объектом в stdin, результат — текстом в stdout; ошибка — текст в stderr и код 1.
Адрес и токен — переменные окружения из секретов (форма «настроить» у коннектора jira):
- JIRA_URL — адрес Jira, например https://jira.company.ru;
- JIRA_TOKEN — персональный токен (Jira Server/Data Center, заголовок Bearer)
  или API-токен Jira Cloud — тогда нужен ещё JIRA_EMAIL (Basic);
- JIRA_VERIFY_SSL=false — не проверять сертификат (корпоративный, самоподписанный).
API v2 есть и в Server, и в Cloud и отдаёт описание текстом, а не деревом документа, как v3.
"""

import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

LIST_FIELDS = "summary,status,issuetype,priority,assignee,updated"
ISSUE_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,created,updated,labels,components,fixVersions,"
    "parent,issuelinks,subtasks,description,comment"
)
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_DESCRIPTION_CHARS = 6000
MAX_COMMENTS = 10
MAX_COMMENT_CHARS = 1500
TIMEOUT_SECONDS = 20


class JiraError(Exception):
    pass


class Gone(Exception):
    """Старый поиск убран (Jira Cloud): нужен новый адрес search/jql."""


def main() -> None:
    arguments = json.load(sys.stdin)
    action = arguments.get("action")
    if action == "check":
        # Служебный вызов кнопки «Проверить подключение»: модели он не виден.
        me = get("/rest/api/2/myself")
        print(f"Подключено: {me.get('displayName') or me.get('name') or 'пользователь'}")
        return
    jql = arguments.get("jql")
    if not isinstance(jql, str) or not jql.strip():
        raise JiraError("Нужен jql — запрос JQL, например key = QA-123")
    limit = arguments.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, str) and limit.strip().isdigit():
        limit = int(limit)
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = DEFAULT_LIMIT
    print(find(jql.strip(), max(1, min(limit, MAX_LIMIT))))


# ---------- запросы ----------

def find(jql: str, limit: int) -> str:
    """Одна найденная задача — целиком, несколько — списком."""
    params = {"jql": jql, "maxResults": limit, "fields": LIST_FIELDS}
    try:
        data = get("/rest/api/2/search", params)
    except Gone:
        # Jira Cloud убрала старый поиск: новый — search/jql, без общего числа найденных.
        data = get("/rest/api/2/search/jql", params)
    issues = data.get("issues") or []
    if not issues:
        return f"По запросу «{jql}» задач нет."
    total = data.get("total")
    if len(issues) == 1 and not (isinstance(total, int) and total > 1):
        return issue(issues[0].get("key", ""))
    head = f"Задач: {len(issues)}" + (f" из {total}" if isinstance(total, int) and total > len(issues) else "")
    lines = [f"{head} · JQL: {jql}", f"Ссылка на задачу: {base()}/browse/<КЛЮЧ>"]
    for item in issues:
        fields = item.get("fields") or {}
        lines.append(" · ".join(filter(None, [
            item.get("key"), name(fields.get("status")), name(fields.get("issuetype")),
            name(fields.get("priority")), fields.get("summary"),
            f"исполнитель: {person(fields.get('assignee'))}", f"обновлена {date(fields.get('updated'))}",
        ])))
    return "\n".join(lines)


def issue(key: str) -> str:
    data = get(f"/rest/api/2/issue/{urllib.parse.quote(key)}", {"fields": ISSUE_FIELDS})
    fields = data.get("fields") or {}
    key = data.get("key", key)
    lines = [
        f"{key} — {fields.get('summary') or ''}",
        f"Ссылка: {base()}/browse/{key}",
        " · ".join(filter(None, [
            name(fields.get("issuetype")), f"статус: {name(fields.get('status'))}",
            f"приоритет: {name(fields.get('priority'))}" if fields.get("priority") else "",
        ])),
        f"Исполнитель: {person(fields.get('assignee'))} · автор: {person(fields.get('reporter'))}",
        f"Создана {date(fields.get('created'))} · обновлена {date(fields.get('updated'))}",
    ]
    extras = [
        ("Метки", ", ".join(fields.get("labels") or [])),
        ("Компоненты", ", ".join(name(item) for item in fields.get("components") or [])),
        ("Версии", ", ".join(name(item) for item in fields.get("fixVersions") or [])),
        ("Родитель", brief(fields.get("parent"))),
        ("Подзадачи", "; ".join(brief(item) for item in fields.get("subtasks") or [])),
        ("Связи", "; ".join(link(item) for item in fields.get("issuelinks") or [])),
    ]
    lines += [f"{title}: {value}" for title, value in extras if value]
    description = (fields.get("description") or "").strip()
    lines += ["", "Описание:", clip(description, MAX_DESCRIPTION_CHARS) if description else "(пусто)"]
    comments = ((fields.get("comment") or {}).get("comments")) or []
    if comments:
        shown = comments[-MAX_COMMENTS:]
        lines += ["", f"Комментарии ({len(comments)}, показаны последние {len(shown)}):" if len(comments) > len(shown)
                  else f"Комментарии ({len(comments)}):"]
        for comment in shown:
            lines.append(f"— {person(comment.get('author'))}, {moment(comment.get('created'))}: "
                         f"{clip((comment.get('body') or '').strip(), MAX_COMMENT_CHARS)}")
    return "\n".join(lines)


def get(path: str, params: dict | None = None) -> dict:
    token = os.environ.get("JIRA_TOKEN", "")
    if not base() or not token:
        raise JiraError("Нет секретов JIRA_URL и JIRA_TOKEN: пользователь добавляет их в форме «Возможности → Инструменты → Коннекторы → jira → настроить»")
    if not base().startswith(("http://", "https://")):
        raise JiraError("JIRA_URL должен начинаться с http:// или https://, например https://jira.company.ru")
    email = os.environ.get("JIRA_EMAIL", "")
    credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
    request = urllib.request.Request(
        base() + path + ("?" + urllib.parse.urlencode(params) if params else ""),
        headers={"Accept": "application/json", "Authorization": f"Basic {credentials}" if email else f"Bearer {token}"},
    )
    context = ssl.create_default_context()
    if os.environ.get("JIRA_VERIFY_SSL", "").lower() in {"false", "0", "no"}:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in (404, 410) and path.endswith("/search"):
            raise Gone() from None
        if status in (401, 403):
            raise JiraError(f"Jira отклонила доступ (HTTP {status}): проверьте JIRA_TOKEN"
                            " (и JIRA_EMAIL для Jira Cloud) в форме «Возможности → Инструменты → Коннекторы → jira → настроить»") from None
        if status == 404:
            raise JiraError("Не найдено: задачи нет или к ней нет доступа") from None
        raise JiraError(f"Jira вернула HTTP {status}: {errors(exc.read())}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        hint = " Если сертификат корпоративный, задайте секрет JIRA_VERIFY_SSL=false." if "CERTIFICATE" in str(reason).upper() else ""
        raise JiraError(f"Jira недоступна: {reason}.{hint}") from None
    try:
        return json.loads(body)
    except ValueError:
        raise JiraError("Jira ответила не JSON: проверьте JIRA_URL — нужен адрес самой Jira") from None


def base() -> str:
    return os.environ.get("JIRA_URL", "").strip().rstrip("/")


# ---------- оформление ----------

def errors(body: bytes) -> str:
    """Текст ошибки Jira — например, что не так в JQL: по нему модель исправит запрос."""
    try:
        data = json.loads(body)
    except ValueError:
        return body.decode("utf-8", errors="replace")[:300]
    messages = list(data.get("errorMessages") or []) + [f"{key}: {value}" for key, value in (data.get("errors") or {}).items()]
    return "; ".join(messages)[:1000] or str(data)[:300]


def name(item: object) -> str:
    return str(item.get("name") or "") if isinstance(item, dict) else ""


def person(item: object) -> str:
    return str(item.get("displayName") or item.get("name") or "—") if isinstance(item, dict) else "не назначен"


def brief(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    fields = item.get("fields") or {}
    return f"{item.get('key')} {fields.get('summary') or ''} [{name(fields.get('status'))}]".strip()


def link(item: dict) -> str:
    kind = item.get("type") or {}
    if "outwardIssue" in item:
        return f"{kind.get('outward', 'связана с')} {brief(item['outwardIssue'])}"
    return f"{kind.get('inward', 'связана с')} {brief(item.get('inwardIssue'))}"


def date(value: object) -> str:
    return str(value)[:10] if value else "—"


def moment(value: object) -> str:
    return str(value)[:16].replace("T", " ") if value else "—"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… (обрезано до {limit} символов)"


if __name__ == "__main__":
    try:
        main()
    except JiraError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
