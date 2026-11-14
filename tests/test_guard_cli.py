"""DP-001 hook as a real PreToolUse process: exit codes, stdout JSON, stderr reasons, and chaos on stdin/policy."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time

import pytest

from app.config_store import REPO_ROOT
from dreamstore.hooks.dp001_pre_tool_guard import HookExit, main
from tests.helpers import HEX32

HOOK = REPO_ROOT / "dreamstore" / "hooks" / "dp001_pre_tool_guard.py"
DECISION_KEYS = {"decision", "rule_id", "reason", "policy_version", "trace_id", "latency_ms"}


def run_hook(stdin_bytes, env_extra=None, timeout=20):
    env = {k: v for k, v in os.environ.items() if k not in ("WORKFLOW_STAGE", "DP001_POLICY_VERSION", "DP001_POLICY_PATH")}
    env.update(env_extra or {})
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, str(HOOK)], input=stdin_bytes, capture_output=True, env=env, timeout=timeout,
                          cwd=str(REPO_ROOT), check=False)
    payload = json.loads(proc.stdout.decode("utf-8"))
    return proc.returncode, payload, proc.stderr.decode("utf-8")


def event(command, stage=None, tool="Bash"):
    body = {"tool_name": tool, "tool_input": {"command": command}}
    if stage:
        body["stage"] = stage
    return json.dumps(body).encode("utf-8")


def policy_copy(tmp_path, mutate):
    doc = json.loads((REPO_ROOT / "config" / "dp001_guard_policy.json").read_text(encoding="utf-8"))
    mutate(doc)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_protocol_exit_codes():
    assert int(HookExit.ALLOW) == 0 and int(HookExit.BLOCK) == 2


def test_allow_exits_zero_with_decision_json():
    code, payload, stderr = run_hook(event("ls -la"))
    assert code == 0 and set(payload) == DECISION_KEYS
    assert payload["decision"] == "allow" and payload["policy_version"] == "1.1.0"
    assert re.match(HEX32, payload["trace_id"]) and isinstance(payload["latency_ms"], int)
    assert stderr == ""


def test_block_exits_two_with_reason_on_stderr():
    code, payload, stderr = run_hook(event("git push --force origin main"))
    assert code == 2 and payload["decision"] == "block" and payload["rule_id"] == "R-GIT-FORCE-PUSH-MAIN"
    assert "BLOCKED by R-GIT-FORCE-PUSH-MAIN" in stderr and "rewrites shared history" in stderr


@pytest.mark.parametrize("raw,rule_id", [
    (b"", "FAIL_CLOSED_EMPTY_INPUT"),
    (b"   \n", "FAIL_CLOSED_EMPTY_INPUT"),
    (b"{not json", "FAIL_CLOSED_MALFORMED_INPUT"),
    (b"[1, 2, 3]", "FAIL_CLOSED_MALFORMED_INPUT"),
    (b'{"tool_name": "Bash", "tool_input": {"command": NaN}}', "FAIL_CLOSED_MALFORMED_INPUT"),
    (b"\xff\xfe\xfd", "FAIL_CLOSED_MALFORMED_INPUT"),
    (b'{"tool_name": "Bash"}', "FAIL_CLOSED_MALFORMED_EVENT"),
])
def test_bad_stdin_fails_closed(raw, rule_id):
    code, payload, stderr = run_hook(raw)
    assert code == 2 and payload["rule_id"] == rule_id and stderr


def test_missing_policy_fails_closed(tmp_path):
    code, payload, _ = run_hook(event("ls"), {"DP001_POLICY_PATH": str(tmp_path / "gone.json")})
    assert code == 2 and payload["rule_id"] == "FAIL_CLOSED_POLICY_UNAVAILABLE" and payload["policy_version"] == "unknown"


def test_corrupt_policy_fails_closed(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text('{"versions": ', encoding="utf-8")
    code, payload, _ = run_hook(event("ls"), {"DP001_POLICY_PATH": str(bad)})
    assert code == 2 and payload["rule_id"] == "FAIL_CLOSED_POLICY_UNAVAILABLE"


def test_semantically_invalid_policy_fails_closed(tmp_path):
    path = policy_copy(tmp_path, lambda d: d["versions"]["1.1.0"]["rules"][0].update(patterns=["(oops"]))
    code, payload, _ = run_hook(event("ls"), {"DP001_POLICY_PATH": path})
    assert code == 2 and "invalid regex" in payload["reason"]


def test_version_override_selects_baseline():
    public = event("gcloud run deploy osprey-core --source . --allow-unauthenticated", "S6_DEPLOY_CLOSE")
    assert run_hook(public)[0] == 2
    code, payload, _ = run_hook(public, {"DP001_POLICY_VERSION": "1.0.0"})
    assert code == 0 and payload["policy_version"] == "1.0.0"


def test_unknown_version_override_fails_closed():
    code, payload, _ = run_hook(event("ls"), {"DP001_POLICY_VERSION": "7.0.0"})
    assert code == 2 and payload["rule_id"] == "FAIL_CLOSED_POLICY_UNAVAILABLE"


def test_stage_comes_from_environment_when_event_has_none():
    deploy = event("gcloud run deploy osprey-core --source . --iap")
    assert run_hook(deploy, {"WORKFLOW_STAGE": "S6_DEPLOY_CLOSE"})[0] == 0
    assert run_hook(deploy, {"WORKFLOW_STAGE": "S3_TEST"})[0] == 2
    assert run_hook(deploy)[1]["rule_id"] == "R-DEPLOY-BEFORE-RELEASE-GATE"


def test_event_stage_wins_over_environment():
    deploy = event("gcloud run deploy osprey-core --source . --iap", stage="S2_CODE")
    assert run_hook(deploy, {"WORKFLOW_STAGE": "S6_DEPLOY_CLOSE"})[0] == 2


def test_oversized_stdin_fails_closed(tmp_path):
    path = policy_copy(tmp_path, lambda d: d["io"].update(max_stdin_bytes=64))
    code, payload, _ = run_hook(event("echo " + "a" * 200), {"DP001_POLICY_PATH": path})
    assert code == 2 and payload["rule_id"] == "FAIL_CLOSED_INPUT_TOO_LARGE"


def test_slow_stdin_times_out_and_blocks(tmp_path):
    path = policy_copy(tmp_path, lambda d: d["io"].update(stdin_timeout_seconds=0.3))
    env = dict(os.environ, DP001_POLICY_PATH=path, PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    proc = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    proc.stdin.write(b'{"tool_name": "Bash", ')
    proc.stdin.flush()
    try:
        proc.wait(timeout=20)
    finally:
        proc.stdin.close()
    out, err = proc.stdout.read(), proc.stderr.read()
    elapsed = time.monotonic() - started
    payload = json.loads(out.decode("utf-8"))
    assert proc.returncode == 2 and payload["rule_id"] == "FAIL_CLOSED_INPUT_TIMEOUT"
    assert elapsed < 10 and b"BLOCKED" in err


def test_trickled_stdin_within_deadline_is_accepted():
    proc = subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    for chunk in (b'{"tool_name": "Bash", ', b'"tool_input": {"command": ', b'"ls"}}'):
        proc.stdin.write(chunk)
        proc.stdin.flush()
        time.sleep(0.05)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 0 and json.loads(out)["decision"] == "allow"


def test_in_process_main_with_text_streams():
    out, err = io.StringIO(), io.StringIO()
    code = main(stdin=io.StringIO('{"tool_name": "Bash", "tool_input": {"command": "kubectl get secrets"}}'),
                stdout=out, stderr=err, environ={})
    assert code == 2 and json.loads(out.getvalue())["rule_id"] == "R-KUBECTL-SECRETS"
    assert "BLOCKED" in err.getvalue()


def test_closed_stdin_is_an_internal_error_that_still_blocks():
    closed = io.StringIO("{}")
    closed.close()
    out = io.StringIO()
    code = main(stdin=closed, stdout=out, stderr=io.StringIO(), environ={})
    assert code == 2 and json.loads(out.getvalue())["rule_id"] == "FAIL_CLOSED_INTERNAL_ERROR"


def test_broken_stdout_still_returns_the_exit_code():
    broken = io.StringIO()
    broken.close()
    err = io.StringIO()
    code = main(stdin=io.StringIO('{"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}'),
                stdout=broken, stderr=err, environ={})
    assert code == 2 and "stdout unavailable" in err.getvalue()
