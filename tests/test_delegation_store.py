"""Поручения сохраняются между запусками и остаются привязаны к своей сессии."""

import pytest

from core.delegation import DelegationStore, diagnose


def create_run(store, *, session="session-a", call="call-1"):
    return store.create(
        session_id=session, task_id="task-1", agent="test-cases", revision="revision-1",
        inputs={"источник": "docs/spec.md"}, workspace=str(store.root.parent / "project"), call_id=call,
    )


def failed_snapshot(output="ModuleNotFoundError: No module named 'local_agent'"):
    return {
        "status": "failed", "error": "Скрипт завершился с кодом 1.",
        "steps": [{"title": "Написать тест-кейсы", "kind": "script", "status": "failed", "output": output}],
    }


def test_persisted_run_keeps_attempt_history_inputs_and_diagnosis_after_restart(tmp_path):
    store = DelegationStore(tmp_path / "delegations")
    run = create_run(store)
    store.update(run["id"], attempts=1)
    failed = store.finish(run["id"], failed_snapshot())
    run["inputs"]["источник"] = "modified-by-caller"
    failed["snapshot"]["steps"][0]["output"] = "modified-by-caller"

    restarted = DelegationStore(store.root)
    restored = restarted.get(run["id"], "session-a")

    assert restored["inputs"] == {"источник": "docs/spec.md"}
    assert restored["agent"] == "test-cases"
    assert restored["attempts"] == 1
    assert restored["status"] == "failed"
    assert restored["diagnosis"]["kind"] == "runtime"
    assert "ModuleNotFoundError" in restored["snapshot"]["steps"][0]["output"]
    assert restored["history"] == [{
        "attempt": 1, "revision": "revision-1", "status": "failed", "error": "Скрипт завершился с кодом 1.",
    }]
    assert restarted.active("session-a")["id"] == run["id"]
    assert restarted.get(run["id"], "session-b") is None
    assert restarted.active("session-b") is None


@pytest.mark.parametrize("run_id", [None, 1, [], "", "../outside", "g" * 32, "0" * 32])
def test_invalid_or_missing_run_ids_are_rejected(tmp_path, run_id):
    store = DelegationStore(tmp_path / "delegations")

    assert store.get(run_id, "session-a") is None
    with pytest.raises(ValueError, match="Поручение не найдено"):
        store.update(run_id, status="done")
    with pytest.raises(ValueError, match="Поручение не найдено"):
        store.finish(run_id, {"status": "done", "steps": []})


def test_by_call_recognizes_previous_calls_after_retry_and_restart_with_session_isolation(tmp_path):
    store = DelegationStore(tmp_path / "delegations")
    first = create_run(store)
    other = create_run(store, session="session-b")
    store.update(first["id"], attempts=1)
    store.finish(first["id"], failed_snapshot())
    store.update(first["id"], attempts=2, calls=["call-1", "call-2"])
    store.finish(first["id"], {"status": "done", "steps": []})
    restarted = DelegationStore(store.root)

    for call_id in ("call-1", "call-2", "call-1"):
        previous = restarted.by_call("session-a", call_id)
        assert previous["id"] == first["id"]
        assert previous["attempts"] == 2 and previous["status"] == "done"
        assert previous["diagnosis"] == {}
        assert [attempt["status"] for attempt in previous["history"]] == ["failed", "done"]
    assert restarted.by_call("session-b", "call-1")["id"] == other["id"]
    assert restarted.by_call("session-b", "call-2") is None
    assert restarted.by_call("session-a", "") is None
    assert restarted.by_call("session-a", "unknown") is None


def test_active_excludes_finished_and_stopped_runs_and_stop_affects_only_its_session(tmp_path):
    store = DelegationStore(tmp_path / "delegations")
    running = create_run(store, call="running")
    failed = create_run(store, call="failed")
    store.finish(failed["id"], failed_snapshot())
    blocked = create_run(store, call="blocked")
    store.update(blocked["id"], status="blocked")
    done = create_run(store, call="done")
    store.finish(done["id"], {"status": "done", "steps": []})
    stopped = create_run(store, call="stopped")
    store.update(stopped["id"], status="stopped")
    other = create_run(store, session="session-b")

    assert store.active("session-a")["id"] == blocked["id"]
    store.stop("session-a")

    assert store.active("session-a") is None
    assert all(store.get(run["id"])["status"] == "stopped" for run in (running, failed, blocked, stopped))
    assert store.get(done["id"])["status"] == "done"
    assert store.active("session-b")["id"] == other["id"]


@pytest.mark.parametrize(("output", "kind", "retryable"), [
    ("ModuleNotFoundError: No module named 'local_agent'", "runtime", False),
    ("ImportError: cannot import name 'timeout'", "runtime", False),
    ("NameError: name 'client' is not defined", "code", False),
    ("Нет папки или файла docs/missing.md в проекте", "inputs", False),
    ("PermissionError: permission denied", "access", False),
    ("Connection refused", "transient", True),
    ("HTTP 503: Service unavailable", "transient", True),
    ("Скрипт не уложился в 30 с и был остановлен", "transient", True),
    ("Скрипт завершился с кодом 7: unknown problem", "execution", False),
])
def test_diagnosis_distinguishes_runtime_repair_transient_retry_and_unknown_failures(output, kind, retryable):
    result = diagnose(failed_snapshot(output))

    assert result["kind"] == kind
    assert result["retryable"] is retryable
    assert result["details"] == output
    assert result["action"]


def test_diagnosis_uses_full_failed_step_instead_of_the_short_snapshot_error():
    snapshot = failed_snapshot("Traceback\nModuleNotFoundError: No module named 'qa'")
    snapshot["steps"].insert(0, {"status": "done", "output": "HTTP 503 was fixed"})

    result = diagnose(snapshot)

    assert result["kind"] == "runtime" and not result["retryable"]
    assert "qa" in result["details"]
