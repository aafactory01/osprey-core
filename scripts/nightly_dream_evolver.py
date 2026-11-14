#!/usr/bin/env python3
"""Night Dreaming evolver for DP-001 (`ops-release-guardrails` v1.0.0 -> v1.1.0).

Replays the curated, labelled scenario traces in dreamstore/evals/ (modelled on common release
failure modes, not recorded from live sessions) through the real DP-001 `evaluate()` under the
baseline and candidate policy versions, then writes (atomically):
  dreamstore/scorecard.json                        computed scorecard with per-case results
  dreamstore/proposals/DP-001.json                 proposal, rule diff, derived lifecycle status
  team-memory/dreams/<date>-nightly-dream-synthesis.md   only with --write
Prints one JSON summary on stdout (telemetry goes to stderr). Exits 1 when the candidate scores
below the baseline or on any configuration error (nothing is written on configuration errors).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import IO, Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import telemetry  # noqa: E402
from app.config_store import REPO_ROOT, ConfigError, ConfigStore  # noqa: E402
from dreamstore.hooks.dp001_policy import PolicyError  # noqa: E402
from dreamstore.replay import GATE_PASS, ReplayError, run_shadow_replay  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
METHOD = "shadow replay of curated scenario traces (PreToolUse events) through dreamstore.hooks.dp001_policy.evaluate"


def write_json(path: Path, doc: Any) -> None:
    """Atomic write: temp file + rename, so readers never see a half-written scorecard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent="  ", sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_scorecard(report: Mapping[str, Any], trace_id: str, generated_at: str, latency_ms: int) -> dict[str, Any]:
    cases = [{
        "id": case["id"],
        "title": case["title"],
        "expected": case["expected"],
        "baseline": case["baseline"]["decision"],
        "baseline_rule": case["baseline"]["rule_id"],
        "baseline_pass": case["baseline"]["pass"],
        "candidate": case["candidate"]["decision"],
        "candidate_rule": case["candidate"]["rule_id"],
        "candidate_pass": case["candidate"]["pass"],
    } for case in report["cases"]]
    return {
        "proposal_id": report["proposal_id"],
        "skill": report["skill"],
        "eval_set_id": report["eval_set_id"],
        "eval_source_label": report["eval_source_label"],
        "method": METHOD,
        "baseline": report["baseline"],
        "candidate": report["candidate"],
        "gate": report["gate"],
        "cases": cases,
        "generated_at": generated_at,
        "trace_id": trace_id,
        "latency_ms": latency_ms,
    }


def build_proposal(proposal_cfg: Mapping[str, Any], report: Mapping[str, Any], generated_at: str) -> dict[str, Any]:
    return {
        **proposal_cfg,
        "status": report["status"],
        "active_version": report["active_version"],
        "rule_diff": report["rule_diff"],
        "gate": report["gate"],
        "scorecard": {side: report[side]["pass_rate_label"] for side in ("baseline", "candidate")},
        "generated_at": generated_at,
    }


def _section(title: str, items: list[str]) -> list[str]:
    return ["", f"## {title}", "", *items]


def _side_line(label: str, side: Mapping[str, Any]) -> str:
    return (f"- {label} v{side['version']}: {side['pass_rate_label']} ({side['passed']}/{side['total']}),"
            f" {side['false_allows']} false allow(s), {side['false_blocks']} false block(s)")


def _case_line(case: Mapping[str, Any]) -> str:
    b, c = case["baseline"], case["candidate"]
    return (f"- {case['id']} {case['title']}: expected {case['expected']};"
            f" baseline {b['decision']} ({'pass' if b['pass'] else 'fail'}, {b['rule_id']});"
            f" candidate {c['decision']} ({'pass' if c['pass'] else 'fail'}, {c['rule_id']})")


def render_synthesis(proposal_cfg: Mapping[str, Any], report: Mapping[str, Any], day: date, generated_at: str) -> str:
    diff = report["rule_diff"]
    header = [
        f"# Night Dreaming synthesis {day.isoformat()}",
        "",
        f"- Proposal: {proposal_cfg['id']} {proposal_cfg['title']}",
        f"- Skill: {proposal_cfg['skill']} v{proposal_cfg['from_version']} to v{proposal_cfg['to_version']}",
        f"- Method: {METHOD}",
        f"- Evidence basis: {proposal_cfg['evidence_basis']}",
        f"- Generated: {generated_at} by scripts/nightly_dream_evolver.py --write",
    ]
    scorecard = [_side_line("Baseline", report["baseline"]), _side_line("Candidate", report["candidate"]),
                 f"- Gate: {report['gate']['verdict']}; proposal status: {report['status']}"]
    rule_diff = [f"- {name.capitalize()}: {', '.join(diff[name]) or 'none'}" for name in ("added", "modified", "removed")]
    lines = [*header, *_section("Scorecard", scorecard), *_section("Rule diff", rule_diff),
             *_section("Cases", [_case_line(case) for case in report["cases"]]),
             *_section("Why", [f"- {reason}" for reason in proposal_cfg["rationale"]])]
    return "\n".join(lines) + "\n"


def run(config_root: Path, out_root: Path, write_synthesis: bool, day: date) -> tuple[int, dict[str, Any]]:
    started = time.perf_counter()
    trace_id = telemetry.new_trace_id()
    try:
        store = ConfigStore(config_root)
        dreaming = store.get("night_dreaming")
        report = run_shadow_replay(store.get("dp001_guard_policy"), store.get("shadow_replay_evals"), dreaming)
    except (ConfigError, PolicyError, ReplayError) as exc:
        return EXIT_FAIL, {"status": "error", "error_taxonomy": exc.taxonomy, "detail": str(exc),
                           "written": [], "trace_id": trace_id, "latency_ms": telemetry.elapsed_ms(started)}
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    outputs = dreaming["outputs"]
    written = [outputs["scorecard"], outputs["proposal"]]
    write_json(out_root / outputs["scorecard"], build_scorecard(report, trace_id, generated_at, telemetry.elapsed_ms(started)))
    write_json(out_root / outputs["proposal"], build_proposal(dreaming["proposal"], report, generated_at))
    if write_synthesis:
        relpath = f"{outputs['synthesis_dir']}/{day.isoformat()}-nightly-dream-synthesis.md"
        target = out_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_synthesis(dreaming["proposal"], report, day, generated_at), encoding="utf-8")
        written.append(relpath)
    regression = report["candidate"]["passed"] < report["baseline"]["passed"]
    # The S5 exit gate relies on this exit code: any gate check failure (not only a regression) fails closed.
    gate_failed = report["gate"]["verdict"] != GATE_PASS
    status = "regression" if regression else ("gate_failed" if gate_failed else "ok")
    return (EXIT_FAIL if regression or gate_failed else EXIT_OK), {
        "status": status,
        "proposal_id": report["proposal_id"],
        "baseline": report["baseline"]["pass_rate_label"],
        "candidate": report["candidate"]["pass_rate_label"],
        "gate": report["gate"]["verdict"],
        "proposal_status": report["status"],
        "written": written,
        "trace_id": trace_id,
        "latency_ms": telemetry.elapsed_ms(started),
    }


def main(argv: list[str] | None = None, stdout: IO[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Night Dreaming DP-001 shadow replay evolver")
    parser.add_argument("--write", action="store_true", help="also write the dated markdown synthesis")
    parser.add_argument("--config-root", default=str(REPO_ROOT), help="root containing config/ and dreamstore/evals/")
    parser.add_argument("--out-root", default=str(REPO_ROOT), help="root that receives scorecard/proposal/synthesis")
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="synthesis date (YYYY-MM-DD, default today UTC)")
    args = parser.parse_args(argv)
    day = args.date or datetime.now(timezone.utc).date()
    previous_stream = telemetry.use_stream(sys.stderr)
    try:
        code, summary = run(Path(args.config_root), Path(args.out_root), args.write, day)
    finally:
        telemetry.use_stream(previous_stream)
    out = stdout if stdout is not None else sys.stdout
    out.write(json.dumps(summary, sort_keys=True) + "\n")
    out.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
