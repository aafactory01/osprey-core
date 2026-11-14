"""GET /api/pipeline through the Flask app: contract shape, headers, telemetry, and never-500 chaos (no network)."""

from __future__ import annotations

import re
import threading

import pytest

from app import telemetry
from app.pipeline_model import PipelineError
from app.pipeline_status import PipelineStatusService
from app.routes_pipeline import EXTENSION_KEY
from tests.helpers import HEX32, rewrite
from tests.pipeline_fakes import (
    CFG, ENV_NAMES, STAGES, FakeClock, FakeTransport, build_doc, default_routes, google_error, ok, provenance_env,
)

TOP_KEYS = {"source_label", "stage_ids", "provenance", "ci", "cd", "fetched_at", "cache_age_ms"}
CI_KEYS = {"provider", "stage", "status", "build_id", "image_tag", "image_digest", "create_time", "finish_time", "duration_ms",
           "steps", "console_url", "source", "error", "latency_ms"}
CD_KEYS = {"provider", "stage", "pipeline", "target", "release", "rollout", "rollout_state", "deploy_start_time",
           "deploy_end_time", "console_url", "source", "error", "latency_ms"}
PROVENANCE_KEYS = {"commit_sha", "short_sha", "build_id", "image_digest", "release", "pipeline", "target", "revision",
                   "service", "deployed_by_cloud_deploy"}


@pytest.fixture(autouse=True)
def _no_provenance_env(monkeypatch):
    for var in ENV_NAMES.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(CFG["metadata"]["host_env_var"], raising=False)


def install(flask_app, routes, env=None):
    transport = FakeTransport(routes)
    store = flask_app.extensions["console.store"]
    flask_app.extensions[EXTENSION_KEY] = PipelineStatusService(store, transport=transport, clock=FakeClock(), env=env)
    return transport


def test_blueprint_installs_the_default_service(app):
    assert isinstance(app.extensions[EXTENSION_KEY], PipelineStatusService)
    assert any(rule.rule == "/api/pipeline" for rule in app.url_map.iter_rules())


def test_default_document_is_honestly_not_configured(client):
    resp = client.get("/api/pipeline")
    body = resp.get_json()
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "no-store"
    assert re.match(HEX32, resp.headers["X-Trace-Id"])
    assert set(body) == TOP_KEYS and CI_KEYS <= set(body["ci"]) and CD_KEYS <= set(body["cd"])
    assert set(body["provenance"]) == PROVENANCE_KEYS and body["provenance"]["deployed_by_cloud_deploy"] is False
    assert body["stage_ids"] == {"release_gate": STAGES["release_gate"], "deploy_close": STAGES["deploy_close"]}
    assert (body["ci"]["status"], body["ci"]["source"]) == ("UNKNOWN", "not_configured")
    assert (body["cd"]["rollout_state"], body["cd"]["source"]) == ("UNKNOWN", "not_configured")
    assert body["ci"]["source_label"] == body["cd"]["source_label"] == CFG["labels"]["not_deployed"]
    text = resp.get_data(as_text=True)
    assert "NaN" not in text and "Infinity" not in text


def test_live_document_reads_provenance_from_the_process_environment(app, client, monkeypatch):
    for var, value in provenance_env().items():
        monkeypatch.setenv(var, value)
    transport = install(app, default_routes())
    body = client.get("/api/pipeline").get_json()
    assert body["provenance"]["deployed_by_cloud_deploy"] is True
    assert (body["ci"]["status"], body["ci"]["source"]) == ("SUCCESS", "live")
    assert body["cd"]["rollout_state"] == "SUCCEEDED" and body["cd"]["source"] == "live"
    assert len(transport.calls) == 3


def test_outbound_telemetry_shares_the_request_trace_id(app, client):
    install(app, default_routes(build=google_error(404, "Requested entity was not found.")), env=provenance_env())
    resp = client.get("/api/pipeline")
    trace_id = resp.headers["X-Trace-Id"]
    events = telemetry.RING.snapshot()
    outbound = [event for event in events if event.get("kind") == "outbound"]
    assert len(outbound) == 3 and all(event["trace_id"] == trace_id for event in outbound)
    failed = [event for event in outbound if event["status"] == "error"]
    assert [(event["op"], event["error_taxonomy"], event["http_status"]) for event in failed] == [
        ("pipeline.cloud_build.get_build", "NOT_FOUND", 404)]
    request_events = [event for event in events if event.get("kind") == "http_request" and event["path"] == "/api/pipeline"]
    assert request_events and request_events[-1]["http_status"] == 200 and request_events[-1]["trace_id"] == trace_id


CHAOS = {
    "unauthenticated": ({"build": google_error(401, "bad token"), "rollouts": google_error(401, "bad token")}, "UNAUTHENTICATED"),
    "permission_denied": ({"build": ok({}, 403), "rollouts": ok({}, 403)}, "PERMISSION_DENIED"),
    "not_found": ({"build": ok({}, 404), "rollouts": ok({}, 404)}, "NOT_FOUND"),
    "rate_limited": ({"build": ok({}, 429), "rollouts": ok({}, 429)}, "RATE_LIMITED"),
    "unavailable": ({"build": ok({}, 503), "rollouts": ok({}, 503)}, "UNAVAILABLE"),
    "timeout": ({"build": PipelineError("TIMEOUT", "timed out"), "rollouts": PipelineError("TIMEOUT", "timed out")}, "TIMEOUT"),
    "malformed": ({"build": (200, b"{oops"), "rollouts": (200, b"[]")}, "MALFORMED_RESPONSE"),
    "metadata_down": ({"token": PipelineError("UNAVAILABLE", "connection refused")}, "NO_CREDENTIALS"),
    "metadata_500": ({"token": ok({}, 500)}, "NO_CREDENTIALS"),
    "transport_bug": ({"build": RuntimeError("bug"), "rollouts": KeyError("bug")}, "UNAVAILABLE"),
}


@pytest.mark.parametrize("name", sorted(CHAOS))
def test_chaos_answers_200_with_a_degraded_document(app, client, name):
    overrides, taxonomy = CHAOS[name]
    install(app, default_routes(**overrides), env=provenance_env())
    resp = client.get("/api/pipeline")
    body = resp.get_json()
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "no-store"
    for block, state_key in ((body["ci"], "status"), (body["cd"], "rollout_state")):
        assert block[state_key] == "UNKNOWN" and block["source"] == "unavailable"
        assert block["error"]["taxonomy"] == taxonomy and block["source_label"] == f"Unavailable: {taxonomy}"
    assert body["provenance"]["deployed_by_cloud_deploy"] is True and set(body) == TOP_KEYS


def test_concurrent_chaos_requests_never_500(app):
    install(app, default_routes(build=[ok({}, 503), ok(build_doc()), (200, b"{x")], rollouts=RuntimeError("bug")),
            env=provenance_env())
    statuses, problems = [], []

    def worker():
        local = app.test_client()
        for _ in range(5):
            resp = local.get("/api/pipeline")
            statuses.append(resp.status_code)
            if resp.status_code != 200 or resp.get_json()["cd"]["source"] != "unavailable":
                problems.append(resp.get_data(as_text=True)[:200])

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert statuses and set(statuses) == {200} and problems == []


@pytest.mark.parametrize("relpath,text,taxonomy", [
    ("config/pipeline_status.json", "{broken", "CONFIG_MALFORMED"),
    ("config/pipeline_status.json", '{"schema_version": "1.0.0", "source_label": "x"}', "CONFIG_SCHEMA"),
    ("config/cicd.json", "[]", "CONFIG_SCHEMA"),
])
def test_invalid_config_is_a_structured_503_never_a_500(temp_root, temp_client, relpath, text, taxonomy):
    rewrite(temp_root / relpath, text)
    resp = temp_client.get("/api/pipeline")
    body = resp.get_json()
    assert resp.status_code == 503 and body["error"]["code"] == "CONFIG_INVALID"
    assert body["error"]["taxonomy"] == taxonomy and body["error"]["file"] == relpath
    assert resp.headers["Cache-Control"] == "no-store"


def test_missing_config_recovers_when_restored(temp_root, temp_client):
    path = temp_root / "config" / "pipeline_status.json"
    saved = path.read_text(encoding="utf-8")
    path.unlink()
    assert temp_client.get("/api/pipeline").status_code == 503
    rewrite(path, saved)
    assert temp_client.get("/api/pipeline").status_code == 200


def test_only_get_is_allowed(client):
    resp = client.post("/api/pipeline", json={})
    assert resp.status_code == 405 and resp.get_json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_readiness_covers_the_pipeline_config(client):
    body = client.get("/readyz").get_json()
    assert body["config"]["pipeline_status"] == {"ok": True} and body["config"]["cicd"] == {"ok": True}
