"""Shadow replay engine and the Night Dreaming evolver script (computed scorecards, regressions, fail-closed)."""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys

import pytest

from app.config_store import REPO_ROOT, ConfigStore
from dreamstore.replay import ReplayError, proposal_status, run_shadow_replay, validate_eval_set
from scripts import nightly_dream_evolver as evolver
from tests.helpers import edit_json

EVOLVER = REPO_ROOT / "scripts" / "nightly_dream_evolver.py"


@pytest.fixture(scope="module")
def inputs():
    store = ConfigStore()
    return store.get("dp001_guard_policy"), store.get("shadow_replay_evals"), store.get("night_dreaming")


def test_replay_scores_baseline_60_and_candidate_100(inputs):
    report = run_shadow_replay(*inputs)
    assert (report["baseline"]["passed"], report["candidate"]["passed"], report["baseline"]["total"]) == (6, 10, 10)
    assert report["baseline"]["pass_rate_label"] == "60%" and report["candidate"]["pass_rate_label"] == "100%"
    assert report["gate"]["verdict"] == "PASS" and all(check["pass"] for check in report["gate"]["checks"])
    assert report["status"] == "ACTIVE" and report["active_version"] == "1.1.0"
    rules = {case["id"]: (case["baseline"]["rule_id"], case["candidate"]["rule_id"]) for case in report["cases"]}
    assert rules["SR-03"] == ("ALLOW_NO_RULE_MATCHED", "R-RUN-DEPLOY-PUBLIC")
    assert rules["SR-05"] == ("R-RM-RF", "ALLOW_NO_RULE_MATCHED")
    assert rules["SR-08"] == ("ALLOW_NO_RULE_MATCHED", "R-PROTECTED-DREAM-ARTIFACTS")
    assert rules["SR-09"] == ("ALLOW_NO_RULE_MATCHED", "R-DEPLOY-BEFORE-RELEASE-GATE")
    assert isinstance(report["replay_latency_us"], int) and report["replay_latency_us"] >= 0


def test_eval_set_is_balanced_and_not_trivial(inputs):
    _, evals, _ = inputs
    labels = [case["expected"] for case in evals["cases"]]
    assert len(labels) == 10 and labels.count("allow") >= 3 and labels.count("block") >= 3
    report = run_shadow_replay(*inputs)
    assert report["baseline"]["false_allows"] == 3 and report["baseline"]["false_blocks"] == 1


def test_rule_diff_is_computed_from_policy(inputs):
    diff = run_shadow_replay(*inputs)["rule_diff"]
    assert diff["removed"] == [] and diff["modified"] == ["R-RM-RF"]
    assert set(diff["added"]) == {"R-RUN-DEPLOY-PUBLIC", "R-RUN-PUBLIC-IAM", "R-RUN-DEPLOY-NO-IAP",
                                  "R-DEPLOY-BEFORE-RELEASE-GATE", "R-CI-BEFORE-RELEASE-GATE", "R-CD-BEFORE-DEPLOY-CLOSE",
                                  "R-SHELL-PROTECTED-PATH-WRITE", "R-PROTECTED-DREAM-ARTIFACTS"}


def test_eval_events_use_workflow_stage_ids(inputs):
    policy, evals, _ = inputs
    stages = [case["event"]["stage"] for case in evals["cases"]]
    assert set(stages) <= set(policy["stage_order"]) and len(stages) == 10


def test_swapped_versions_fail_the_gate(inputs):
    policy, evals, dreaming = copy.deepcopy(inputs)
    dreaming["proposal"].update(from_version="1.1.0", to_version="1.0.0")
    report = run_shadow_replay(policy, evals, dreaming)
    assert report["gate"]["verdict"] == "FAIL" and report["status"] == "REJECTED_GATE_FAILED"


def test_status_is_staged_when_candidate_not_active(inputs):
    policy, _, _ = inputs
    assert proposal_status(dict(policy, active_version="1.0.0"), "1.1.0", "PASS") == "STAGED_AWAITING_HUMAN_GATE"
    assert proposal_status(policy, "1.1.0", "FAIL") == "REJECTED_GATE_FAILED"


@pytest.mark.parametrize("doc", [
    {}, {"cases": []}, {"cases": [{"id": "A", "expected": "allow", "event": {}}, {"id": "A", "expected": "allow", "event": {}}]},
    {"cases": [{"id": "A", "expected": "maybe", "event": {}}]}, {"cases": [{"id": "A", "expected": "block", "event": "x"}]},
    {"cases": ["not a case"]},
])
def test_invalid_eval_sets_raise(doc):
    with pytest.raises(ReplayError):
        validate_eval_set(doc)


def run_evolver(args):
    out = io.StringIO()
    code = evolver.main(args, stdout=out)
    return code, json.loads(out.getvalue())


def test_evolver_writes_computed_artifacts(tmp_path):
    code, summary = run_evolver(["--out-root", str(tmp_path), "--date", "2026-11-14", "--write"])
    assert code == 0 and summary["status"] == "ok" and summary["gate"] == "PASS"
    assert (summary["baseline"], summary["candidate"], summary["proposal_status"]) == ("60%", "100%", "ACTIVE")
    scorecard = json.loads((tmp_path / "dreamstore" / "scorecard.json").read_text())
    proposal = json.loads((tmp_path / "dreamstore" / "proposals" / "DP-001.json").read_text())
    synthesis = (tmp_path / "team-memory" / "dreams" / "2026-11-14-nightly-dream-synthesis.md").read_text()
    assert scorecard["baseline"]["passed"] == 6 and scorecard["candidate"]["passed"] == 10 and len(scorecard["cases"]) == 10
    assert scorecard["trace_id"] == summary["trace_id"]
    assert proposal["status"] == "ACTIVE" and proposal["scorecard"] == {"baseline": "60%", "candidate": "100%"}
    assert "Baseline v1.0.0: 60% (6/10)" in synthesis and "Candidate v1.1.0: 100% (10/10)" in synthesis
    assert summary["written"][-1] == "team-memory/dreams/2026-11-14-nightly-dream-synthesis.md"


def test_evolver_without_write_skips_markdown(tmp_path):
    code, summary = run_evolver(["--out-root", str(tmp_path)])
    assert code == 0 and len(summary["written"]) == 2
    assert not (tmp_path / "team-memory").exists()


def test_evolver_exits_nonzero_on_regression(temp_root, tmp_path):
    edit_json(temp_root / "config" / "night_dreaming.json",
              lambda doc: doc["proposal"].update(from_version="1.1.0", to_version="1.0.0"))
    code, summary = run_evolver(["--config-root", str(temp_root), "--out-root", str(tmp_path)])
    assert code == 1 and summary["status"] == "regression" and summary["gate"] == "FAIL"
    assert json.loads((tmp_path / "dreamstore" / "scorecard.json").read_text())["gate"]["verdict"] == "FAIL"


def test_evolver_exits_nonzero_when_gate_fails_without_regression(temp_root, tmp_path):
    # Same version on both sides: 6/10 vs 6/10 is no regression, but 3 missed blocks fail the gate.
    edit_json(temp_root / "config" / "night_dreaming.json",
              lambda doc: doc["proposal"].update(from_version="1.0.0", to_version="1.0.0"))
    code, summary = run_evolver(["--config-root", str(temp_root), "--out-root", str(tmp_path)])
    assert code == 1 and summary["status"] == "gate_failed" and summary["gate"] == "FAIL"
    assert (summary["baseline"], summary["candidate"]) == ("60%", "60%")


def test_evolver_config_error_writes_nothing(temp_root, tmp_path):
    (temp_root / "config" / "dp001_guard_policy.json").unlink()
    code, summary = run_evolver(["--config-root", str(temp_root), "--out-root", str(tmp_path)])
    assert code == 1 and summary["status"] == "error" and summary["error_taxonomy"] == "CONFIG_MISSING"
    assert summary["written"] == [] and not (tmp_path / "dreamstore").exists()


def test_evolver_invalid_policy_is_reported(temp_root, tmp_path):
    edit_json(temp_root / "config" / "dp001_guard_policy.json", lambda doc: doc.update(active_version="4.0.0"))
    code, summary = run_evolver(["--config-root", str(temp_root), "--out-root", str(tmp_path)])
    assert code == 1 and summary["error_taxonomy"] == "POLICY_INVALID"


def test_evolver_runs_as_a_standalone_script(tmp_path):
    proc = subprocess.run([sys.executable, str(EVOLVER), "--out-root", str(tmp_path)], capture_output=True, text=True,
                          timeout=60, check=False, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["candidate"] == "100%" and proc.stdout.count("\n") == 1


def test_committed_scorecard_matches_live_replay(inputs):
    committed = json.loads((REPO_ROOT / "dreamstore" / "scorecard.json").read_text())
    live = run_shadow_replay(*inputs)
    for side in ("baseline", "candidate"):
        assert committed[side]["passed"] == live[side]["passed"]
        assert committed[side]["pass_rate_label"] == live[side]["pass_rate_label"]
    proposal = json.loads((REPO_ROOT / "dreamstore" / "proposals" / "DP-001.json").read_text())
    assert proposal["status"] == live["status"] and proposal["rule_diff"] == live["rule_diff"]
