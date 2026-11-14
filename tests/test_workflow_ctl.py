"""workflow_ctl: deterministic S5 -> S6 transitions, retries, rollback/bounce verdicts, ledger, status, and wiring."""

from __future__ import annotations

import inspect
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import s5_release_gate, s6_deploy, s6_exit, s6_rollback, workflow_ctl
from scripts.cicd_common import Deps, seal
from tests import test_s5_release_gate as s5_fakes
from tests import test_s6_deploy as deploy_fakes
from tests import test_s6_exit as exit_fakes
from tests.helpers import edit_json

COMMIT = s5_fakes.COMMIT
IMAGE = deploy_fakes.IMAGE
RELEASE = deploy_fakes.RELEASE
OK = (0, {"ok": True})


class Script:
    """Fake stage: scripted (rc, summary) results in order (the last one repeats); records every call."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class Probe(Script):
    """Fake s6_exit dispatching on mode (preflight / verify / reprobe)."""

    def __init__(self, preflight=OK, verify=OK, reprobe=OK):
        super().__init__(OK)
        self.by_mode = {"preflight": [preflight], "verify": list(verify) if isinstance(verify, list) else [verify],
                        "reprobe": [reprobe]}

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        queue = self.by_mode[kwargs["mode"]]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def modes(self):
        return [kwargs["mode"] for _, kwargs in self.calls]


@pytest.fixture
def root(temp_root, tmp_path):
    evidence = tmp_path / "evidence"
    edit_json(temp_root / "config" / "cicd.json", lambda doc: doc["evidence"].update(
        s5_packet=str(evidence / "packet.json"), s6_deploy_report=str(evidence / "deploy.json"),
        ledger=str(evidence / "ledger.jsonl"),
        s6_exit_reports={mode: str(evidence / f"exit_{mode}.json") for mode in ("verify", "reprobe", "preflight")}))
    return temp_root


def run(root, stages, sleeps=None, extra=None):
    out, sleeps = io.StringIO(), sleeps if sleeps is not None else []
    deps = Deps(runner=lambda cmd, timeout: pytest.fail(f"unexpected command {cmd}"), sleep=sleeps.append,
                clock=lambda: 0.0, environ={})
    argv = ["--config-root", str(root), "release", "--commit", COMMIT, "--token-command", "print-token", *(extra or [])]
    code = workflow_ctl.main(argv, deps=deps, stages=stages, stdout=out)
    return code, out.getvalue().splitlines()


def ledger(root):
    path = json.loads((root / "config" / "cicd.json").read_text())["evidence"]["ledger"]
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def stages(gate=None, deploy=None, probe=None, rollback=None):
    return workflow_ctl.Stages(gate=gate or Script((0, {"ok": True, "image_ref": IMAGE})),
                               deploy=deploy or Script((0, {"ok": True, "release": RELEASE})),
                               probe=probe or Probe(), rollback=rollback or Script((0, {"executed": True})))


def test_pass_path_closes_and_ledgers_every_transition(root):
    flow = stages()
    code, lines = run(root, flow)
    assert code == 0 and lines == ["CLOSE S6_DEPLOY_CLOSE"]
    rows = ledger(root)
    assert [row["transition"] for row in rows] == ["PREFLIGHT:probe", "S5_RELEASE_GATE:gate", "S6_DEPLOY_CLOSE:deploy",
                                                   "S6_DEPLOY_CLOSE:exit", "verdict:close"]
    assert len({row["trace_id"] for row in rows}) == 1 and all(isinstance(row["latency_ms"], int) for row in rows)
    assert [row["rc"] for row in rows] == [0, 0, 0, 0, 0] and flow.rollback.calls == []
    assert flow.probe.modes() == ["preflight", "verify"]
    _, gate_kwargs = flow.gate.calls[0]
    assert gate_kwargs["commit"] == COMMIT and str(gate_kwargs["packet_path"]).endswith("packet.json")


def test_stage_kwargs_bind_to_the_real_signatures(root):
    flow = stages(probe=Probe(verify=(1, {})), deploy=Script((1, {"release": RELEASE, "new_revision_serving": None})))
    run(root, flow)
    flow_ok = stages()
    run(root, flow_ok)
    for fake, real in ((flow.gate, s5_release_gate.run_release_gate), (flow.deploy, s6_deploy.run_deploy),
                       (flow.probe, s6_exit.run_exit), (flow.rollback, s6_rollback.run_rollback),
                       (flow_ok.probe, s6_exit.run_exit)):
        assert fake.calls
        for args, kwargs in fake.calls:
            inspect.signature(real).bind(*args, **kwargs)
    argv = flow.rollback.calls[0][0][0]
    assert s6_rollback.parse_args(argv).execute is True and s6_rollback.parse_args(argv).bad_release == RELEASE


@pytest.mark.parametrize("summary,route", [({"bounce_to": "S4_VERIFY_QA"}, "S4_VERIFY_QA"), ({}, "S3_BUILD")])
def test_s5_failure_bounces_without_touching_s6(root, summary, route):
    flow = stages(gate=Script((1, {"ok": False, **summary})))
    code, lines = run(root, flow)
    assert code == 1 and lines == [f"BOUNCE S5_RELEASE_GATE -> {route}"] and flow.deploy.calls == []


def test_s5_transient_retries_with_backoff_and_resumes_the_build(root):
    flow = stages(gate=Script((75, {"build_id": "b-1"}), (75, {"build_id": "b-1"}), (0, {"ok": True, "image_ref": IMAGE})))
    sleeps = []
    code, lines = run(root, flow, sleeps)
    assert code == 0 and lines == ["CLOSE S6_DEPLOY_CLOSE"] and sleeps == [15, 45]
    assert "build_id" not in flow.gate.calls[0][1] and [call[1]["build_id"] for call in flow.gate.calls[1:]] == ["b-1"] * 2
    assert [row["attempt"] for row in ledger(root) if row["transition"] == "S5_RELEASE_GATE:gate"] == [1, 2, 3]


def test_s5_transient_exhaustion_halts_with_75(root):
    flow = stages(gate=Script((75, {"error_taxonomy": "RATE_LIMITED"})))
    sleeps = []
    code, lines = run(root, flow, sleeps)
    assert code == 75 and lines == ["HALT S5_RELEASE_GATE: RATE_LIMITED"] and sleeps == [15, 45, 120]
    assert len(flow.gate.calls) == 4 and flow.deploy.calls == []


def test_exit_gate_failure_rolls_back_reprobes_and_bounces(root):
    flow = stages(probe=Probe(verify=(1, {"error_taxonomy": "EXIT_GATE_FAILED"})))
    code, lines = run(root, flow)
    assert code == 1 and lines == ["ROLLBACK & BOUNCE S6_DEPLOY_CLOSE -> S3_BUILD"]
    argv = flow.rollback.calls[0][0][0]
    assert argv[:3] == ["--execute", "--project", "gement02"] and argv[argv.index("--bad-release") + 1] == RELEASE
    assert argv[argv.index("--bad-image") + 1] == IMAGE and flow.probe.modes() == ["preflight", "verify", "reprobe"]
    assert [row["transition"] for row in ledger(root)][-3:] == ["S6_DEPLOY_CLOSE:rollback", "S6_DEPLOY_CLOSE:reprobe",
                                                                "verdict:rollback_bounce"]


def test_failed_deploy_that_never_served_bounces_without_rollback(root):
    flow = stages(deploy=Script((1, {"release": RELEASE, "new_revision_serving": False})))
    code, lines = run(root, flow)
    assert code == 1 and lines == ["BOUNCE S6_DEPLOY_CLOSE -> S3_BUILD"] and flow.rollback.calls == []


def test_failed_deploy_with_unknown_serving_runs_the_guarded_rollback(root):
    flow = stages(deploy=Script((1, {"release": RELEASE, "new_revision_serving": None})),
                  rollback=Script((0, {"executed": False, "skipped": "target is not on the rejected release"})))
    code, lines = run(root, flow)
    assert code == 1 and lines == ["BOUNCE S6_DEPLOY_CLOSE -> S3_BUILD"] and len(flow.rollback.calls) == 1


def test_deploy_transient_resumes_the_same_release(root):
    flow = stages(deploy=Script((75, {"release": RELEASE}), (0, {"ok": True, "release": RELEASE})))
    code, _ = run(root, flow)
    assert code == 0 and flow.deploy.calls[1][1]["resume_release"] == RELEASE


def test_exit_gate_transient_exhaustion_counts_as_a_failure(root):
    flow = stages(probe=Probe(verify=(75, {"error_taxonomy": "COLD_START_EXHAUSTED"})))
    code, lines = run(root, flow)
    assert code == 1 and lines == ["ROLLBACK & BOUNCE S6_DEPLOY_CLOSE -> S3_BUILD"]


def test_rollback_and_reprobe_failures_raise_an_incident(root):
    flow = stages(probe=Probe(verify=(1, {}), reprobe=(1, {})),
                  rollback=Script((1, {"executed": False, "error_taxonomy": "PERMISSION_DENIED"})))
    code, lines = run(root, flow)
    assert code == 1 and lines == ["INCIDENT S6_DEPLOY_CLOSE: rollback rc=1, reprobe rc=1",
                                   "BOUNCE S6_DEPLOY_CLOSE -> S3_BUILD"]


def test_transient_rollback_taxonomy_is_retried(root):
    flow = stages(probe=Probe(verify=(1, {})),
                  rollback=Script((1, {"executed": False, "error_taxonomy": "UNAVAILABLE"}), (0, {"executed": True})))
    sleeps = []
    code, lines = run(root, flow, sleeps)
    assert code == 1 and lines == ["ROLLBACK & BOUNCE S6_DEPLOY_CLOSE -> S3_BUILD"] and sleeps == [15]
    assert len(flow.rollback.calls) == 2


@pytest.mark.parametrize("result,code", [((1, {"error_taxonomy": "TOKEN_COMMAND_MISSING"}), 1),
                                         ((75, {"error_taxonomy": "TOKEN_UNAVAILABLE"}), 75)])
def test_preflight_failure_halts_before_building(root, result, code):
    flow = stages(probe=Probe(preflight=result))
    rc, lines = run(root, flow)
    assert rc == code and lines == [f"HALT PREFLIGHT: {result[1]['error_taxonomy']}"] and flow.gate.calls == []


def test_release_requires_commit_and_config(temp_root):
    with pytest.raises(SystemExit):
        workflow_ctl.parse_args(["release"])
    (temp_root / "config" / "cicd.json").unlink()
    out = io.StringIO()
    assert workflow_ctl.main(["--config-root", str(temp_root), "status"], stdout=out) == 1
    assert out.getvalue().startswith("HALT CONFIG_MISSING")


def test_status_summarizes_evidence_and_flags_malformed_files(root):
    cfg = json.loads((root / "config" / "cicd.json").read_text())["evidence"]
    packet = seal({"commit_sha": COMMIT, "image_ref": IMAGE, "pytest_summary": "561 passed"})
    for path, doc in ((cfg["s5_packet"], packet), (cfg["s6_deploy_report"], {"ok": True, "release": RELEASE}),
                      (cfg["s6_exit_reports"]["verify"], {"ok": True, "mode": "verify", "failed_checks": []})):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(doc), encoding="utf-8")
    Path(cfg["ledger"]).write_text("\n".join(json.dumps({"transition": f"t{index}"}) for index in range(12)) + "\n",
                                   encoding="utf-8")
    out = io.StringIO()
    assert workflow_ctl.main(["--config-root", str(root), "status"], stdout=out) == 0
    lines = {line["artifact"]: line for line in map(json.loads, out.getvalue().splitlines())}
    assert lines["s5_release_packet"]["sealed"] is True and lines["s5_release_packet"]["commit_sha"] == COMMIT
    assert lines["s6_deploy_report"]["release"] == RELEASE and lines["s6_exit_verify"]["ok"] is True
    assert lines["s6_exit_reprobe"]["present"] is False and len(lines["workflow_ledger"]["tail"]) == 10
    with open(cfg["s6_deploy_report"], "w", encoding="utf-8") as handle:
        handle.write("{broken")
    out = io.StringIO()
    assert workflow_ctl.main(["--config-root", str(root), "status"], stdout=out) == 1
    assert "EVIDENCE_MALFORMED" in out.getvalue()


def test_end_to_end_with_the_real_stage_functions_and_fake_clouds(root, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "cloudbuild.yaml").write_text("steps: []\n", encoding="utf-8")
    ci = s5_fakes.FakeGcloud(describes=[s5_fakes.build_doc()])
    cd = deploy_fakes.FakeCloud(rollouts=[[deploy_fakes.rollout("SUCCEEDED")]], service=exit_fakes.service_doc(),
                                revision=deploy_fakes.revision_doc())
    iap = exit_fakes.FakeGcloud()

    def runner(cmd, timeout):
        if cmd[0] == "print-token" or "iap" in cmd or "get-iam-policy" in cmd:
            return iap(cmd, timeout)
        if cmd[0] == "git" or cmd[1] in ("builds", "storage"):
            return ci(cmd, timeout)
        return cd(cmd, timeout)

    deps = Deps(runner=runner, http=exit_fakes.FakeHttp(), sleep=lambda _: None, clock=lambda: 0.0,
                utcnow=lambda: datetime(2026, 11, 14, 22, 45, tzinfo=timezone.utc), environ={})
    out = io.StringIO()
    code = workflow_ctl.main(["--config-root", str(root), "release", "--commit", COMMIT, "--source", str(source),
                              "--token-command", "print-token"], deps=deps, stdout=out)
    assert code == 0 and out.getvalue().splitlines() == ["CLOSE S6_DEPLOY_CLOSE"]
    assert ["git_status", "git_head", "submit", "describe", "cat"] == [key for key in ci.keys()][:5]
    assert "create" in cd.keys() and [row["rc"] for row in ledger(root)] == [0, 0, 0, 0, 0]
