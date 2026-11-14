"""Unit tests for .workflow/task_backend.py and app/receipts.py (registry, formats, HMAC chain primitives)."""

from __future__ import annotations

import threading

import pytest

from app.config_store import ConfigStore
from app.errors import AuditKeyError
from app.receipts import build_chain, canonical_json, key_fingerprint, load_key, verify_chain, verify_receipt
from app.workflow_service import load_task_backend_module
from tests.helpers import TEST_HMAC_KEY

tb = load_task_backend_module()
STAGES = ["S1_SPEC", "S2_CODE", "S3_TEST", "S4_REVIEW", "S5_RELEASE_GATE", "S6_DEPLOY_CLOSE"]


@pytest.fixture
def backend_cfg():
    return ConfigStore().get("task_backends")


@pytest.fixture
def registry(backend_cfg):
    return tb.TaskBackendRegistry(backend_cfg, STAGES)


def test_registry_ids_and_default(registry):
    assert registry.ids() == ["farmtable", "github", "jira", "gitlab", "beads", "linear"]
    assert registry.default_backend == "farmtable"


@pytest.mark.parametrize("backend,first,last", [
    ("farmtable", "CORE-T-101", "CORE-T-106"), ("github", "aafactory01/osprey-core#41", "aafactory01/osprey-core#46"),
    ("jira", "CORE-210", "CORE-215"), ("gitlab", "platform/osprey-core#77", "platform/osprey-core#82"),
    ("linear", "COR-330", "COR-335"),
])
def test_sequence_keys(registry, backend, first, last):
    rows = registry.map_workflow(backend)
    assert [row["stage"] for row in rows] == STAGES
    assert rows[0]["task_key"] == first and rows[-1]["task_key"] == last


def test_beads_keys_are_deterministic_hashes(registry):
    keys = [row["task_key"] for row in registry.map_workflow("beads")]
    assert keys == [row["task_key"] for row in registry.map_workflow("beads")]
    assert len(set(keys)) == 6 and all(key.startswith("bd-") and len(key) == 7 for key in keys)


def test_native_statuses_follow_config(registry, backend_cfg):
    for backend in registry.ids():
        rows = registry.map_workflow(backend)
        assert [row["status"] for row in rows] == [backend_cfg["backends"][backend]["stage_map"][s]["status"] for s in STAGES]


@pytest.mark.parametrize("fmt", ["{seq.__class__}", "{0}", "{}", "{unknown}", "{project_key[0]}", "{seq", "seq}"])
def test_unsafe_or_broken_formats_are_rejected(backend_cfg, fmt):
    backend_cfg["backends"]["jira"]["issue_key_format"] = fmt
    with pytest.raises(tb.TaskBackendError):
        tb.TaskBackendRegistry(backend_cfg, STAGES)


def test_missing_stage_mapping_is_rejected(backend_cfg):
    del backend_cfg["backends"]["linear"]["stage_map"]["S4_REVIEW"]
    with pytest.raises(tb.TaskBackendError, match="S4_REVIEW"):
        tb.TaskBackendRegistry(backend_cfg, STAGES)


def test_unknown_default_backend_is_rejected(backend_cfg):
    backend_cfg["default_backend"] = "trello"
    with pytest.raises(tb.TaskBackendError, match="default_backend"):
        tb.TaskBackendRegistry(backend_cfg, STAGES)


def test_empty_stage_list_is_rejected(backend_cfg):
    with pytest.raises(tb.TaskBackendError):
        tb.TaskBackendRegistry(backend_cfg, [])


@pytest.mark.parametrize("value", ["trello", "", None, 3, ["jira"]])
def test_unknown_backend_lookup(registry, value):
    with pytest.raises(tb.UnknownBackendError):
        registry.get(value)


def test_selector_defaults_persists_and_is_thread_safe(registry):
    selector = tb.TaskBackendSelector()
    assert selector.selected(registry) == "farmtable"
    assert selector.select(registry, "gitlab") == "gitlab" and selector.selected(registry) == "gitlab"
    errors = []

    def worker(backend):
        try:
            for _ in range(50):
                selector.select(registry, backend)
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in registry.ids()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors and selector.selected(registry) in registry.ids()


@pytest.fixture
def audit_cfg():
    return ConfigStore().get("audit_receipts")


def test_canonical_json_is_order_independent_and_compact():
    assert canonical_json({"b": 1, "a": "\u00e9"}) == canonical_json({"a": "\u00e9", "b": 1}) == '{"a":"\u00e9","b":1}'.encode()
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_load_key_requires_a_long_enough_secret(audit_cfg):
    with pytest.raises(AuditKeyError, match="not configured"):
        load_key(audit_cfg, {})
    with pytest.raises(AuditKeyError, match="at least"):
        load_key(audit_cfg, {"AUDIT_HMAC_KEY": "short"})
    assert load_key(audit_cfg, {"AUDIT_HMAC_KEY": TEST_HMAC_KEY}) == TEST_HMAC_KEY.encode()


def test_fingerprint_never_contains_the_key():
    key = TEST_HMAC_KEY.encode()
    fingerprint = key_fingerprint(key, 16)
    assert fingerprint.startswith("k-") and len(fingerprint) == 18 and TEST_HMAC_KEY[:16] not in fingerprint


def test_chain_is_deterministic_and_linked(audit_cfg):
    key = TEST_HMAC_KEY.encode()
    chain = build_chain(audit_cfg, key)
    assert chain == build_chain(audit_cfg, key)
    assert chain[0]["receipt"]["prev_signature"] == "GENESIS"
    assert all(chain[i]["receipt"]["prev_signature"] == chain[i - 1]["signature"] for i in range(1, len(chain)))
    assert verify_chain(audit_cfg, key, chain)["valid"] is True
    assert build_chain(audit_cfg, b"k" * 40)[0]["signature"] != chain[0]["signature"]


@pytest.mark.parametrize("items", [[], "chain", [1], [{"receipt": "x", "signature": "y"}], None])
def test_verify_chain_bad_shapes_fail(audit_cfg, items):
    result = verify_chain(audit_cfg, TEST_HMAC_KEY.encode(), items)
    assert result["verdict"] == "FAIL" and result["valid"] is False


@pytest.mark.parametrize("receipt", ["text", None, {"seq": True}, {"seq": 99}, {"seq": 10 ** 400}, {"x": -0.0}])
def test_verify_receipt_odd_payloads_fail_cleanly(audit_cfg, receipt):
    result = verify_receipt(audit_cfg, TEST_HMAC_KEY.encode(), receipt, "0" * 64)
    assert result["verdict"] == "FAIL"
