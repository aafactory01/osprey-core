"""Task backend selection, Night Dreaming replay, and HMAC receipt endpoints (happy path + edge cases)."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re

import pytest

from app.receipts import canonical_json
from tests.helpers import HEX32, HEX64, edit_json

BACKENDS = ["farmtable", "github", "jira", "gitlab", "beads", "linear"]
STAGES = ["S1_SPEC", "S2_TESTS", "S3_BUILD", "S4_VERIFY_QA", "S5_RELEASE_GATE", "S6_DEPLOY_CLOSE"]
KEY_PATTERNS = {
    "farmtable": r"^CORE-T-\d+$", "github": r"^aafactory01/osprey-core#\d+$", "jira": r"^CORE-\d+$",
    "gitlab": r"^platform/osprey-core#\d+$", "beads": r"^bd-[0-9a-f]{4}$", "linear": r"^COR-\d+$",
}


def test_task_backends_registry_lists_six(client):
    body = client.get("/api/task-backends").get_json()
    assert [b["id"] for b in body["backends"]] == BACKENDS
    assert body["default_backend"] == "farmtable" and body["selected"] == "farmtable"
    assert all(b["capabilities"] and b["issue_key_format"] for b in body["backends"])


def test_task_backend_default_mapping(client):
    body = client.get("/api/task-backend").get_json()
    assert body["selected"] == "farmtable" and body["scope"] == "process"
    assert [row["stage"] for row in body["mapping"]] == STAGES
    assert body["mapping"][0]["task_key"] == "CORE-T-101" and body["mapping"][0]["status"] == "PLANNED"


@pytest.mark.parametrize("backend", BACKENDS)
def test_select_each_backend_is_echoed_and_persisted(client, backend):
    resp = client.post("/api/task-backend", json={"backend": backend})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["selected"] == backend and body["requested"] == backend and body["backend"]["id"] == backend
    assert [row["stage"] for row in body["mapping"]] == STAGES
    keys = [row["task_key"] for row in body["mapping"]]
    assert len(set(keys)) == len(STAGES) and all(re.match(KEY_PATTERNS[backend], key) for key in keys)
    assert client.get("/api/task-backend").get_json()["selected"] == backend
    assert client.get("/api/task-backends").get_json()["selected"] == backend


def test_unknown_backend_is_400_and_selection_unchanged(client):
    client.post("/api/task-backend", json={"backend": "jira"})
    resp = client.post("/api/task-backend", json={"backend": "trello"})
    err = resp.get_json()["error"]
    assert resp.status_code == 400 and err["code"] == "UNKNOWN_BACKEND" and err["allowed"] == BACKENDS
    assert client.get("/api/task-backend").get_json()["selected"] == "jira"


@pytest.mark.parametrize("value", [123, None, -0.0, 1e308, [], {}, True, "JIRA", "j\u00edra", "", " jira"])
def test_backend_value_edge_cases_are_400(client, value):
    resp = client.post("/api/task-backend", json={"backend": value})
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "UNKNOWN_BACKEND"


@pytest.mark.parametrize("raw,code", [
    ("", "EMPTY_BODY"), ("   ", "EMPTY_BODY"), ("{", "MALFORMED_JSON"), ("[1, 2]", "BODY_NOT_OBJECT"),
    ('{"backend": NaN}', "MALFORMED_JSON"), ('{"backend": 1e400}', "NON_FINITE_NUMBER"),
    ("\xff\xfe", "MALFORMED_JSON"), ("[" * 5000 + "]" * 5000, "BODY_NOT_OBJECT"),
    ("[" * 30000 + "]" * 30000, "JSON_TOO_DEEP"),
], ids=["empty", "blank", "truncated", "array", "nan", "overflow", "binary", "deep-array", "too-deep"])
def test_malformed_bodies_are_structured_400(client, raw, code):
    resp = client.post("/api/task-backend", data=raw.encode("latin-1"), content_type="application/json")
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == code


@pytest.mark.parametrize("path,body", [
    ("/api/task-backend", '{"backend": "jira"}'), ("/api/receipts/verify", '{"chain": []}'), ("/api/dreams/replay", "{}"),
])
@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", None],
                         ids=["text", "form", "missing"])
def test_non_json_content_type_is_structured_415(client, path, body, content_type):
    kwargs = {"content_type": content_type} if content_type else {}
    resp = client.post(path, data=body, **kwargs)
    err = resp.get_json()["error"]
    assert resp.status_code == 415 and err["code"] == "UNSUPPORTED_MEDIA_TYPE" and "application/json" in err["message"]


def test_json_with_charset_is_accepted(client):
    resp = client.post("/api/task-backend", data='{"backend": "linear"}', content_type="application/json; charset=utf-8")
    assert resp.status_code == 200 and resp.get_json()["selected"] == "linear"


def test_ui_fetch_sends_json_content_type():
    from app.config_store import REPO_ROOT

    api_js = (REPO_ROOT / "app" / "static" / "js" / "api.js").read_text()
    assert "init.headers['Content-Type'] = 'application/json';" in api_js
    callers = "".join((REPO_ROOT / "app" / "static" / "js" / name).read_text() for name in ("backends.js", "dreams.js", "receipts.js"))
    assert callers.count("method: 'POST'") == callers.count("method: 'POST', body:") == 4


def test_dreams_scorecard_is_computed_live(client):
    body = client.get("/api/dreams").get_json()
    replay = body["replay"]
    assert (replay["baseline"]["passed"], replay["baseline"]["total"], replay["baseline"]["pass_rate"]) == (6, 10, 0.6)
    assert (replay["candidate"]["passed"], replay["candidate"]["total"], replay["candidate"]["pass_rate"]) == (10, 10, 1.0)
    assert replay["baseline"]["pass_rate_label"] == "60%" and replay["candidate"]["pass_rate_label"] == "100%"
    assert (replay["baseline"]["false_allows"], replay["baseline"]["false_blocks"]) == (3, 1)
    failing = sorted(case["id"] for case in replay["cases"] if not case["baseline"]["pass"])
    assert failing == ["SR-03", "SR-05", "SR-08", "SR-09"]
    assert all(case["candidate"]["pass"] for case in replay["cases"])
    assert replay["gate"]["verdict"] == "PASS" and replay["status"] == "ACTIVE"
    assert replay["rule_diff"]["modified"] == ["R-RM-RF"] and "R-RUN-DEPLOY-PUBLIC" in replay["rule_diff"]["added"]
    assert body["proposal"]["id"] == "DP-001" and body["proposal"]["skill"] == "ops-release-guardrails"
    assert body["nightly"]["present"] and body["nightly"]["consistent_with_live"] is True
    assert isinstance(body["latency_ms"], int) and isinstance(replay["replay_latency_us"], int)


def test_dreams_replay_post_recomputes_with_new_trace(client):
    first = client.get("/api/dreams").get_json()
    resp = client.post("/api/dreams/replay", json={})
    second = resp.get_json()
    assert resp.status_code == 200 and re.match(HEX32, second["trace_id"])
    assert second["trace_id"] != first["trace_id"]
    assert second["replay"]["candidate"]["pass_rate_label"] == "100%"


def test_dreams_numbers_follow_the_policy_not_a_constant(temp_root, temp_client):
    def drop_public_rules(doc):
        rules = doc["versions"]["1.1.0"]["rules"]
        doc["versions"]["1.1.0"]["rules"] = [r for r in rules if r["id"] not in ("R-RUN-DEPLOY-PUBLIC", "R-RUN-DEPLOY-NO-IAP")]

    edit_json(temp_root / "config" / "dp001_guard_policy.json", drop_public_rules)
    replay = temp_client.get("/api/dreams").get_json()["replay"]
    assert replay["candidate"]["passed"] == 9 and replay["candidate"]["false_allows"] == 1
    assert replay["gate"]["verdict"] == "FAIL" and replay["status"] == "REJECTED_GATE_FAILED"
    assert temp_client.get("/api/dreams").get_json()["nightly"]["consistent_with_live"] is False


def test_dreams_missing_eval_set_fails_closed(temp_root, temp_client):
    (temp_root / "dreamstore" / "evals" / "ops_release_guardrails_shadow_replay.json").unlink()
    resp = temp_client.get("/api/dreams")
    assert resp.status_code == 503 and resp.get_json()["error"]["taxonomy"] == "CONFIG_MISSING"


def test_dreams_eval_path_mismatch_fails_closed(temp_root, temp_client):
    edit_json(temp_root / "config" / "night_dreaming.json", lambda doc: doc.update(eval_set_path="dreamstore/evals/other.json"))
    resp = temp_client.post("/api/dreams/replay", json={})
    assert resp.status_code == 503 and "eval_set_path" in resp.get_json()["error"]["detail"]


def test_dreams_invalid_policy_regex_fails_closed(temp_root, temp_client):
    edit_json(temp_root / "config" / "dp001_guard_policy.json",
              lambda doc: doc["versions"]["1.1.0"]["rules"][0].update(patterns=["(unclosed"]))
    resp = temp_client.get("/api/dreams")
    assert resp.status_code == 503 and "invalid regex" in resp.get_json()["error"]["detail"]


def test_receipts_chain_is_signed_and_linked(client, audit_key):
    resp = client.get("/api/receipts")
    body = resp.get_json()
    assert resp.status_code == 200 and body["count"] == 7 and body["chain_valid"] is True
    assert body["algorithm"] == "HMAC-SHA256" and body["key_id"].startswith("k-")
    assert audit_key not in resp.get_data(as_text=True)
    prev = body["genesis_prev_signature"]
    for seq, item in enumerate(body["receipts"], start=1):
        assert item["receipt"]["seq"] == seq and item["receipt"]["prev_signature"] == prev
        assert re.match(HEX64, item["signature"]) and item["short_signature"] == item["signature"][:12]
        expected = hmac.new(audit_key.encode(), canonical_json(item["receipt"]), hashlib.sha256).hexdigest()
        assert item["signature"] == expected
        prev = item["signature"]


def test_receipts_fail_closed_without_key(no_audit_key):
    from app.app import create_app

    test_client = create_app().test_client()
    for resp in (test_client.get("/api/receipts"), test_client.post("/api/receipts/verify", json={"chain": []})):
        err = resp.get_json()["error"]
        assert resp.status_code == 503 and err["code"] == "AUDIT_KEY_NOT_CONFIGURED"
        assert "audit key not configured" in err["message"]


def test_receipts_reject_short_key(monkeypatch):
    from app.app import create_app

    monkeypatch.setenv("AUDIT_HMAC_KEY", "too-short")
    resp = create_app().test_client().get("/api/receipts")
    assert resp.status_code == 503 and "at least" in resp.get_json()["error"]["message"]


def _items(client):
    return client.get("/api/receipts").get_json()["receipts"]


def test_verify_single_receipt_passes(client):
    item = _items(client)[3]
    body = client.post("/api/receipts/verify", json={"receipt": item["receipt"], "signature": item["signature"]}).get_json()
    assert body["verdict"] == "PASS" and body["checks"] == {"signature": True, "chain_link": True}
    assert body["receipt_id"] == "RCPT-0004" and body["mode"] == "receipt"


@pytest.mark.parametrize("field,value", [
    ("decision", "PASS"), ("timestamp", "2026-11-13T15:31:49Z"), ("seq", 4.0), ("seq", -0.0),
    ("evidence", "Two chaos tests failed \u2014 edited"), ("actor", "core-deployer"), ("extra", 10 ** 30),
])
def test_tampered_receipt_fails(client, field, value):
    item = _items(client)[3]
    tampered = copy.deepcopy(item["receipt"])
    tampered[field] = value
    body = client.post("/api/receipts/verify", json={"receipt": tampered, "signature": item["signature"]}).get_json()
    assert body["verdict"] == "FAIL" and body["checks"]["signature"] is False


def test_tampered_prev_link_fails_link_check(client):
    items = _items(client)
    tampered = dict(items[2]["receipt"], prev_signature=items[0]["signature"])
    body = client.post("/api/receipts/verify", json={"receipt": tampered, "signature": items[2]["signature"]}).get_json()
    assert body["verdict"] == "FAIL" and body["checks"]["chain_link"] is False


def test_signature_from_another_key_fails(client):
    item = _items(client)[0]
    forged = hmac.new(b"x" * 64, canonical_json(item["receipt"]), hashlib.sha256).hexdigest()
    body = client.post("/api/receipts/verify", json={"receipt": item["receipt"], "signature": forged}).get_json()
    assert body["verdict"] == "FAIL"


@pytest.mark.parametrize("signature", [123, None, "", "zz" * 32, "\u00e9" * 64, ["a"]])
def test_odd_signatures_fail_without_crashing(client, signature):
    item = _items(client)[0]
    resp = client.post("/api/receipts/verify", json={"receipt": item["receipt"], "signature": signature})
    assert resp.status_code == 200 and resp.get_json()["verdict"] == "FAIL"


def test_verify_full_chain_passes(client):
    chain = [{"receipt": i["receipt"], "signature": i["signature"]} for i in _items(client)]
    body = client.post("/api/receipts/verify", json={"chain": chain}).get_json()
    assert body["verdict"] == "PASS" and body["mode"] == "chain" and body["broken_at"] is None


@pytest.mark.parametrize("mutation,broken_at", [("drop", 2), ("swap", 1), ("tamper", 4)])
def test_broken_chain_is_detected(client, mutation, broken_at):
    chain = [{"receipt": i["receipt"], "signature": i["signature"]} for i in _items(client)]
    if mutation == "drop":
        del chain[2]
    elif mutation == "swap":
        chain[1], chain[2] = chain[2], chain[1]
    else:
        chain[4]["receipt"] = dict(chain[4]["receipt"], decision="BLOCK")
    body = client.post("/api/receipts/verify", json={"chain": chain}).get_json()
    assert body["verdict"] == "FAIL" and body["broken_at"] == broken_at


@pytest.mark.parametrize("payload", [{}, {"receipt": {}}, {"signature": "a"}, {"chain": "nope"}, {"chain": [1, 2]}])
def test_verify_rejects_or_fails_bad_shapes(client, payload):
    resp = client.post("/api/receipts/verify", json=payload)
    if "chain" in payload:
        assert resp.status_code == 200 and resp.get_json()["verdict"] == "FAIL"
    else:
        assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "VERIFY_BODY_INVALID"


def test_signatures_depend_on_the_key(client, monkeypatch):
    first = client.get("/api/receipts").get_json()
    monkeypatch.setenv("AUDIT_HMAC_KEY", "another-test-key-" + "0" * 40)
    second = client.get("/api/receipts").get_json()
    assert first["key_id"] != second["key_id"]
    assert first["receipts"][0]["signature"] != second["receipts"][0]["signature"]
    stale = first["receipts"][0]
    body = client.post("/api/receipts/verify", json={"receipt": stale["receipt"], "signature": stale["signature"]}).get_json()
    assert body["verdict"] == "FAIL"
    assert json.dumps(second).count("another-test-key") == 0
