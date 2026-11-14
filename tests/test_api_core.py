"""Happy-path and edge tests for liveness, readiness, console shell, and read-only API endpoints."""

from __future__ import annotations

import json
import re

import pytest

from tests.helpers import HEX32, edit_json, load_json


def test_index_serves_console_shell_with_security_headers(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Service Release &amp; Operations Console" in html
    assert 'id="theme-toggle"' in html and 'role="tablist"' in html
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert re.match(HEX32, resp.headers["X-Trace-Id"])


@pytest.mark.parametrize("path", ["/healthz", "/health"])
def test_liveness_endpoints_need_no_config(temp_root, audit_key, path):
    from app.app import create_app

    for child in (temp_root / "config").iterdir():
        child.unlink()
    resp = create_app(temp_root).test_client().get(path)
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}
    assert resp.headers["Cache-Control"] == "no-store"


def test_health_alias_matches_healthz(client):
    assert client.get("/health").get_json() == client.get("/healthz").get_json()


def test_ready_alias_matches_readyz(client):
    ready, readyz = client.get("/ready"), client.get("/readyz")
    assert ready.status_code == readyz.status_code == 200
    assert ready.get_json() == readyz.get_json()
    assert ready.headers["Cache-Control"] == "no-store"


def test_readyz_ready_with_audit_key(client):
    resp = client.get("/readyz")
    body = resp.get_json()
    assert resp.status_code == 200 and body["ready"] is True
    assert body["audit_key_configured"] is True
    assert all(entry["ok"] for entry in body["config"].values())
    assert len(body["config"]) == len(load_json("config/config_schema.json")["files"])


def test_readyz_reports_missing_key_as_boolean_only(no_audit_key):
    from app.app import create_app

    resp = create_app().test_client().get("/readyz")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["audit_key_configured"] is False


def test_readyz_never_leaks_key(client, audit_key):
    assert audit_key not in client.get("/readyz").get_data(as_text=True)


def test_readyz_503_when_any_config_is_broken(temp_root, temp_client):
    (temp_root / "config" / "rollouts.json").write_text("{broken", encoding="utf-8")
    resp = temp_client.get("/readyz")
    body = resp.get_json()
    assert resp.status_code == 503 and body["ready"] is False
    assert body["config"]["rollouts"]["taxonomy"] == "CONFIG_MALFORMED"
    assert body["config"]["health"]["ok"] is True


def test_ui_config_comes_from_config_file(client):
    body = client.get("/api/ui-config").get_json()
    settings = load_json("config/app_settings.json")
    assert body["ui"] == settings["ui"]
    assert body["service_name"] == settings["service_name"]
    assert body["revision"] == settings["runtime"]["unset_label"]


def test_ui_config_reports_cloud_run_revision(client, monkeypatch):
    monkeypatch.setenv("K_REVISION", "osprey-core-00042-xyz")
    monkeypatch.setenv("K_SERVICE", "osprey-core")
    body = client.get("/api/ui-config").get_json()
    assert body["revision"] == "osprey-core-00042-xyz"
    assert body["cloud_run_service"] == "osprey-core"


def test_health_reports_insufficient_data_then_healthy(client):
    first = client.get("/api/health").get_json()
    assert first["status"] == "INSUFFICIENT_DATA"
    assert all(slo["state"] == "INSUFFICIENT_DATA" for slo in first["slos"])
    for _ in range(load_json("config/health.json")["min_samples"]):
        assert client.get("/api/rollouts").status_code == 200
    body = client.get("/api/health").get_json()
    assert body["status"] == "HEALTHY"
    ratio = next(slo for slo in body["slos"] if slo["kind"] == "success_ratio")
    latency = next(slo for slo in body["slos"] if slo["kind"] == "latency_percentile")
    assert ratio["measured"] == 1.0 and ratio["failures"] == 0
    assert isinstance(latency["measured_ms"], int) and latency["measured_ms"] >= 0
    assert body["sla"]["target"] == load_json("config/health.json")["sla"]["target"]


def test_health_breaches_when_server_errors_dominate(temp_root, temp_client):
    edit_json(temp_root / "config" / "rollouts.json", lambda doc: doc["rollouts"][0].update(traffic_percent=99))
    for _ in range(6):
        assert temp_client.get("/api/rollouts").status_code == 503
    body = temp_client.get("/api/health").get_json()
    ratio = next(slo for slo in body["slos"] if slo["kind"] == "success_ratio")
    assert ratio["state"] == "BREACHED" and ratio["failures"] >= 6
    assert body["status"] == "BREACHED"


def test_rollouts_payload_is_consistent(client):
    body = client.get("/api/rollouts").get_json()
    assert body["active_traffic_percent"] == body["traffic_total_percent"] == 100
    assert [item["id"] for item in body["rollouts"]] == ["R-104", "R-103", "R-102", "R-101"]


def test_rollouts_traffic_mismatch_fails_closed(temp_root, temp_client):
    edit_json(temp_root / "config" / "rollouts.json", lambda doc: doc["rollouts"][0].update(traffic_percent=20))
    resp = temp_client.get("/api/rollouts")
    err = resp.get_json()["error"]
    assert resp.status_code == 503
    assert err["code"] == "CONFIG_INVALID" and err["taxonomy"] == "CONFIG_SCHEMA"
    assert "105%" in err["detail"]


def test_workflow_statuses_are_derived_from_current_stage(client):
    body = client.get("/api/workflow").get_json()
    stages = body["stages"]
    assert [s["id"] for s in stages] == ["S1_SPEC", "S2_CODE", "S3_TEST", "S4_REVIEW", "S5_RELEASE_GATE", "S6_DEPLOY_CLOSE"]
    assert [s["status"] for s in stages] == ["completed"] * 3 + ["active"] + ["pending"] * 2
    assert [s["mode"] for s in stages] == ["agentic"] * 4 + ["deterministic"] * 2
    assert [s["position"] for s in stages] == list(range(1, 7))
    assert body["mode_label"] == "Hybrid"
    assert all(s["owners"] and s["entry_gates"] and s["exit_gates"] and s["evidence"] for s in stages)


def test_workflow_follows_config_changes(temp_root, temp_client):
    edit_json(temp_root / "config" / "workflow_stages.json", lambda doc: doc.update(current_stage="S6_DEPLOY_CLOSE"))
    stages = temp_client.get("/api/workflow").get_json()["stages"]
    assert [s["status"] for s in stages] == ["completed"] * 5 + ["active"]


def test_workflow_unknown_current_stage_is_503(temp_root, temp_client):
    edit_json(temp_root / "config" / "workflow_stages.json", lambda doc: doc.update(current_stage="S9_UNKNOWN"))
    resp = temp_client.get("/api/workflow")
    assert resp.status_code == 503 and resp.get_json()["error"]["taxonomy"] == "CONFIG_SCHEMA"


REQUIRED_AGENTS = [
    "core-coordinator", "core-arch-subcoord", "core-backend-subcoord", "core-frontend-subcoord", "core-data-api-subcoord",
    "core-qa-release-subcoord", "core-docs-subcoord", "core-planner", "core-spec-writer", "core-api-builder", "core-ui-builder",
    "core-db-builder", "core-chart-builder", "core-rollout-builder", "core-slo-builder", "core-observability-builder",
    "core-grid-worker", "core-budget-calc-worker", "core-tester", "core-reviewer-gemini", "core-reviewer-opus",
    "core-reviewer-fable", "core-iap-security", "core-docs-writer", "core-deployer",
]


def test_agents_roster_has_all_25_unique_agents(client):
    body = client.get("/api/agents").get_json()
    ids = [agent["id"] for agent in body["agents"]]
    assert body["count"] == 25 and len(set(ids)) == 25
    assert sorted(ids) == sorted(REQUIRED_AGENTS)
    owners = {owner for stage in client.get("/api/workflow").get_json()["stages"] for owner in stage["owners"]}
    assert owners <= set(ids)
    assert all(agent["reports_to"] in set(ids) | {"human-operator"} for agent in body["agents"])


def test_telemetry_returns_measured_events_with_trace_ids(client):
    traced = client.get("/api/rollouts")
    body = client.get("/api/telemetry?limit=5").get_json()
    assert body["limit"] == 5 and body["count"] <= 5
    assert body["capacity"] == load_json("config/app_settings.json")["telemetry"]["ring_size"]
    requests = [e for e in body["events"] if e["kind"] == "http_request"]
    assert requests and all(re.match(HEX32, e["trace_id"]) for e in requests)
    assert all(isinstance(e["latency_ms"], int) and e["latency_ms"] >= 0 for e in body["events"])
    assert traced.headers["X-Trace-Id"] in {e["trace_id"] for e in requests}


@pytest.mark.parametrize("limit", ["0", "-1", "abc", "1.5", "999999999"])
def test_telemetry_rejects_bad_limits(client, limit):
    resp = client.get(f"/api/telemetry?limit={limit}")
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "INVALID_LIMIT"


def test_telemetry_default_limit(client):
    for _ in range(3):
        client.get("/healthz")
    body = client.get("/api/telemetry").get_json()
    assert body["limit"] == load_json("config/app_settings.json")["telemetry"]["default_limit"]


def test_request_telemetry_is_json_on_stdout(client, capsys):
    resp = client.get("/api/agents")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    match = [line for line in lines if line.get("trace_id") == resp.headers["X-Trace-Id"] and line["kind"] == "http_request"]
    assert match and match[0]["path"] == "/api/agents" and match[0]["http_status"] == 200
    assert match[0]["severity"] == "INFO" and match[0]["error_taxonomy"] == "NONE"


def test_unknown_api_route_is_structured_404(client):
    resp = client.get("/api/does-not-exist")
    err = resp.get_json()["error"]
    assert resp.status_code == 404 and err["code"] == "NOT_FOUND" and re.match(HEX32, err["trace_id"])


def test_wrong_method_is_structured_405(client):
    resp = client.delete("/api/receipts")
    assert resp.status_code == 405 and resp.get_json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_oversized_body_is_structured_413(client):
    limit = load_json("config/app_settings.json")["server"]["max_request_bytes"]
    resp = client.post("/api/task-backend", data="x" * (limit + 1), content_type="application/json")
    assert resp.status_code == 413 and resp.get_json()["error"]["code"] == "REQUEST_ENTITY_TOO_LARGE"


def test_api_responses_are_not_cached(client):
    assert client.get("/api/workflow").headers["Cache-Control"] == "no-store"
