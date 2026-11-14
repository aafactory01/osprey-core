"""Night Dreaming shadow replay: grade guard policy versions on recorded, labelled tool calls.

The scorecard is computed, never asserted: every case is re-evaluated with the real DP-001
`evaluate()` under the baseline and the candidate policy versions, and the pass rates, false
allows (missed blocks), and false blocks (needless friction) are counted from those decisions.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, Mapping

from dreamstore.hooks.dp001_policy import Decision, diff_rules, evaluate, resolve_policy, validate_policy_document

_ONE_MS = timedelta(milliseconds=1)
_ONE_US = timedelta(microseconds=1)
GATE_PASS = "PASS"
GATE_FAIL = "FAIL"


class ReplayError(Exception):
    """The eval set or gate configuration cannot produce a trustworthy scorecard."""

    taxonomy = "REPLAY_INVALID"


def _side(decision: Decision, expected: str) -> dict[str, Any]:
    return {
        "decision": decision.decision,
        "rule_id": decision.rule_id,
        "reason": decision.reason,
        "pass": decision.decision == expected,
    }


def _summary(version: str, cases: list[dict[str, Any]], side: str) -> dict[str, Any]:
    total = len(cases)
    passed = sum(1 for case in cases if case[side]["pass"])
    false_allows = sum(1 for case in cases if case["expected"] != case[side]["decision"] == "allow")
    false_blocks = sum(1 for case in cases if case["expected"] != case[side]["decision"] == "block")
    rate = passed / total
    return {
        "version": version,
        "passed": passed,
        "failed": total - passed,
        "total": total,
        "pass_rate": rate,
        "pass_rate_label": f"{rate:.0%}",
        "false_allows": false_allows,
        "false_blocks": false_blocks,
    }


def _gate(baseline: dict[str, Any], candidate: dict[str, Any], gate_cfg: Mapping[str, Any]) -> dict[str, Any]:
    checks = []
    if gate_cfg["require_no_regression"]:
        ok = candidate["passed"] >= baseline["passed"]
        checks.append({"check": "candidate_not_worse_than_baseline", "pass": ok,
                       "detail": f"{candidate['passed']}/{candidate['total']} vs {baseline['passed']}/{baseline['total']}"})
    if gate_cfg["require_zero_false_allows"]:
        ok = candidate["false_allows"] == 0
        checks.append({"check": "candidate_has_zero_false_allows", "pass": ok,
                       "detail": f"{candidate['false_allows']} missed block(s)"})
    verdict = GATE_PASS if all(check["pass"] for check in checks) else GATE_FAIL
    return {"verdict": verdict, "checks": checks}


def validate_eval_set(eval_doc: Any) -> list[dict[str, Any]]:
    """Structural checks beyond the JSON schema: unique ids and allow/block labels."""
    cases = eval_doc.get("cases") if isinstance(eval_doc, Mapping) else None
    if not isinstance(cases, list) or not cases:
        raise ReplayError("eval set has no cases")
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("id") if isinstance(case, Mapping) else None
        if not isinstance(case_id, str) or case_id in seen:
            raise ReplayError(f"eval case id missing or duplicated: {case_id!r}")
        if case.get("expected") not in ("allow", "block") or not isinstance(case.get("event"), Mapping):
            raise ReplayError(f"eval case {case_id}: needs expected allow/block and an event object")
        seen.add(case_id)
    return cases


def proposal_status(policy_doc: Mapping[str, Any], candidate_version: str, verdict: str) -> str:
    """Lifecycle state derived from facts, not typed by hand."""
    if verdict != GATE_PASS:
        return "REJECTED_GATE_FAILED"
    if policy_doc.get("active_version") == candidate_version:
        return "ACTIVE"
    return "STAGED_AWAITING_HUMAN_GATE"


def run_shadow_replay(policy_doc: Mapping[str, Any], eval_doc: Mapping[str, Any], dreaming: Mapping[str, Any]) -> dict[str, Any]:
    """Replay every recorded case under baseline and candidate; return the computed scorecard."""
    started = time.perf_counter()
    validate_policy_document(policy_doc)
    cases_in = validate_eval_set(eval_doc)
    proposal = dreaming["proposal"]
    baseline_policy = resolve_policy(policy_doc, proposal["from_version"])
    candidate_policy = resolve_policy(policy_doc, proposal["to_version"])
    cases: list[dict[str, Any]] = []
    for case in cases_in:
        event = case["event"]
        tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), Mapping) else {}
        subject = next((str(v) for v in tool_input.values() if isinstance(v, str)), "")
        cases.append({
            "id": case["id"],
            "title": case["title"],
            "tool_name": event.get("tool_name"),
            "stage": event.get("stage"),
            "subject": subject,
            "expected": case["expected"],
            "baseline": _side(evaluate(event, baseline_policy), case["expected"]),
            "candidate": _side(evaluate(event, candidate_policy), case["expected"]),
        })
    baseline = _summary(proposal["from_version"], cases, "baseline")
    candidate = _summary(proposal["to_version"], cases, "candidate")
    gate = _gate(baseline, candidate, dreaming["gate"])
    elapsed = timedelta(seconds=time.perf_counter() - started)
    return {
        "proposal_id": proposal["id"],
        "skill": proposal["skill"],
        "eval_set_id": eval_doc.get("eval_set_id"),
        "baseline": baseline,
        "candidate": candidate,
        "gate": gate,
        "status": proposal_status(policy_doc, proposal["to_version"], gate["verdict"]),
        "active_version": policy_doc["active_version"],
        "rule_diff": diff_rules(baseline_policy, candidate_policy),
        "cases": cases,
        "replay_latency_us": round(elapsed / _ONE_US),
        "replay_latency_ms": round(elapsed / _ONE_MS),
    }
