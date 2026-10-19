"""Сервер стартует и отдаёт страницу интерфейса."""

from fastapi.testclient import TestClient

from meepo.app import create_app
from meepo.settings import Env, UserSettings


def test_index_page(tmp_path):
    client = TestClient(create_app(Env(data_path=tmp_path)))
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>Meepo</title>" in response.text
    assert client.get("/ui/css/theme.css").status_code == 200


def test_env_file_and_overrides(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# комментарий\nLLM_BASE_URL=http://x/v1\nPORT=9000\nLLM_VERIFY_SSL=false\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "9100")
    env = Env.load(env_file)
    assert env.llm_base_url == "http://x/v1"
    assert env.port == 9100
    assert env.llm_verify_ssl is False


def test_user_settings_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    assert UserSettings.load(path).max_tool_rounds == 40
    settings = UserSettings(model="m", guards={"language": False})
    settings.save(path)
    loaded = UserSettings.load(path)
    assert loaded.model == "m"
    assert loaded.guards == {"language": False, "promise": True, "plan": True, "retry": True}
