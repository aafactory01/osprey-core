"""DP-001 guard: pure evaluate() vectors for every rule, file-write gates, malformed events, policy validation."""

from __future__ import annotations

import copy

import pytest

from dreamstore.hooks.dp001_pre_tool_guard import (
    DEFAULT_POLICY_PATH, Decision, PolicyError, evaluate, load_policy_document, resolve_policy, validate_policy_document,
)


@pytest.fixture(scope="module")
def doc():
    return load_policy_document(DEFAULT_POLICY_PATH)


@pytest.fixture(scope="module")
def v11(doc):
    return resolve_policy(doc, "1.1.0")


@pytest.fixture(scope="module")
def v10(doc):
    return resolve_policy(doc, "1.0.0")


def bash(command, stage="S3_BUILD"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "stage": stage}


def write(path, stage="S3_BUILD", tool="Write", key="file_path"):
    return {"tool_name": tool, "tool_input": {key: path, "content": "x"}, "stage": stage}


def test_policy_document_shape(doc):
    assert doc["active_version"] == "1.1.0" and set(doc["versions"]) == {"1.0.0", "1.1.0"}
    assert doc["policy_id"] == "ops-release-guardrails"


@pytest.mark.parametrize("command,stage,rule_id", [
    ("git push --force origin main", "S3_BUILD", "R-GIT-FORCE-PUSH-MAIN"),
    ("git push origin main -f", "S3_BUILD", "R-GIT-FORCE-PUSH-MAIN"),
    ("git push origin +main", "S3_BUILD", "R-GIT-FORCE-PUSH-MAIN"),
    ("cd repo && git -C . push --force-with-lease origin main", "S2_TESTS", "R-GIT-FORCE-PUSH-MAIN"),
    ("git reset --hard origin/main", "S3_BUILD", "R-GIT-RESET-HARD-ORIGIN"),
    ("git reset --hard @{u}", "S3_BUILD", "R-GIT-RESET-HARD-ORIGIN"),
    ("rm -rf /", "S3_BUILD", "R-RM-RF"),
    ("rm -rf ~", "S3_BUILD", "R-RM-RF"),
    ("rm -fr .", "S3_BUILD", "R-RM-RF"),
    ('rm -rf "$REPO_ROOT"', "S3_BUILD", "R-RM-RF"),
    ("sudo rm -rf --no-preserve-root /", "S3_BUILD", "R-RM-RF"),
    ("gcloud run services delete osprey-core --region us-central1", "S6_DEPLOY_CLOSE", "R-RUN-SERVICE-DELETE"),
    ("gcloud run deploy osprey-core --source . --iap --allow-unauthenticated", "S6_DEPLOY_CLOSE", "R-RUN-DEPLOY-PUBLIC"),
    ("gcloud run services add-iam-policy-binding osprey-core --member=allUsers --role=roles/run.invoker", "S6_DEPLOY_CLOSE",
     "R-RUN-PUBLIC-IAM"),
    ("gcloud beta run deploy osprey-core --source .", "S6_DEPLOY_CLOSE", "R-RUN-DEPLOY-NO-IAP"),
    ("gcloud run deploy osprey-core --source . --iap", "S3_BUILD", "R-DEPLOY-BEFORE-RELEASE-GATE"),
    ("gcloud run services update-traffic osprey-core --to-latest", "S4_VERIFY_QA", "R-DEPLOY-BEFORE-RELEASE-GATE"),
    ("python3 scripts/s6_rollback.py --execute", "S2_TESTS", "R-DEPLOY-BEFORE-RELEASE-GATE"),
    ("kubectl delete namespace prod", "S6_DEPLOY_CLOSE", "R-KUBECTL-DELETE-NS"),
    ("kubectl --context x delete ns prod", "S6_DEPLOY_CLOSE", "R-KUBECTL-DELETE-NS"),
    ("kubectl get secrets -A -o yaml", "S4_VERIFY_QA", "R-KUBECTL-SECRETS"),
    ("kubectl -n prod describe secret db-password", "S4_VERIFY_QA", "R-KUBECTL-SECRETS"),
    ("gcloud run services list --impersonate-service-account=sa@p.iam.gserviceaccount.com", "S4_VERIFY_QA", "R-IMPERSONATE-SA"),
    ("echo $AUDIT_HMAC_KEY", "S2_TESTS", "R-SECRETS-ECHO"),
    ("printf '%s' ${github_token}", "S2_TESTS", "R-SECRETS-ECHO"),
    ("printenv GITHUB_TOKEN", "S2_TESTS", "R-SECRETS-ECHO"),
    ("gcloud secrets versions access latest --secret=audit-key", "S5_RELEASE_GATE", "R-SECRETS-ECHO"),
    ("cat .env", "S3_BUILD", "R-SECRETS-ECHO"),
    ("echo stage=S9 > .workflow/workflow.yaml", "S3_BUILD", "R-SHELL-PROTECTED-PATH-WRITE"),
    ("sed -i s/60/100/ dreamstore/scorecard.json", "S2_TESTS", "R-SHELL-PROTECTED-PATH-WRITE"),
    ("cp /tmp/p.json config/dp001_guard_policy.json", "S4_VERIFY_QA", "R-SHELL-PROTECTED-PATH-WRITE"),
])
def test_v11_blocks(v11, command, stage, rule_id):
    decision = evaluate(bash(command, stage), v11)
    assert decision.blocked and decision.rule_id == rule_id and decision.policy_version == "1.1.0"
    assert decision.reason


@pytest.mark.parametrize("command,stage", [
    ("ls -la", "S3_BUILD"),
    ("python -m pytest -q -p no:cacheprovider", "S2_TESTS"),
    ("rm -rf ./dist", "S3_BUILD"),
    ("rm -rf build/ node_modules/.cache", "S3_BUILD"),
    ("git push origin feature/console", "S3_BUILD"),
    ("git push --force origin feature/console", "S3_BUILD"),
    ("git reset --hard HEAD~1", "S3_BUILD"),
    ("gcloud run deploy osprey-core --source . --region us-central1 --iap --no-allow-unauthenticated", "S6_DEPLOY_CLOSE"),
    ("gcloud run deploy osprey-core --source . --iap", "S5_RELEASE_GATE"),
    ("python3 scripts/s6_rollback.py", "S2_TESTS"),
    ("kubectl get pods -n prod", "S4_VERIFY_QA"),
    ("cat .workflow/workflow.yaml", "S3_BUILD"),
    ("echo $HOME && echo 'h\u00e9llo w\u00f6rld'", "S3_BUILD"),
])
def test_v11_allows(v11, command, stage):
    decision = evaluate(bash(command, stage), v11)
    assert decision.decision == "allow" and decision.rule_id == "ALLOW_NO_RULE_MATCHED"


def test_nbsp_obfuscated_force_push_still_blocked(v11):
    assert evaluate(bash("git\u00a0push --force origin\u00a0main"), v11).blocked


@pytest.mark.parametrize("path,stage,blocked", [
    (".workflow/workflow.yaml", "S1_SPEC", False),
    (".workflow/workflow.yaml", "S3_BUILD", True),
    ("/workspace/osprey-core/.workflow/task_backend.py", "S4_VERIFY_QA", True),
    ("WORKFLOW.yaml", "S2_TESTS", True),
    ("app/../.workflow/workflow.yaml", "S3_BUILD", True),
    (".WORKFLOW/Workflow.YAML", "S3_BUILD", True),
    (".workflow\\workflow.yaml", "S3_BUILD", True),
    ("dreamstore/scorecard.json", "S1_SPEC", True),
    ("dreamstore/proposals/DP-001.json", "S6_DEPLOY_CLOSE", True),
    ("/abs/osprey-core/config/dp001_guard_policy.json", "S1_SPEC", True),
    ("dreamstore/hooks/dp001_pre_tool_guard.py", "S3_BUILD", True),
    ("app/app.py", "S3_BUILD", False),
    ("config/rollouts.json", "S3_BUILD", False),
])
def test_v11_file_write_gates(v11, path, stage, blocked):
    assert evaluate(write(path, stage), v11).blocked is blocked


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("MultiEdit", "file_path"), ("write_file", "path"),
                                      ("replace", "absolute_path"), ("edit_file", "TargetFile"), ("NotebookEdit", "notebook_path")])
def test_every_write_tool_shape_is_guarded(v11, tool, key):
    assert evaluate(write("dreamstore/scorecard.json", "S2_TESTS", tool, key), v11).rule_id == "R-PROTECTED-DREAM-ARTIFACTS"


@pytest.mark.parametrize("tool,key", [("run_shell_command", "command"), ("run_command", "CommandLine"), ("shell", "cmd")])
def test_every_shell_tool_shape_is_guarded(v11, tool, key):
    event = {"tool_name": tool, "tool_input": {key: "kubectl get secrets"}, "stage": "S3_BUILD"}
    assert evaluate(event, v11).rule_id == "R-KUBECTL-SECRETS"


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "WebFetch"])
def test_unguarded_tools_allowed(v11, tool):
    decision = evaluate({"tool_name": tool, "tool_input": {"path": ".workflow/workflow.yaml"}}, v11)
    assert decision.decision == "allow" and decision.rule_id == "ALLOW_UNGUARDED_TOOL"


@pytest.mark.parametrize("event,rule_id", [
    ("not a dict", "FAIL_CLOSED_MALFORMED_EVENT"),
    ({}, "FAIL_CLOSED_MALFORMED_EVENT"),
    ({"tool_name": "", "tool_input": {}}, "FAIL_CLOSED_MALFORMED_EVENT"),
    ({"tool_name": 7, "tool_input": {}}, "FAIL_CLOSED_MALFORMED_EVENT"),
    ({"tool_name": "Bash", "tool_input": "rm -rf /"}, "FAIL_CLOSED_MALFORMED_EVENT"),
    ({"tool_name": "Bash", "tool_input": {"command": "ls"}, "stage": 4}, "FAIL_CLOSED_MALFORMED_EVENT"),
    ({"tool_name": "Bash", "tool_input": {}}, "FAIL_CLOSED_MISSING_INPUT"),
    ({"tool_name": "Bash", "tool_input": {"command": ["rm", "-rf", "/"]}}, "FAIL_CLOSED_MISSING_INPUT"),
    ({"tool_name": "Bash", "tool_input": {"command": "   "}}, "FAIL_CLOSED_MISSING_INPUT"),
    ({"tool_name": "Write", "tool_input": {"content": "x"}}, "FAIL_CLOSED_MISSING_INPUT"),
])
def test_malformed_events_fail_closed(v11, event, rule_id):
    decision = evaluate(event, v11)
    assert decision.blocked and decision.rule_id == rule_id


def test_numbers_and_unicode_in_payload_do_not_confuse_guard(v11):
    event = {"tool_name": "Bash", "tool_input": {"command": "echo ok \u2603", "n": -0.0, "big": 10 ** 400}, "stage": "S3_BUILD"}
    assert evaluate(event, v11).decision == "allow"


@pytest.mark.parametrize("stage", [None, "", "NIGHTLY", "s6_deploy_close"])
def test_stage_gate_blocks_unknown_or_missing_stage(v11, stage):
    event = bash("gcloud run deploy osprey-core --iap", stage)
    decision = evaluate(event, v11)
    assert decision.blocked and decision.rule_id == "R-DEPLOY-BEFORE-RELEASE-GATE"


def test_baseline_differs_exactly_where_dp001_says(v10, v11):
    assert evaluate(bash("rm -rf ./dist"), v10).rule_id == "R-RM-RF"
    assert not evaluate(bash("rm -rf ./dist"), v11).blocked
    public = bash("gcloud run deploy osprey-core --allow-unauthenticated", "S6_DEPLOY_CLOSE")
    assert not evaluate(public, v10).blocked and evaluate(public, v11).blocked


# CI/CD stage gates (v1.1.0): Cloud Build CI from S5_RELEASE_GATE on, Google Cloud Deploy only at S6_DEPLOY_CLOSE.
# Each entry lists the stage_gate rules that can fire, in policy order, as (rule id, minimum stage).
CI = (("R-CI-BEFORE-RELEASE-GATE", "S5_RELEASE_GATE"),)
CD = (("R-CD-BEFORE-DEPLOY-CLOSE", "S6_DEPLOY_CLOSE"),)
ROLLBACK = (("R-DEPLOY-BEFORE-RELEASE-GATE", "S5_RELEASE_GATE"), ("R-CD-BEFORE-DEPLOY-CLOSE", "S6_DEPLOY_CLOSE"))
CICD_COMMANDS = [
    ("gcloud builds submit --config=cloudbuild.yaml", CI),
    ("gcloud --project=gement02 builds submit --config=cloudbuild.yaml --substitutions=SHORT_SHA=8a1e974", CI),
    ("gcloud beta builds submit --config=cloudbuild.yaml", CI),
    ("python3 scripts/s5_release_gate.py --commit 8a1e974", CI),
    ("python3 scripts/workflow_ctl.py release --commit 8a1e974", CI),
    ("gcloud deploy releases create rel-8a1e974 --delivery-pipeline=osprey-core-cd --region=us-central1", CD),
    ("gcloud deploy apply --file=clouddeploy.yaml --region=us-central1 --project=gement02", CD),
    ("gcloud deploy targets rollback osprey-core-prod --delivery-pipeline=osprey-core-cd --region=us-central1", CD),
    ("gcloud\u00a0deploy\u00a0apply --file=clouddeploy.yaml", CD),
    ("python3 scripts/s6_deploy.py --packet .workflow/evidence/s5_release_packet.json", CD),
    ("python3 scripts/s6_rollback.py --project gement02 --execute", ROLLBACK),
]


def _expected_gate(gates, stage, order):
    return next(((rule_id, floor) for rule_id, floor in gates if order.index(stage) < order.index(floor)), None)


@pytest.mark.parametrize("command,gates", CICD_COMMANDS)
def test_cicd_commands_are_stage_gated(doc, v11, command, gates):
    order = doc["stage_order"]
    for stage in order:
        decision, expected = evaluate(bash(command, stage), v11), _expected_gate(gates, stage, order)
        if expected is None:
            assert decision.decision == "allow", (command, stage, decision)
        else:
            assert decision.blocked and decision.rule_id == expected[0], (command, stage, decision)
            assert f"current stage {stage} is before {expected[1]}" in decision.reason


@pytest.mark.parametrize("command,gates", CICD_COMMANDS)
@pytest.mark.parametrize("stage", [None, "", "NIGHTLY", "s6_deploy_close"])
def test_cicd_gates_fail_closed_without_a_known_stage(v11, command, gates, stage):
    decision = evaluate(bash(command, stage), v11)
    assert decision.blocked and decision.rule_id == gates[0][0]


@pytest.mark.parametrize("command", [
    "cat scripts/s6_deploy.py scripts/s5_release_gate.py", "python3 -m py_compile scripts/s5_release_gate.py scripts/s6_deploy.py",
    "python3 scripts/s6_rollback.py --project gement02", "python3 scripts/workflow_ctl.py status",
    "python3 scripts/s6_exit.py --packet .workflow/evidence/s5_release_packet.json --report .workflow/evidence/s6_deploy_report.json",
    "gcloud builds list --region=us-central1", "gcloud builds describe 1234 --region=us-central1",
    "gcloud deploy releases list --delivery-pipeline=osprey-core-cd --region=us-central1",
    "gcloud deploy rollouts list --release=rel-1 --delivery-pipeline=osprey-core-cd --region=us-central1",
])
def test_cicd_reads_and_dry_runs_stay_allowed_before_s5(v11, command):
    decision = evaluate(bash(command, "S4_VERIFY_QA"), v11)
    assert decision.decision == "allow" and decision.rule_id == "ALLOW_NO_RULE_MATCHED"


def test_compound_cicd_command_needs_the_latest_stage(v11):
    both = "gcloud builds submit --config=cloudbuild.yaml && gcloud deploy releases create rel-1 --delivery-pipeline=osprey-core-cd"
    assert evaluate(bash(both, "S5_RELEASE_GATE"), v11).rule_id == "R-CD-BEFORE-DEPLOY-CLOSE"
    assert evaluate(bash(both, "S6_DEPLOY_CLOSE"), v11).decision == "allow"


@pytest.mark.parametrize("command,gates", [entry for entry in CICD_COMMANDS if entry[1] != ROLLBACK])
def test_baseline_has_no_cicd_gates(v10, v11, command, gates):
    assert evaluate(bash(command, "S2_TESTS"), v10).decision == "allow"
    assert evaluate(bash(command, "S2_TESTS"), v11).rule_id == gates[0][0]


@pytest.mark.parametrize("breakage", ["no_rules", "bad_regex_runtime", "not_mapping", "bad_kind"])
def test_defective_policy_blocks_instead_of_raising(v11, breakage):
    policy = copy.deepcopy(v11)
    if breakage == "no_rules":
        del policy["rules"]
    elif breakage == "bad_regex_runtime":
        policy["rules"][0]["patterns"] = ["(unclosed"]
    elif breakage == "bad_kind":
        policy["rules"][0]["kind"] = "mystery"
    else:
        policy = ["not", "a", "mapping"]
    decision = evaluate(bash("git push --force origin main"), policy)
    assert isinstance(decision, Decision) and decision.blocked
    assert decision.rule_id == "FAIL_CLOSED_POLICY_INVALID"


def test_default_decision_block_is_honored(v11):
    policy = dict(v11, default_decision="block")
    decision = evaluate(bash("ls"), policy)
    assert decision.blocked and decision.rule_id == "BLOCK_DEFAULT_DECISION"


def _mutated(doc, mutate):
    clone = copy.deepcopy(doc)
    mutate(clone)
    return clone


@pytest.mark.parametrize("mutate,message", [
    (lambda d: d["versions"]["1.1.0"]["rules"].append(dict(d["versions"]["1.1.0"]["rules"][0])), "duplicate rule id"),
    (lambda d: d["versions"]["1.1.0"]["rules"][0].update(patterns=["(bad"]), "invalid regex"),
    (lambda d: d["versions"]["1.1.0"]["rules"][0].update(kind="shell_magic"), "unknown kind"),
    (lambda d: d["versions"]["1.1.0"]["rules"][0].update(tool_class="file_write"), "needs tool_class"),
    (lambda d: d["versions"]["1.1.0"]["rules"][-1].update(allowed_stages=["S7_PARTY"]), "allowed_stages"),
    (lambda d: d["versions"]["1.1.0"]["rules"][7].update(min_stage="S0"), "min_stage"),
    (lambda d: d["versions"]["1.1.0"]["rules"][0].update(flags=["NOT_A_FLAG"]), "unknown regex flag"),
    (lambda d: d.update(active_version="9.9.9"), "not a defined version"),
    (lambda d: d["io"].update(stdin_timeout_seconds=0), "positive"),
    (lambda d: d["io"].update(max_stdin_bytes=True), "positive integer"),
    (lambda d: d.update(stage_order=["S1_SPEC", "S1_SPEC"]), "duplicates"),
    (lambda d: d.update(default_decision="maybe"), "default_decision"),
    (lambda d: d["tool_classes"].pop("shell"), "tool_classes.shell"),
    (lambda d: d["versions"]["1.1.0"].update(rules="nope"), "rules list"),
])
def test_policy_validation_rejects_defects(doc, mutate, message):
    with pytest.raises(PolicyError, match=message):
        validate_policy_document(_mutated(doc, mutate))


def test_resolve_unknown_version_raises(doc):
    with pytest.raises(PolicyError, match="not defined"):
        resolve_policy(doc, "0.0.1")


def test_load_policy_document_failures(tmp_path):
    with pytest.raises(PolicyError, match="unreadable"):
        load_policy_document(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(PolicyError, match="not valid JSON"):
        load_policy_document(bad)
    nan = tmp_path / "nan.json"
    nan.write_text('{"io": NaN}', encoding="utf-8")
    with pytest.raises(PolicyError, match="not valid JSON"):
        load_policy_document(nan)
    binary = tmp_path / "bin.json"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(PolicyError, match="not UTF-8"):
        load_policy_document(binary)
