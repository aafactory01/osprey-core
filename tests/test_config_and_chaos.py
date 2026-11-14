"""ConfigStore: validation, caching, and fail-closed behavior, plus chaos at the API layer."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.app import create_app
from app.config_store import ConfigError, ConfigStore
from tests.helpers import edit_json, rewrite

NAMES = ["agents", "app_settings", "audit_receipts", "cicd", "dp001_guard_policy", "health", "night_dreaming",
         "pipeline_status", "rollback", "rollouts", "shadow_replay_evals", "task_backends", "workflow_stages"]


def test_every_declared_config_validates():
    store = ConfigStore()
    assert store.names() == NAMES
    assert all(entry["ok"] for entry in store.check_all().values())


def test_cache_serves_without_rereading(temp_root, monkeypatch):
    store = ConfigStore(temp_root)
    store.get("rollouts")
    reads = []
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: reads.append(self) or original(self, *a, **k))
    store.get("rollouts")
    assert reads == []


def test_returned_documents_are_copies(temp_root):
    store = ConfigStore(temp_root)
    store.get("rollouts")["rollouts"].clear()
    assert len(store.get("rollouts")["rollouts"]) == 4


def test_changed_file_is_reloaded(temp_root):
    store = ConfigStore(temp_root)
    assert store.get("health")["min_samples"] == 5
    edit_json(temp_root / "config" / "health.json", lambda doc: doc.update(min_samples=9))
    assert store.get("health")["min_samples"] == 9


def test_file_removed_mid_run_raises_instead_of_serving_stale(temp_root):
    store = ConfigStore(temp_root)
    store.get("rollouts")
    (temp_root / "config" / "rollouts.json").unlink()
    with pytest.raises(ConfigError) as info:
        store.get("rollouts")
    assert info.value.taxonomy == "CONFIG_MISSING" and info.value.file == "config/rollouts.json"


def test_os_error_on_read_is_unreadable(temp_root, monkeypatch):
    store = ConfigStore(temp_root)
    target = temp_root / "config" / "agents.json"
    original = Path.read_text

    def flaky(self, *args, **kwargs):
        if self == target:
            raise OSError(5, "Input/output error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    with pytest.raises(ConfigError) as info:
        store.get("agents")
    assert info.value.taxonomy == "CONFIG_UNREADABLE" and "Input/output error" in info.value.detail


@pytest.mark.parametrize("text,taxonomy", [
    ("{", "CONFIG_MALFORMED"), ('{"min_samples": NaN}', "CONFIG_MALFORMED"), ("[]", "CONFIG_SCHEMA"),
    ("\ufeff{}", "CONFIG_MALFORMED"),
])
def test_malformed_documents(temp_root, text, taxonomy):
    rewrite(temp_root / "config" / "health.json", text)
    with pytest.raises(ConfigError) as info:
        ConfigStore(temp_root).get("health")
    assert info.value.taxonomy == taxonomy


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d["telemetry"].update(ring_size=True), "ring_size: expected int"),
    (lambda d: d["telemetry"].update(ring_size=10 ** 30), "above maximum"),
    (lambda d: d["telemetry"].update(ring_size=0), "below minimum"),
    (lambda d: d["ui"].update(default_tab="settings"), "is not one of"),
    (lambda d: d.pop("server"), "server: required key missing"),
    (lambda d: d.update(service_name=""), "shorter than"),
    (lambda d: d["ui"].update(fetch_timeout_ms="8000"), "expected int"),
])
def test_schema_violations_are_typed(temp_root, mutate, fragment):
    edit_json(temp_root / "config" / "app_settings.json", mutate)
    with pytest.raises(ConfigError) as info:
        ConfigStore(temp_root).get("app_settings")
    assert info.value.taxonomy == "CONFIG_SCHEMA" and fragment in info.value.detail


def test_negative_zero_and_large_values_respect_bounds(temp_root):
    edit_json(temp_root / "config" / "health.json", lambda d: d["slos"][1].update(target_ms=-0.0))
    assert ConfigStore(temp_root).get("health")["slos"][1]["target_ms"] == 0
    edit_json(temp_root / "config" / "health.json", lambda d: d["sla"].update(target=1.0000001))
    with pytest.raises(ConfigError, match="above maximum"):
        ConfigStore(temp_root).get("health")


def test_unknown_name_and_missing_schema(temp_root):
    store = ConfigStore(temp_root)
    with pytest.raises(ConfigError) as info:
        store.get("nope")
    assert info.value.taxonomy == "CONFIG_UNKNOWN"
    (temp_root / "config" / "config_schema.json").unlink()
    with pytest.raises(ConfigError) as info:
        store.get("health")
    assert info.value.taxonomy == "CONFIG_MISSING" and info.value.file == "config/config_schema.json"
    assert store.check_all()["config_schema"]["ok"] is False


def test_api_recovers_after_config_returns(temp_root, temp_client):
    path = temp_root / "config" / "rollouts.json"
    saved = path.read_text()
    assert temp_client.get("/api/rollouts").status_code == 200
    path.unlink()
    resp = temp_client.get("/api/rollouts")
    assert resp.status_code == 503 and resp.get_json()["error"]["taxonomy"] == "CONFIG_MISSING"
    rewrite(path, saved)
    assert temp_client.get("/api/rollouts").status_code == 200


def test_schema_loss_fails_every_config_endpoint_but_not_liveness(temp_root, temp_client):
    (temp_root / "config" / "config_schema.json").unlink()
    for path in ("/api/ui-config", "/api/workflow", "/api/dreams", "/api/receipts", "/api/task-backends"):
        assert temp_client.get(path).status_code == 503, path
    assert temp_client.get("/healthz").status_code == 200
    assert temp_client.get("/readyz").status_code == 503


def test_app_starts_without_settings_and_serves_structured_503(temp_root, audit_key):
    (temp_root / "config" / "app_settings.json").unlink()
    test_client = create_app(temp_root).test_client()
    assert test_client.get("/healthz").status_code == 200
    for path in ("/api/ui-config", "/api/telemetry", "/api/health"):
        resp = test_client.get(path)
        assert resp.status_code == 503 and resp.get_json()["error"]["file"] == "config/app_settings.json"


def test_unreadable_policy_mid_run_returns_503(temp_root, temp_client):
    assert temp_client.get("/api/dreams").status_code == 200
    rewrite(temp_root / "config" / "dp001_guard_policy.json", '{"versions": ')
    resp = temp_client.get("/api/dreams")
    assert resp.status_code == 503 and resp.get_json()["error"]["taxonomy"] == "CONFIG_MALFORMED"


def test_concurrent_requests_while_config_flaps_never_500(temp_root, temp_app):
    path = temp_root / "config" / "workflow_stages.json"
    good = path.read_text()
    statuses, problems, stop = [], [], threading.Event()

    def reader():
        local = temp_app.test_client()
        while not stop.is_set():
            resp = local.get("/api/workflow")
            body = resp.get_json()
            statuses.append(resp.status_code)
            ok = ("stages" in body) if resp.status_code == 200 else (body.get("error", {}).get("code") == "CONFIG_INVALID")
            if not ok:
                problems.append((resp.status_code, body))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for index in range(40):
        rewrite(path, good if index % 2 else json.dumps({"broken": True}))
    rewrite(path, good)
    stop.set()
    for thread in threads:
        thread.join(timeout=30)
    assert statuses and set(statuses) <= {200, 503} and problems == []
