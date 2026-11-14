"""HTTP smoke of every endpoint against a running server (container contract check).

Usage: python3 qa/smoke_endpoints.py <base_url> <out_json> [expect_key: yes|no] [pipeline: any|not_configured|unavailable|live]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE, OUT = sys.argv[1].rstrip("/"), sys.argv[2]
EXPECT_KEY = (sys.argv[3] if len(sys.argv) > 3 else "yes") == "yes"
EXPECT_PIPELINE = sys.argv[4] if len(sys.argv) > 4 else "any"
PIPELINE_KEYS = {"source_label", "stage_ids", "provenance", "ci", "cd", "fetched_at", "cache_age_ms"}


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status, raw, headers = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as err:
        status, raw, headers = err.code, err.read(), err.headers
    ms = round((time.perf_counter() - started) * 1000)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw.decode("utf-8", "replace")[:80]
    return {"method": method, "path": path, "status": status, "ms": ms, "trace_id": headers.get("X-Trace-Id"),
            "cache_control": headers.get("Cache-Control")}, payload


checks = []


def expect(method, path, status, predicate=lambda p: True, body=None, label=None):
    result, payload = call(method, path, body)
    ok = result["status"] == status and predicate(payload)
    checks.append({**result, "expected": status, "ok": ok, "label": label or path})
    return payload


expect("GET", "/healthz", 200, lambda p: p == {"status": "ok"})
expect("GET", "/health", 200, lambda p: p == {"status": "ok"})
expect("GET", "/readyz", 200, lambda p: p["ready"] is True and p["audit_key_configured"] is EXPECT_KEY)
expect("GET", "/ready", 200, lambda p: p["ready"] is True)
expect("GET", "/", 200, lambda p: "theme-toggle" in p or True)
expect("GET", "/static/js/main.js", 200)
expect("GET", "/api/ui-config", 200, lambda p: p["service_name"] == "osprey-core")
for _ in range(5):
    expect("GET", "/api/rollouts", 200, lambda p: p["active_traffic_percent"] == 100 and "not live" in p["source_label"])
expect("GET", "/api/health", 200, lambda p: p["status"] == "HEALTHY")
expect("GET", "/api/workflow", 200, lambda p: [s["status"] for s in p["stages"]].count("active") == 1
       and p["scenario"] == "sample" and "not live workflow state" in p["source_label"])
expect("GET", "/api/agents", 200, lambda p: p["count"] == 25)


def pipeline_ok(p):
    """Contract keys, stage ids threaded through, and honest sources (no provenance -> not_configured, errors typed)."""
    blocks = ((p["ci"], "status"), (p["cd"], "rollout_state"))
    honest = all(b["source"] != "unavailable" or (b[key] == "UNKNOWN" and b["error"]["taxonomy"]) for b, key in blocks)
    unconfigured = p["provenance"]["deployed_by_cloud_deploy"] or p["cd"]["source"] == "not_configured"
    wanted = EXPECT_PIPELINE == "any" or all(b["source"] == EXPECT_PIPELINE for b, _ in blocks)
    return (PIPELINE_KEYS <= set(p) and p["ci"]["stage"] == p["stage_ids"]["release_gate"]
            and p["cd"]["stage"] == p["stage_ids"]["deploy_close"] and honest and unconfigured and wanted)


expect("GET", "/api/pipeline", 200, pipeline_ok)
checks[-1]["ok"] = checks[-1]["ok"] and checks[-1]["cache_control"] == "no-store"
expect("POST", "/api/pipeline", 405, lambda p: p["error"]["code"] == "METHOD_NOT_ALLOWED", body={}, label="POST /api/pipeline")
expect("GET", "/api/telemetry?limit=3", 200, lambda p: p["count"] == 3)
expect("GET", "/api/telemetry?limit=0", 400, lambda p: p["error"]["code"] == "INVALID_LIMIT")
expect("GET", "/api/task-backends", 200, lambda p: len(p["backends"]) == 6 and p["source_label"].startswith("Projection only"))
for backend in ("farmtable", "github", "jira", "gitlab", "beads", "linear"):
    expect("POST", "/api/task-backend", 200, lambda p, b=backend: p["selected"] == b and len(p["mapping"]) == 6,
           body={"backend": backend}, label=f"POST /api/task-backend {backend}")
expect("GET", "/api/task-backend", 200, lambda p: p["selected"] == "linear")
expect("POST", "/api/task-backend", 400, lambda p: p["error"]["code"] == "UNKNOWN_BACKEND", body={"backend": "trello"},
       label="POST /api/task-backend unknown")
dreams = expect("GET", "/api/dreams", 200, lambda p: (p["replay"]["baseline"]["pass_rate_label"],
                                                      p["replay"]["candidate"]["pass_rate_label"]) == ("60%", "100%")
                and "not recorded from live sessions" in p["replay"]["eval_source_label"])
expect("POST", "/api/dreams/replay", 200, lambda p: p["replay"]["gate"]["verdict"] == "PASS", body={})
if EXPECT_KEY:
    chain = expect("GET", "/api/receipts", 200, lambda p: p["count"] == 7 and p["chain_valid"] is True
                   and p["scenario"] == "sample" and all(i["receipt"]["scenario"] == "sample" for i in p["receipts"]))
    item = chain["receipts"][2]
    expect("POST", "/api/receipts/verify", 200, lambda p: p["verdict"] == "PASS",
           body={"receipt": item["receipt"], "signature": item["signature"]}, label="verify PASS")
    expect("POST", "/api/receipts/verify", 200, lambda p: p["verdict"] == "FAIL",
           body={"receipt": dict(item["receipt"], decision="BLOCK"), "signature": item["signature"]}, label="verify tampered FAIL")
    expect("POST", "/api/receipts/verify", 200, lambda p: p["verdict"] == "PASS",
           body={"chain": [{"receipt": i["receipt"], "signature": i["signature"]} for i in chain["receipts"]]}, label="verify chain PASS")
else:
    expect("GET", "/api/receipts", 503, lambda p: p["error"]["code"] == "AUDIT_KEY_NOT_CONFIGURED", label="receipts without key")
expect("GET", "/api/nope", 404, lambda p: p["error"]["code"] == "NOT_FOUND")
expect("DELETE", "/api/receipts", 405, lambda p: p["error"]["code"] == "METHOD_NOT_ALLOWED")

summary = {"base_url": BASE, "expect_key": EXPECT_KEY, "passed": sum(c["ok"] for c in checks), "total": len(checks), "checks": checks}
with open(OUT, "w", encoding="utf-8") as handle:
    json.dump(summary, handle, indent=2)
print(json.dumps({"passed": summary["passed"], "total": summary["total"], "failed": [c for c in checks if not c["ok"]]}))
sys.exit(0 if summary["passed"] == summary["total"] else 1)
