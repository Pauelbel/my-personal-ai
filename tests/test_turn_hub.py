"""Ход идёт независимо от вкладки: к нему подключаются заново, его останавливают, сообщения ждут в очереди."""

import asyncio
import time

from fastapi.testclient import TestClient
from tests.fakes import post_session
from tests.test_graph_flow import settings

from local_agent.api.app import create_app
from local_agent.llm.models import ChatResult


class GatedProvider:
    """Отвечает, только когда тест откроет шлагбаум: так ход гарантированно ещё идёт."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls = 0

    async def chat(self, model, messages, tools=None, response_format=None, options=None):
        self.calls += 1
        number = self.calls
        await self.gate.wait()
        return ChatResult(content=f"Ответ {number}", input_tokens=10, output_tokens=2)

    async def list_models(self):
        return ["test"]

    async def close(self):
        pass


def wait_idle(client, url) -> dict:
    for _ in range(200):
        state = client.get(url + "/turn").json()
        if not state["running"]:
            return state
        time.sleep(0.02)
    raise AssertionError("ход не закончился")


def test_turn_can_be_followed_again_and_queue_goes_after_answer(tmp_path):
    provider = GatedProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        hub = client.app.state.turn_hub
        # Ничего не идёт: сообщение из очереди уходит сразу.
        client.post(url + "/queue", json={"content": "Первый"})
        queued = client.post(url + "/queue", json={"content": "Второй"}).json()
        busy = client.post(url + "/turns/stream", json={"content": "Третий"})
        state = client.get(url + "/turn").json()

        async def follow_from_start():
            turn = hub.running(session["id"])
            reader = asyncio.create_task(_types(hub.follow(turn)))
            provider.gate.set()
            return await reader

        events = client.portal.call(follow_from_start)
        final = wait_idle(client, url)
        history = client.get(url + "/messages").json()

    assert busy.status_code == 409
    assert state["running"] and state["queue"] == [queued]
    # Подключились после начала хода, но получили его целиком.
    assert events[0] == "user_message" and events[-1] == "done"
    assert final["queue"] == []
    assert [(item["role"], item["content"]) for item in history] == [
        ("user", "Первый"), ("assistant", "Ответ 1"), ("user", "Второй"), ("assistant", "Ответ 2"),
    ]


def test_stopped_turn_keeps_the_queue_for_the_user(tmp_path):
    provider = GatedProvider()
    with TestClient(create_app(settings(tmp_path), llm_provider=provider)) as client:
        session = post_session(client, {"model": "test"}).json()
        url = f"/api/sessions/{session['id']}"
        client.post(url + "/queue", json={"content": "Долгий"})
        item = client.post(url + "/queue", json={"content": "Потом"}).json()
        stopped = client.post(url + "/turn/stop")
        state = wait_idle(client, url)
        removed = client.delete(f"{url}/queue/{item['id']}")
        after = client.get(url + "/turn").json()
        again = client.post(url + "/turn/stop")
        history = client.get(url + "/messages").json()

    assert stopped.status_code == 204 and state["queue"] == [item]
    assert removed.status_code == 204 and after["queue"] == [] and again.status_code == 404
    # Остановленный ход не тянет за собой очередь: «Потом» так и не ушло.
    assert [item["content"] for item in history if item["role"] == "user"] == ["Долгий"]


def test_session_changes_are_published(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        bus = client.app.state.events
        queue = client.portal.call(_subscribe, bus)
        session = post_session(client, {"model": "test"}).json()
        client.put(f"/api/sessions/{session['id']}/title", json={"title": "Новое имя"})
        events = client.portal.call(_drain, queue)
        bus.unsubscribe(queue)

    assert {"type": "changed", "session_id": session["id"]} in events


async def _types(events) -> list[str]:
    return [event["type"] async for event in events]


async def _subscribe(bus):
    return bus.subscribe()


async def _drain(queue) -> list[dict]:
    await asyncio.sleep(0.05)
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items
