"""Секреты: значения не уходят ни в API, ни в модель; скрипт получает только объявленные. Jira — только чтение."""

import asyncio
import base64
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from tests.test_graph_flow import settings

from local_agent.api.app import create_app
from local_agent.connectors.scripts import read_script_tool, write_script_tool
from local_agent.connectors.secrets import SecretStore

TOKEN = "s3cr3t-token-value"


def test_store_keeps_values_private_and_masks_them(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.set("JIRA_TOKEN", f"  {TOKEN} ")
    store.set("FLAG", "1")
    assert store.names() == ["FLAG", "JIRA_TOKEN"] and store.get("JIRA_TOKEN") == TOKEN
    assert stat.S_IMODE((tmp_path / "secrets.json").stat().st_mode) == 0o600
    # Короткое значение не маскируется: иначе пропали бы все единицы в выводе.
    assert store.mask(f"token={TOKEN}, flag=1") == "token=***, flag=1"
    for bad in ("jira_token", "1TOKEN", "A", "JIRA-TOKEN"):
        with pytest.raises(ValueError):
            store.set(bad, "x")
    store.delete("FLAG")
    assert store.names() == ["JIRA_TOKEN"]


def enable_jira(client):
    assert client.post("/api/capabilities/library/tool/jira").json() == {"id": "jira"}


def test_api_writes_secrets_but_never_returns_values(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        enable_jira(client)
        assert client.put("/api/capabilities/secrets/JIRA_TOKEN", json={"value": TOKEN}).status_code == 204
        assert client.put("/api/capabilities/secrets/bad-name", json={"value": "x"}).status_code == 400
        response = client.get("/api/capabilities")
        assert TOKEN not in response.text
        jira = next(tool for tool in response.json()["tools"] if tool["id"] == "jira")
        fields = {field["name"]: field for field in jira["fields"]}
        # Форма коннектора видит, что токен задан, а адрес ещё нет.
        assert fields["JIRA_TOKEN"]["set"] is True and fields["JIRA_URL"]["set"] is False
        check = client.post("/api/capabilities/connectors/jira/check")
        assert check.status_code == 400 and "JIRA_URL" in check.json()["detail"]
        assert client.delete("/api/capabilities/secrets/JIRA_TOKEN").status_code == 204
        assert client.delete("/api/capabilities/secrets/JIRA_TOKEN").status_code == 404


def test_script_gets_only_declared_secrets_and_output_is_masked(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.set("API_TOKEN", TOKEN)
    store.set("OTHER_TOKEN", "other-secret-value")
    code = "import os\nprint(os.environ.get('API_TOKEN'), os.environ.get('OTHER_TOKEN'))\n"
    tool = write_script_tool(
        tmp_path / "tools", "echo_token", name="x", description="x", reason="r", parameters={"type": "object"},
        code=code, requires_approval=False, network=False, secrets=["API_TOKEN"],
    )
    tool = read_script_tool(tool.folder)
    assert tool.secrets == ("API_TOKEN",)
    tool.secret_store = store
    result = asyncio.run(tool.execute({}, None))
    assert not result.is_error and result.content == "*** None"

    tool.secrets = ("API_TOKEN", "MISSING_TOKEN")
    missing = asyncio.run(tool.execute({}, None))
    assert missing.is_error and "MISSING_TOKEN" in missing.content and "настроить" in missing.content


# ---------- Jira: скрипт data/tools/jira против локального сервера ----------

ISSUE = {
    "key": "QA-7",
    "fields": {
        "summary": "Падает экспорт", "status": {"name": "В работе"}, "issuetype": {"name": "Баг"},
        "priority": {"name": "High"}, "assignee": {"displayName": "Павел"}, "updated": "2026-10-15T10:00:00.000+0300",
    },
}


class FakeJira(BaseHTTPRequestHandler):
    """Jira Server по умолчанию; cloud=True — старый поиск отвечает 410, как в Jira Cloud."""

    cloud = False
    requests: list[tuple[str, dict, str]] = []

    def do_GET(self):
        url = urlparse(self.path)
        FakeJira.requests.append((url.path, parse_qs(url.query), self.headers.get("Authorization", "")))
        if url.path == "/rest/api/2/search" and FakeJira.cloud:
            return self.reply(410, {})
        if url.path in ("/rest/api/2/search", "/rest/api/2/search/jql"):
            if "stauts" in parse_qs(url.query)["jql"][0]:
                return self.reply(400, {"errorMessages": ["Поле 'stauts' не существует"]})
            return self.reply(200, {"issues": [ISSUE], **({} if FakeJira.cloud else {"total": 3})})
        if url.path == "/rest/api/2/issue/QA-7":
            issue = json.loads(json.dumps(ISSUE))
            issue["fields"] |= {
                "description": "Шаги: открыть отчёт", "labels": ["regress"],
                "comment": {"comments": [{"author": {"displayName": "Анна"}, "created": "2026-10-15T11:30:00.000+0300",
                                          "body": f"Воспроизвела, токен {TOKEN}"}]},
                "issuelinks": [{"type": {"outward": "блокирует"},
                                "outwardIssue": {"key": "QA-8", "fields": {"summary": "Релиз", "status": {"name": "Open"}}}}],
            }
            return self.reply(200, issue)
        if url.path == "/rest/api/2/myself":
            return self.reply(200, {"displayName": "Павел"})
        return self.reply(404, {})

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def jira(tmp_path):
    """Клиент приложения с заданными секретами Jira, указывающими на локальный сервер."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeJira)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeJira.requests, FakeJira.cloud = [], False
    with TestClient(create_app(settings(tmp_path))) as client:
        enable_jira(client)
        store = client.app.state.secrets
        store.set("JIRA_URL", f"http://127.0.0.1:{server.server_address[1]}/")
        store.set("JIRA_TOKEN", TOKEN)
        tool = client.app.state.tool_registry.get("jira")
        yield client, store, lambda **arguments: asyncio.run(tool.execute(arguments, None))
    server.shutdown()


def test_jira_is_ready_to_enable_then_editable_and_archived_one_stays_archived(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        # Готовый коннектор не включается сам: он среди готовых, пока его не включат.
        assert client.app.state.tool_registry.get("jira") is None
        assert {"kind": "tool", "id": "jira"}.items() <= next(
            item for item in client.get("/api/capabilities").json()["library"] if item["id"] == "jira"
        ).items()
        enable_jira(client)
        assert client.post("/api/capabilities/library/tool/jira").status_code == 404
        source = client.get("/api/capabilities/tool/jira").json()
        assert set(source["files"]) == {"tool.md", "parameters.json", "script.py"}
        assert "secrets: JIRA_URL, JIRA_TOKEN, JIRA_EMAIL?, JIRA_VERIFY_SSL?" in source["files"]["tool.md"]
        assert client.post("/api/capabilities/tool/jira/archive").status_code == 204
    # Убранный в архив не возвращается при следующем запуске.
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.app.state.tool_registry.get("jira") is None


def test_my_issues_use_bearer_token_on_server(jira):
    client, _, run = jira
    result = run(action="mine", limit=5)
    assert not result.is_error, result.content
    assert "QA-7 · В работе · Баг · High · Падает экспорт · исполнитель: Павел · обновлена 2026-10-15" in result.content
    assert "Задач: 1 из 3" in result.content and "/browse/<КЛЮЧ>" in result.content
    path, query, auth = FakeJira.requests[0]
    assert path == "/rest/api/2/search" and query["jql"][0].startswith("assignee = currentUser()")
    assert query["maxResults"] == ["5"] and auth == f"Bearer {TOKEN}"
    assert client.post("/api/capabilities/connectors/jira/check").json() == {"message": "Подключено: Павел"}


def test_cloud_falls_back_to_new_search_with_basic_auth(jira):
    _, store, run = jira
    store.set("JIRA_EMAIL", "me@company.ru")
    FakeJira.cloud = True
    result = run(action="search", jql="project = QA")
    assert not result.is_error and "QA-7" in result.content
    assert [request[0] for request in FakeJira.requests] == ["/rest/api/2/search", "/rest/api/2/search/jql"]
    expected = base64.b64encode(f"me@company.ru:{TOKEN}".encode()).decode()
    assert FakeJira.requests[-1][2] == f"Basic {expected}"


def test_issue_with_comments_and_jql_errors_reach_the_model(jira):
    _, _, run = jira
    result = run(action="issue", key="qa-7")
    assert not result.is_error, result.content
    for part in ("QA-7 — Падает экспорт", "Шаги: открыть отчёт", "Метки: regress", "блокирует QA-8 Релиз [Open]",
                 "— Анна, 2026-10-15 11:30: Воспроизвела, токен ***"):
        assert part in result.content
    assert TOKEN not in result.content
    error = run(action="search", jql="stauts = Open")
    assert error.is_error and "stauts" in error.content
    assert run(action="issue", key="DROP TABLE").is_error


def test_jira_without_secrets_asks_user_to_configure(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        enable_jira(client)
        result = asyncio.run(client.app.state.tool_registry.get("jira").execute({"action": "mine"}, None))
    assert result.is_error and "JIRA_URL" in result.content and "настроить" in result.content


def test_jira_is_listed_as_connector_with_form_fields(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        enable_jira(client)
        jira = next(tool for tool in client.get("/api/capabilities").json()["tools"] if tool["id"] == "jira")
        assert jira["origin"] == "connector" and jira["configured"] is False and jira["checkable"] is True
        client.put("/api/capabilities/secrets/JIRA_URL", json={"value": "https://jira.company.ru"})
        client.put("/api/capabilities/secrets/JIRA_TOKEN", json={"value": TOKEN})
        response = client.get("/api/capabilities")
        jira = next(tool for tool in response.json()["tools"] if tool["id"] == "jira")

    fields = {field["name"]: field for field in jira["fields"]}
    assert jira["configured"] is True and list(fields) == ["JIRA_URL", "JIRA_TOKEN", "JIRA_EMAIL", "JIRA_VERIFY_SSL"]
    # Адрес — не тайна, его видно в форме; токен — только «задан».
    assert fields["JIRA_URL"]["value"] == "https://jira.company.ru" and "value" not in fields["JIRA_TOKEN"]
    assert fields["JIRA_TOKEN"]["set"] and fields["JIRA_EMAIL"] == {"name": "JIRA_EMAIL", "optional": True, "set": False}
    assert TOKEN not in response.text
