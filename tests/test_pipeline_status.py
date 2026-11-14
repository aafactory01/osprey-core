"""PipelineStatusService: provenance, live mapping, newest-rollout choice, cache TTL, and token reuse (no network)."""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from app import telemetry
from app.config_store import ConfigError, ConfigStore
from app.pipeline_model import PipelineError
from tests.helpers import edit_json
from tests.pipeline_fakes import (
    BUILD_ID, BUILD_URL_PART, CFG, CICD, DIGEST, ENV_NAMES, FAKE_TOKEN, PIPELINE, RELEASE, ROLLOUTS_URL_PART, SHORT_SHA,
    STAGES, TARGET, TOKEN_OK, TOKEN_URL_PART, FakeClock, build_doc, default_routes, google_error, make_service, ok,
    provenance_env, provenance_values, rollout,
)

CI_KEYS = {"provider", "stage", "status", "build_id", "image_tag", "image_digest", "create_time", "finish_time", "duration_ms",
           "steps", "console_url", "source", "error", "latency_ms"}
CD_KEYS = {"provider", "stage", "pipeline", "target", "release", "rollout", "rollout_state", "deploy_start_time",
           "deploy_end_time", "console_url", "source", "error", "latency_ms"}
PROVENANCE_KEYS = {"commit_sha", "short_sha", "build_id", "image_digest", "release", "pipeline", "target", "revision",
                   "service", "deployed_by_cloud_deploy"}
NOT_DEPLOYED = CFG["labels"]["not_deployed"]
TTL_S = CFG["cache"]["ttl_ms"] / 1000
ERROR_TTL_S = CFG["cache"]["error_ttl_ms"] / 1000


def test_happy_path_maps_build_and_newest_rollout():
    service, transport, _ = make_service()
    doc = service.status()
    assert set(doc) == {"source_label", "stage_ids", "provenance", "ci", "cd", "fetched_at", "cache_age_ms"}
    assert doc["stage_ids"] == {"release_gate": STAGES["release_gate"], "deploy_close": STAGES["deploy_close"]}
    assert doc["source_label"] == CFG["source_label"] and doc["cache_age_ms"] == 0 and doc["fetched_at"].endswith("Z")
    ci, cd = doc["ci"], doc["cd"]
    assert CI_KEYS <= set(ci) and CD_KEYS <= set(cd)
    assert (ci["provider"], ci["stage"], ci["status"], ci["tone"]) == (CICD["ci"]["provider"], STAGES["release_gate"], "SUCCESS", "ok")
    assert (ci["source"], ci["source_label"], ci["error"]) == ("live", CFG["ci"]["live_label"], None)
    assert (ci["build_id"], ci["image_tag"], ci["image_digest"], ci["digest_matches_provenance"]) == (BUILD_ID, SHORT_SHA, DIGEST, True)
    assert (ci["create_time"], ci["finish_time"], ci["duration_ms"]) == ("2026-11-14T20:00:00.123Z", "2026-11-14T20:03:17.500Z", 192500)
    assert ci["steps"] == [
        {"id": "pytest", "status": "SUCCESS", "tone": "ok", "duration_ms": 60000},
        {"id": "gcr.io/cloud-builders/docker", "status": "SUCCESS", "tone": "ok", "duration_ms": 120000},
        {"id": "step-3", "status": "QUEUED", "tone": "warn", "duration_ms": None},
    ]
    assert ci["bounce_to"] == [STAGES["build"], STAGES["verify"]]
    assert ci["console_url"] == CFG["ci"]["console_url_template"].format(
        region=CICD["region"], build_id=BUILD_ID, project=CICD["project_id"])
    assert isinstance(ci["latency_ms"], int) and ci["latency_ms"] >= 0
    assert (cd["provider"], cd["stage"], cd["pipeline"], cd["target"]) == (CICD["cd"]["provider"], STAGES["deploy_close"], PIPELINE, TARGET)
    assert (cd["release"], cd["rollout"], cd["rollout_state"], cd["tone"]) == (RELEASE, "rollout-new", "SUCCEEDED", "ok")
    assert (cd["deploy_start_time"], cd["deploy_end_time"]) == ("2026-11-14T20:05:00.000Z", "2026-11-14T20:06:30.250Z")
    assert (cd["source"], cd["source_label"], cd["error"]) == ("live", CFG["cd"]["live_label"], None)
    assert cd["control_loop"] == [CFG["cd"]["control_loop"][0]["label"], CFG["cd"]["control_loop"][1]["label"], STAGES["build"]]
    assert isinstance(cd["latency_ms"], int)
    assert doc["provenance"] == {**provenance_values(), "deployed_by_cloud_deploy": True}
    assert transport.count(TOKEN_URL_PART) == transport.count(BUILD_URL_PART) == transport.count(ROLLOUTS_URL_PART) == 1


def test_outbound_calls_carry_the_right_headers_urls_and_timeouts():
    service, transport, _ = make_service()
    service.status()
    by_part = {part: next(call for call in transport.calls if part in call["url"])
               for part in (TOKEN_URL_PART, BUILD_URL_PART, ROLLOUTS_URL_PART)}
    meta = CFG["metadata"]
    assert by_part[TOKEN_URL_PART]["headers"] == {meta["header_name"]: meta["header_value"]}
    assert by_part[TOKEN_URL_PART]["url"].startswith(f"http://{meta['default_host']}/")
    for part in (BUILD_URL_PART, ROLLOUTS_URL_PART):
        assert by_part[part]["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert by_part[part]["url"].startswith("https://") and CICD["project_id"] in by_part[part]["url"]
        assert 0 < by_part[part]["timeout_s"] <= CFG["http"]["call_timeout_ms"] / 1000
        assert by_part[part]["max_bytes"] == CFG["http"]["max_response_bytes"]
    assert by_part[BUILD_URL_PART]["url"].endswith(f"/builds/{BUILD_ID}")
    assert f"/deliveryPipelines/{PIPELINE}/releases/{RELEASE}/rollouts?pageSize={CFG['cd']['page_size']}" in by_part[ROLLOUTS_URL_PART]["url"]


def test_no_provenance_is_not_configured_and_never_calls_out():
    service, transport, _ = make_service(env={})
    doc = service.status()
    assert transport.calls == []
    assert doc["provenance"] == {key: None for key in ENV_NAMES} | {"deployed_by_cloud_deploy": False}
    for block, state_key in ((doc["ci"], "status"), (doc["cd"], "rollout_state")):
        assert block[state_key] == "UNKNOWN" and block["tone"] == "neutral"
        assert (block["source"], block["source_label"], block["error"], block["latency_ms"]) == ("not_configured", NOT_DEPLOYED, None, None)
        assert block["console_url"] is None
    assert doc["ci"]["steps"] == [] and doc["ci"]["bounce_to"] == [STAGES["build"], STAGES["verify"]]
    assert doc["stage_ids"]["release_gate"] == STAGES["release_gate"]


def test_missing_build_id_keeps_cd_live_and_names_the_variable():
    service, transport, _ = make_service(env=provenance_env(build_id=None))
    doc = service.status()
    assert doc["ci"]["source"] == "not_configured" and doc["ci"]["status"] == "UNKNOWN"
    assert doc["ci"]["source_label"] == CFG["labels"]["missing_env"].format(env_var=ENV_NAMES["build_id"])
    assert doc["cd"]["source"] == "live" and transport.count(BUILD_URL_PART) == 0


@pytest.mark.parametrize("unset", ["release", "pipeline", "target"])
def test_any_missing_cloud_deploy_parameter_means_not_deployed(unset):
    service, transport, _ = make_service(env=provenance_env(**{unset: None}))
    doc = service.status()
    assert doc["provenance"]["deployed_by_cloud_deploy"] is False
    assert doc["cd"]["source"] == "not_configured" and doc["cd"]["source_label"] == NOT_DEPLOYED
    assert doc["ci"]["source"] == "live" and transport.count(ROLLOUTS_URL_PART) == 0


def test_blank_environment_values_are_null():
    service, _, _ = make_service(env=provenance_env(build_id="   ", commit_sha=""))
    doc = service.status()
    assert doc["provenance"]["build_id"] is None and doc["provenance"]["commit_sha"] is None
    assert doc["ci"]["source"] == "not_configured"


def test_manifest_placeholder_values_are_null_not_provenance():
    placeholder = CFG["provenance_unset_values"][0]
    service, transport, _ = make_service(env=provenance_env(build_id=placeholder, release=placeholder.upper()))
    doc = service.status()
    assert doc["provenance"]["build_id"] is None and doc["provenance"]["release"] is None
    assert doc["provenance"]["deployed_by_cloud_deploy"] is False and transport.calls == []


@pytest.mark.parametrize("payload", [{}, {"rollouts": []}, {"rollouts": [rollout("x", "2026-11-14T20:00:00Z", target="elsewhere")]}])
def test_no_rollout_for_the_target_is_not_found(payload):
    service, _, _ = make_service(default_routes(rollouts=ok(payload)))
    cd = service.status()["cd"]
    assert (cd["rollout_state"], cd["source"], cd["error"]["taxonomy"]) == ("UNKNOWN", "unavailable", "NOT_FOUND")
    assert TARGET in cd["error"]["detail"] and cd["source_label"] == "Unavailable: NOT_FOUND"


def test_newest_rollout_for_target_wins_over_newer_other_targets():
    payload = {"rollouts": [rollout("a", "2026-11-14T20:00:00Z", state="FAILED"), rollout("b", "2026-11-14T20:00:00.000001Z"),
                            rollout("c", "2026-11-14T23:00:00Z", target="elsewhere", state="FAILED"),
                            rollout("d", "garbage", state="FAILED"), "not-an-object"]}
    service, _, _ = make_service(default_routes(rollouts=ok(payload)))
    cd = service.status()["cd"]
    assert (cd["rollout"], cd["rollout_state"]) == ("b", "SUCCEEDED")


def test_failed_rollout_reports_failure_reason_and_crit_tone():
    payload = {"rollouts": [rollout("z", "2026-11-14T20:00:00Z", state="FAILED", failureReason="CLOUD_BUILD_REQUEST_FAILED")]}
    service, _, _ = make_service(default_routes(rollouts=ok(payload)))
    cd = service.status()["cd"]
    assert (cd["rollout_state"], cd["tone"], cd["failure_reason"], cd["source"]) == ("FAILED", "crit", "CLOUD_BUILD_REQUEST_FAILED", "live")


@pytest.mark.parametrize("status,tone", [("FAILURE", "crit"), ("WORKING", "warn"), ("QUEUED", "warn"), ("TIMEOUT", "crit"),
                                         ("SOMETHING_NEW", CFG["default_tone"])])
def test_build_status_tones_come_from_config(status, tone):
    service, _, _ = make_service(default_routes(build=ok(build_doc(status, statusDetail="step 2 failed"))))
    ci = service.status()["ci"]
    assert (ci["status"], ci["tone"], ci["source"]) == (status, tone, "live")
    assert ci["status_detail"] == "step 2 failed"


def test_digest_mismatch_and_missing_images_are_reported_not_guessed():
    other = "sha256:" + "cd" * 32
    service, _, _ = make_service(default_routes(build=ok(build_doc(results={"images": [{"name": f"{CICD['ci']['image_repository']}:x", "digest": other}]}))))
    assert service.status()["ci"]["digest_matches_provenance"] is False
    service, _, _ = make_service(default_routes(build=ok(build_doc(results={}, steps="nope", startTime=None))))
    ci = service.status()["ci"]
    assert (ci["image_tag"], ci["image_digest"], ci["digest_matches_provenance"], ci["steps"], ci["duration_ms"]) == (None, None, None, [], None)


def test_cache_ttl_is_honored_and_cache_age_is_measured():
    clock = FakeClock()
    service, transport, _ = make_service(clock=clock)
    first = service.status()
    clock.advance(TTL_S / 2)
    second = service.status()
    assert transport.count(BUILD_URL_PART) == 1 and second["cache_age_ms"] == round(TTL_S / 2 * 1000)
    assert second["fetched_at"] == first["fetched_at"]
    clock.advance(TTL_S)
    third = service.status()
    assert transport.count(BUILD_URL_PART) == 2 and third["cache_age_ms"] == 0


def test_failures_use_the_shorter_error_ttl():
    clock = FakeClock()
    service, transport, _ = make_service(default_routes(build=ok({}, 503)), clock=clock)
    assert service.status()["ci"]["source"] == "unavailable"
    clock.advance(ERROR_TTL_S)
    service.status()
    assert transport.count(BUILD_URL_PART) == 2 and ERROR_TTL_S < TTL_S


def test_cache_is_keyed_by_provenance_and_returns_copies():
    service, transport, _ = make_service()
    doc = service.status()
    doc["ci"]["steps"].clear()
    assert len(service.status()["ci"]["steps"]) == 3 and transport.count(BUILD_URL_PART) == 1


def test_token_is_reused_until_expiry_minus_margin():
    clock = FakeClock()
    service, transport, _ = make_service(clock=clock)
    service.status()
    clock.advance(TTL_S + 1)
    service.status()
    assert transport.count(TOKEN_URL_PART) == 1 and transport.count(BUILD_URL_PART) == 2
    clock.advance(3599 - CFG["metadata"]["refresh_margin_ms"] / 1000)
    service.status()
    assert transport.count(TOKEN_URL_PART) == 2


def test_token_without_expiry_is_not_cached():
    clock = FakeClock()
    service, transport, _ = make_service(default_routes(token=ok({"access_token": FAKE_TOKEN})), clock=clock)
    service.status()
    clock.advance(TTL_S + 1)
    service.status()
    assert transport.count(TOKEN_URL_PART) == 2


def test_metadata_host_override_and_path_encoding():
    env = provenance_env(build_id="a/b?c") | {CFG["metadata"]["host_env_var"]: "127.0.0.1:9"}
    service, transport, _ = make_service(env=env)
    service.status()
    assert any(call["url"].startswith("http://127.0.0.1:9/") for call in transport.calls)
    assert any(call["url"].endswith("/builds/a%2Fb%3Fc") for call in transport.calls)


def test_concurrent_requests_share_one_refresh():
    def slow_build(_url):
        time.sleep(0.05)
        return ok(build_doc())

    service, transport, _ = make_service(default_routes(build=slow_build))
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.status()["ci"]["status"])) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert results == ["SUCCESS"] * 8 and transport.count(BUILD_URL_PART) == 1


def test_every_outbound_call_emits_telemetry_with_the_bound_trace_id():
    buffer = io.StringIO()
    telemetry.use_stream(buffer)
    token = telemetry.bind_trace_id("feedface" * 4)
    try:
        make_service(default_routes(rollouts=ok({}, 403)))[0].status()
    finally:
        telemetry.reset_trace_id(token)
    records = [json.loads(line) for line in buffer.getvalue().splitlines()]
    outbound = {record["op"]: record for record in records if record.get("kind") == "outbound"}
    assert set(outbound) == {"pipeline.metadata_token", "pipeline.cloud_build.get_build", "pipeline.cloud_deploy.list_rollouts"}
    assert all(r["trace_id"] == "feedface" * 4 and isinstance(r["latency_ms"], int) for r in outbound.values())
    assert outbound["pipeline.cloud_build.get_build"]["status"] == "ok"
    denied = outbound["pipeline.cloud_deploy.list_rollouts"]
    assert (denied["status"], denied["error_taxonomy"], denied["http_status"]) == ("error", "PERMISSION_DENIED", 403)
    assert FAKE_TOKEN not in buffer.getvalue()
    refresh = [r for r in records if r.get("op") == "pipeline.refresh"]
    assert refresh and refresh[0]["cd_source"] == "unavailable" and refresh[0]["status"] == "error"


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d.pop("cache"), "cache: required key missing"),
    (lambda d: d["ci"].update(build_url_template="https://x/{nope}"), "bad template field"),
    (lambda d: d["ci"].update(bounce_to_roles=["no_such_role"]), "no_such_role"),
    (lambda d: d["cd"].update(control_loop=[{"label": "a", "stage_role": "build"}]), "exactly one of"),
    (lambda d: d["ci"]["status_tones"].update(SUCCESS="green"), "is not one of"),
    (lambda d: d["http"].update(budget_ms=0), "below minimum"),
])
def test_invalid_pipeline_config_fails_closed(temp_root, mutate, fragment):
    edit_json(temp_root / "config" / "pipeline_status.json", mutate)
    service, transport, _ = make_service(store=ConfigStore(temp_root))
    with pytest.raises(ConfigError) as info:
        service.status()
    assert info.value.taxonomy == "CONFIG_SCHEMA" and fragment in info.value.detail


# --- Chaos: every upstream failure degrades to status UNKNOWN with the right taxonomy; nothing raises. ---

def _expected_taxonomy(status):
    return CFG["taxonomy"]["by_http_status"].get(str(status), CFG["taxonomy"]["default_http"])


@pytest.mark.parametrize("status,taxonomy", [(401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED"), (404, "NOT_FOUND"),
                                             (429, "RATE_LIMITED"), (500, "UNAVAILABLE"), (503, "UNAVAILABLE"),
                                             (504, "TIMEOUT"), (418, CFG["taxonomy"]["default_http"])])
def test_build_http_errors_degrade_ci_only(status, taxonomy):
    service, _, _ = make_service(default_routes(build=google_error(status, f"upstream said {status}")))
    doc = service.status()
    ci = doc["ci"]
    assert taxonomy == _expected_taxonomy(status)
    assert (ci["status"], ci["tone"], ci["source"]) == ("UNKNOWN", "neutral", "unavailable")
    assert ci["error"] == {"taxonomy": taxonomy, "http_status": status, "detail": f"upstream said {status}"}
    assert ci["source_label"] == f"Unavailable: {taxonomy}"
    assert ci["build_id"] == BUILD_ID and ci["console_url"] and ci["steps"] == [] and ci["image_digest"] is None
    assert doc["cd"]["source"] == "live"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_rollout_http_errors_degrade_cd_only(status):
    service, _, _ = make_service(default_routes(rollouts=ok({"unexpected": True}, status)))
    doc = service.status()
    cd = doc["cd"]
    assert (cd["rollout_state"], cd["source"], cd["rollout"]) == ("UNKNOWN", "unavailable", None)
    assert cd["error"] == {"taxonomy": _expected_taxonomy(status), "http_status": status, "detail": f"HTTP {status}"}
    assert doc["ci"]["source"] == "live"


@pytest.mark.parametrize("raised,taxonomy", [
    (PipelineError("TIMEOUT", "timed out: read timeout"), "TIMEOUT"),
    (PipelineError("UNAVAILABLE", "connection failed: refused"), "UNAVAILABLE"),
    (ConnectionResetError("peer reset"), "UNAVAILABLE"),
    (RuntimeError("transport bug"), "UNAVAILABLE"),
])
def test_transport_failures_are_typed(raised, taxonomy):
    service, _, _ = make_service(default_routes(build=raised, rollouts=raised))
    doc = service.status()
    for block in (doc["ci"], doc["cd"]):
        assert block["source"] == "unavailable" and block["error"]["taxonomy"] == taxonomy and block["error"]["http_status"] is None


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b'{"status": NaN}', b"\xff\xfe", b'{"id": "x"}', b'{"status": 7}'])
def test_malformed_build_payloads(body):
    service, _, _ = make_service(default_routes(build=(200, body)))
    ci = service.status()["ci"]
    assert (ci["status"], ci["source"], ci["error"]["taxonomy"]) == ("UNKNOWN", "unavailable", "MALFORMED_RESPONSE")


@pytest.mark.parametrize("payload", [[], {"rollouts": "nope"}, {"rollouts": [rollout("x", "2026-11-14T20:00:00Z", state=None)]}])
def test_malformed_rollout_payloads(payload):
    service, _, _ = make_service(default_routes(rollouts=ok(payload)))
    cd = service.status()["cd"]
    assert (cd["rollout_state"], cd["error"]["taxonomy"]) == ("UNKNOWN", "MALFORMED_RESPONSE")


@pytest.mark.parametrize("token_route,cause", [
    (PipelineError("UNAVAILABLE", "connection failed: [Errno 111] Connection refused"), "UNAVAILABLE"),
    (PipelineError("TIMEOUT", "timed out"), "TIMEOUT"),
    (ok({"error": "no service account"}, 404), "NOT_FOUND"),
    (ok({}, 500), "UNAVAILABLE"),
    ((200, b"<html>"), "MALFORMED_RESPONSE"),
])
def test_metadata_server_down_means_no_credentials_for_both_blocks(token_route, cause):
    service, transport, _ = make_service(default_routes(token=token_route))
    doc = service.status()
    for block in (doc["ci"], doc["cd"]):
        assert (block["source"], block["error"]["taxonomy"]) == ("unavailable", "NO_CREDENTIALS")
        assert block["error"]["detail"].startswith(f"{cause}: ") and block["source_label"] == "Unavailable: NO_CREDENTIALS"
    assert transport.count(BUILD_URL_PART) == transport.count(ROLLOUTS_URL_PART) == 0


def test_token_payload_without_access_token_is_no_credentials():
    service, _, _ = make_service(default_routes(token=ok({"expires_in": 3599})))
    assert service.status()["ci"]["error"]["taxonomy"] == "NO_CREDENTIALS"


def test_exhausted_budget_times_out_without_calling():
    clock = FakeClock()

    def slow_token(_url):
        clock.advance(CFG["http"]["budget_ms"] / 1000 + 1)
        return TOKEN_OK

    service, transport, _ = make_service(default_routes(token=slow_token), clock=clock)
    doc = service.status()
    assert doc["ci"]["error"]["taxonomy"] == doc["cd"]["error"]["taxonomy"] == "TIMEOUT"
    assert transport.count(BUILD_URL_PART) == transport.count(ROLLOUTS_URL_PART) == 0


def test_unexpected_mapping_failure_degrades_instead_of_raising(monkeypatch):
    from app import pipeline_status

    monkeypatch.setattr(pipeline_status, "map_build", lambda *args: {}["boom"])
    doc = make_service()[0].status()
    assert doc["ci"]["error"] == {"taxonomy": "UNAVAILABLE", "http_status": None, "detail": "unexpected KeyError"}
    assert doc["cd"]["source"] == "live"


def test_oversized_and_wrongly_shaped_transport_results_are_typed():
    service, _, _ = make_service(default_routes(build=PipelineError("MALFORMED_RESPONSE", "response is larger than 1 bytes"),
                                                rollouts=lambda _url: "not-a-tuple"))
    doc = service.status()
    assert doc["ci"]["error"]["taxonomy"] == "MALFORMED_RESPONSE" and doc["cd"]["error"]["taxonomy"] == "UNAVAILABLE"
