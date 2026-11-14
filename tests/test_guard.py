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


def bash(command, stage="S2_CODE"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "stage": stage}


def write(path, stage="S2_CODE", tool="Write", key="file_path"):
    return {"tool_name": tool, "tool_input": {key: path, "content": "x"}, "stage": stage}


def test_policy_document_shape(doc):
    assert doc["active_version"] == "1.1.0" and set(doc["versions"]) == {"1.0.0", "1.1.0"}
    assert doc["policy_id"] == "ops-release-guardrails"


@pytest.mark.parametrize("command,stage,rule_id", [
    ("git push --force origin main", "S2_CODE", "R-GIT-FORCE-PUSH-MAIN"),
    ("git push origin main -f", "S2_CODE", "R-GIT-FORCE-PUSH-MAIN"),
    ("git push origin +main", "S2_CODE", "R-GIT-FORCE-PUSH-MAIN"),
    ("cd repo && git -C . push --force-with-lease origin main", "S3_TEST", "R-GIT-FORCE-PUSH-MAIN"),
    ("git reset --hard origin/main", "S2_CODE", "R-GIT-RESET-HARD-ORIGIN"),
    ("git reset --hard @{u}", "S2_CODE", "R-GIT-RESET-HARD-ORIGIN"),
    ("rm -rf /", "S2_CODE", "R-RM-RF"),
    ("rm -rf ~", "S2_CODE", "R-RM-RF"),
    ("rm -fr .", "S2_CODE", "R-RM-RF"),
    ('rm -rf "$REPO_ROOT"', "S2_CODE", "R-RM-RF"),
    ("sudo rm -rf --no-preserve-root /", "S2_CODE", "R-RM-RF"),
    ("gcloud run services delete osprey-core --region us-central1", "S6_DEPLOY_CLOSE", "R-RUN-SERVICE-DELETE"),
    ("gcloud run deploy osprey-core --source . --iap --allow-unauthenticated", "S6_DEPLOY_CLOSE", "R-RUN-DEPLOY-PUBLIC"),
    ("gcloud run services add-iam-policy-binding osprey-core --member=allUsers --role=roles/run.invoker", "S6_DEPLOY_CLOSE",
     "R-RUN-PUBLIC-IAM"),
    ("gcloud beta run deploy osprey-core --source .", "S6_DEPLOY_CLOSE", "R-RUN-DEPLOY-NO-IAP"),
    ("gcloud run deploy osprey-core --source . --iap", "S2_CODE", "R-DEPLOY-BEFORE-RELEASE-GATE"),
    ("gcloud run services update-traffic osprey-core --to-latest", "S4_REVIEW", "R-DEPLOY-BEFORE-RELEASE-GATE"),
    ("python3 scripts/s6_rollback.py --execute", "S3_TEST", "R-DEPLOY-BEFORE-RELEASE-GATE"),
    ("kubectl delete namespace prod", "S6_DEPLOY_CLOSE", "R-KUBECTL-DELETE-NS"),
    ("kubectl --context x delete ns prod", "S6_DEPLOY_CLOSE", "R-KUBECTL-DELETE-NS"),
    ("kubectl get secrets -A -o yaml", "S4_REVIEW", "R-KUBECTL-SECRETS"),
    ("kubectl -n prod describe secret db-password", "S4_REVIEW", "R-KUBECTL-SECRETS"),
    ("gcloud run services list --impersonate-service-account=sa@p.iam.gserviceaccount.com", "S4_REVIEW", "R-IMPERSONATE-SA"),
    ("echo $AUDIT_HMAC_KEY", "S3_TEST", "R-SECRETS-ECHO"),
    ("printf '%s' ${github_token}", "S3_TEST", "R-SECRETS-ECHO"),
    ("printenv GITHUB_TOKEN", "S3_TEST", "R-SECRETS-ECHO"),
    ("gcloud secrets versions access latest --secret=audit-key", "S5_RELEASE_GATE", "R-SECRETS-ECHO"),
    ("cat .env", "S2_CODE", "R-SECRETS-ECHO"),
    ("echo stage=S9 > .workflow/workflow.yaml", "S2_CODE", "R-SHELL-PROTECTED-PATH-WRITE"),
    ("sed -i s/60/100/ dreamstore/scorecard.json", "S3_TEST", "R-SHELL-PROTECTED-PATH-WRITE"),
    ("cp /tmp/p.json config/dp001_guard_policy.json", "S4_REVIEW", "R-SHELL-PROTECTED-PATH-WRITE"),
])
def test_v11_blocks(v11, command, stage, rule_id):
    decision = evaluate(bash(command, stage), v11)
    assert decision.blocked and decision.rule_id == rule_id and decision.policy_version == "1.1.0"
    assert decision.reason


@pytest.mark.parametrize("command,stage", [
    ("ls -la", "S2_CODE"),
    ("python -m pytest -q -p no:cacheprovider", "S3_TEST"),
    ("rm -rf ./dist", "S2_CODE"),
    ("rm -rf build/ node_modules/.cache", "S2_CODE"),
    ("git push origin feature/console", "S2_CODE"),
    ("git push --force origin feature/console", "S2_CODE"),
    ("git reset --hard HEAD~1", "S2_CODE"),
    ("gcloud run deploy osprey-core --source . --region us-central1 --iap --no-allow-unauthenticated", "S6_DEPLOY_CLOSE"),
    ("gcloud run deploy osprey-core --source . --iap", "S5_RELEASE_GATE"),
    ("python3 scripts/s6_rollback.py", "S3_TEST"),
    ("kubectl get pods -n prod", "S4_REVIEW"),
    ("cat .workflow/workflow.yaml", "S2_CODE"),
    ("echo $HOME && echo 'h\u00e9llo w\u00f6rld'", "S2_CODE"),
])
def test_v11_allows(v11, command, stage):
    decision = evaluate(bash(command, stage), v11)
    assert decision.decision == "allow" and decision.rule_id == "ALLOW_NO_RULE_MATCHED"


def test_nbsp_obfuscated_force_push_still_blocked(v11):
    assert evaluate(bash("git\u00a0push --force origin\u00a0main"), v11).blocked


@pytest.mark.parametrize("path,stage,blocked", [
    (".workflow/workflow.yaml", "S1_SPEC", False),
    (".workflow/workflow.yaml", "S2_CODE", True),
    ("/workspace/osprey-core/.workflow/task_backend.py", "S4_REVIEW", True),
    ("WORKFLOW.yaml", "S3_TEST", True),
    ("app/../.workflow/workflow.yaml", "S2_CODE", True),
    (".WORKFLOW/Workflow.YAML", "S2_CODE", True),
    (".workflow\\workflow.yaml", "S2_CODE", True),
    ("dreamstore/scorecard.json", "S1_SPEC", True),
    ("dreamstore/proposals/DP-001.json", "S6_DEPLOY_CLOSE", True),
    ("/abs/osprey-core/config/dp001_guard_policy.json", "S1_SPEC", True),
    ("dreamstore/hooks/dp001_pre_tool_guard.py", "S2_CODE", True),
    ("app/app.py", "S2_CODE", False),
    ("config/rollouts.json", "S2_CODE", False),
])
def test_v11_file_write_gates(v11, path, stage, blocked):
    assert evaluate(write(path, stage), v11).blocked is blocked


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("MultiEdit", "file_path"), ("write_file", "path"),
                                      ("replace", "absolute_path"), ("edit_file", "TargetFile"), ("NotebookEdit", "notebook_path")])
def test_every_write_tool_shape_is_guarded(v11, tool, key):
    assert evaluate(write("dreamstore/scorecard.json", "S3_TEST", tool, key), v11).rule_id == "R-PROTECTED-DREAM-ARTIFACTS"


@pytest.mark.parametrize("tool,key", [("run_shell_command", "command"), ("run_command", "CommandLine"), ("shell", "cmd")])
def test_every_shell_tool_shape_is_guarded(v11, tool, key):
    event = {"tool_name": tool, "tool_input": {key: "kubectl get secrets"}, "stage": "S2_CODE"}
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
    event = {"tool_name": "Bash", "tool_input": {"command": "echo ok \u2603", "n": -0.0, "big": 10 ** 400}, "stage": "S2_CODE"}
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
