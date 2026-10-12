"""Картинки в чате: загрузка, передача модели, вставка в заметку вики, удаление вместе с сессией."""

import base64
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from tests.fakes import ScriptedProvider, call, events_of, post_session
from tests.test_graph_flow import settings

from local_agent.agent.context import IMAGE_MESSAGES, build_context
from local_agent.api.app import create_app
from local_agent.llm.models import ChatMessage
from local_agent.llm.openai_compatible import OpenAICompatibleProvider
from local_agent.memory.attachments import AttachmentStore
from local_agent.memory.models import Message
from local_agent.wiki.service import NoteFields

PIXEL = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PIXEL_URL = "data:image/png;base64," + base64.b64encode(PIXEL).decode()


def test_store_keeps_only_images_under_its_own_names(tmp_path):
    store = AttachmentStore(tmp_path)
    name = store.save(PIXEL_URL)
    assert store.path(name).read_bytes() == PIXEL and name.endswith(".png")
    with pytest.raises(ValueError, match="картинка"):
        store.save("data:text/html;base64," + base64.b64encode(b"<script>").decode())
    # Имя из сообщения не может указать на чужой файл.
    assert store.path("../secret.png") is None and store.path("x.png") is None
    store.delete([name])
    assert store.path(name) is None


def test_pasted_image_reaches_model_and_goes_into_wiki_note(tmp_path):
    provider = ScriptedProvider(
        call("wiki_save_note", title="Схема стенда", description="Как устроен тестовый стенд",
             ideas=["Стенд из трёх сервисов"], keywords=["стенд", "схема"], images=["NAME"]),
        "Сохранил заметку с картинкой",
    )
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        name = client.post("/api/attachments", json={"data": PIXEL_URL}).json()["name"]
        provider.steps[0][1]["images"] = [name]
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        # Картинка без текста — тоже сообщение.
        events = events_of(client.post(url + "/turns/stream", json={"content": "", "attachments": [name]}))
        history = client.get(url + "/messages").json()
        note = client.get("/api/wiki/page", params={"path": "схема_стенда.md"}).json()["content"]
        image = client.get("/api/wiki/file", params={"path": f"images/{name}"})
        missing = client.post(url + "/turns/stream", json={"content": "ещё", "attachments": ["0123456789ab.png"]})
        # Заметку дополнили без новых картинок — старая осталась.
        client.app.state.wiki_service.save_note(
            NoteFields(title="Схема стенда", description="Как устроен стенд", ideas=("Три сервиса",), keywords=("стенд", "схема")),
            path="схема_стенда.md",
        )
        rebuilt = client.app.state.wiki_service.read("схема_стенда.md")
        client.delete(url)

    assert events[-1]["type"] == "done"
    [user] = [item for item in history if item["role"] == "user"]
    assert user["attachments"] == [name] and user["content"] == ""
    # Модель получила картинку файлом и её имя текстом — по имени агент вставил её в заметку.
    question = provider.requests[0]["messages"][-1]
    assert question.images == (str(tmp_path / "attachments" / name),) and f"[Картинки: {name}]" in question.content
    assert f"## Картинки\n\n![Схема стенда](images/{name})" in note
    assert image.status_code == 200 and image.content == PIXEL
    assert missing.status_code == 400
    assert f"![Схема стенда](images/{name})" in rebuilt
    # Картинка ушла вместе с сессией, а копия в вики осталась.
    assert not (tmp_path / "attachments" / name).exists() and (tmp_path / "wiki" / "images" / name).exists()


def test_model_sees_only_recent_images(tmp_path):
    store = AttachmentStore(tmp_path)
    names = [store.save(PIXEL_URL) for _ in range(IMAGE_MESSAGES + 1)]
    history = [
        Message(id=str(number), session_id="s", role="user", content=f"вопрос {number}",
                created_at=datetime.now(UTC), attachments=[name])
        for number, name in enumerate(names)
    ]
    context = build_context("Правила", history, max_messages=20, max_tokens=100_000, images=store.path)
    first, *rest = context.messages[1:]
    # Старую картинку модель уже не видит, но знает её имя — может попросить прислать снова.
    assert first.images == () and f"{names[0]} — их уже не видно" in first.content
    assert all(len(message.images) == 1 for message in rest)


def test_provider_sends_image_as_content_part(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(PIXEL)
    payload = OpenAICompatibleProvider._message_payload(ChatMessage("user", "Что тут?", images=(str(path),)))
    assert payload["content"] == [
        {"type": "text", "text": "Что тут?"},
        {"type": "image_url", "image_url": {"url": PIXEL_URL}},
    ]
