"""Hybrid workflow wiring: the YAML definition, the console config, the CI/CD contract, the DP-001 stage order,
the agent roster, the Cloud Build and Cloud Deploy files, and WORKFLOW.md describe one control loop."""

from __future__ import annotations

import ast
import json
import re

import pytest
import yaml

from app.config_store import REPO_ROOT
from dreamstore.hooks.dp001_pre_tool_guard import DEFAULT_POLICY_PATH, evaluate, load_policy_document, resolve_policy

AGENTIC = ["S1_SPEC", "S2_TESTS", "S3_BUILD", "S4_VERIFY_QA"]
DETERMINISTIC = ["S5_RELEASE_GATE", "S6_DEPLOY_CLOSE"]
CI_STEPS = ["guard-substitutions", "unit-tests", "docker-build", "container-smoke", "docker-push", "release-packet"]
S5_BOUNCES = {"guard-substitutions": "S4_VERIFY_QA", "unit-tests": "S3_BUILD", "docker-build": "S3_BUILD",
              "container-smoke": "S4_VERIFY_QA", "docker-push": "S3_BUILD", "release-packet": "S4_VERIFY_QA"}
COUNCIL = {"core-reviewer-gemini", "core-reviewer-opus", "core-reviewer-fable"}
PACKET = ".workflow/evidence/s5_release_packet.json"
CLOUDBUILD = REPO_ROOT / "cloudbuild.yaml"
TEST_REF = re.compile(r"(tests/test_\w+\.py)::(test_\w+)")


def _load(relpath):
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    return yaml.safe_load(text) if relpath.endswith((".yaml", ".yml")) else json.loads(text)


@pytest.fixture(scope="module")
def workflow():
    return _load(".workflow/workflow.yaml")


@pytest.fixture(scope="module")
def console():
    return _load("config/workflow_stages.json")


@pytest.fixture(scope="module")
def cicd():
    return _load("config/cicd.json")


def _stages(doc):
    return {stage["id"]: stage for stage in doc["stages"]}


def _bounce_targets(stage):
    route = stage.get("bounce_to")
    if route is None:
        return []
    values = route.values() if isinstance(route, dict) else [route] if isinstance(route, str) else route
    return sorted(set(values))


def bash(command, stage):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "stage": stage}


def test_yaml_and_console_describe_the_same_stages(workflow, console):
    ids = [stage["id"] for stage in workflow["stages"]]
    assert ids == [stage["id"] for stage in console["stages"]] == AGENTIC + DETERMINISTIC
    for ours, theirs in zip(workflow["stages"], console["stages"]):
        assert (ours["title"], ours["execution"], ours["owners"]) == (theirs["title"], theirs["mode"], theirs["owners"])
        assert ours["entry_gates"] == theirs["entry_gates"] and ours["exit_gates"] == theirs["exit_gates"]
        assert _bounce_targets(ours) == _bounce_targets(theirs), ours["id"]
    assert workflow["version"] == console["version"] and console["current_stage"] in ids


def test_contract_stage_ids_follow_the_workflow_order(workflow, cicd):
    assert list(cicd["stage_ids"].values()) == [stage["id"] for stage in workflow["stages"]]


def test_guard_stage_order_follows_the_workflow(workflow):
    policy = load_policy_document(DEFAULT_POLICY_PATH)
    assert policy["stage_order"] == [stage["id"] for stage in workflow["stages"]]


def test_agentic_stages_are_graded_and_deterministic_stages_run_scripts(workflow):
    stages = _stages(workflow)
    for stage_id in AGENTIC:
        stage = stages[stage_id]
        assert stage["execution"] == "agentic" and "command" not in stage and stage["exit_gate"].strip()
        assert "bounce_to" not in stage and "rollback_command" not in stage
    for stage_id in DETERMINISTIC:
        stage = stages[stage_id]
        assert stage["execution"] == "deterministic"
        assert stage["command"].startswith("python3 scripts/") and stage["exit_gate"].startswith("python3 scripts/")


def test_agentic_exit_gates_point_at_real_checks(workflow):
    for stage in _stages(workflow).values():
        if stage["execution"] != "agentic":
            continue
        for file, name in TEST_REF.findall(stage["exit_gate"]):
            tree = ast.parse((REPO_ROOT / file).read_text(encoding="utf-8"))
            assert name in {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}, name
        script = re.search(r"\b(qa/\w+\.py)\b", stage["exit_gate"])
        assert script is None or (REPO_ROOT / script.group(1)).is_file()


def test_s5_bounces_each_cloud_build_step_to_the_stage_that_owns_the_fix(workflow):
    gate = _stages(workflow)["S5_RELEASE_GATE"]
    assert gate["bounce_to"] == S5_BOUNCES and list(gate["bounce_to"]) == gate["ci"]["steps"] == CI_STEPS
    assert set(gate["bounce_to"].values()) <= {"S3_BUILD", "S4_VERIFY_QA"}
    assert gate["command"] == "python3 scripts/s5_release_gate.py --commit <HEAD sha>"
    assert gate["ci"]["packet"] == PACKET and PACKET in gate["evidence"]


def test_s6_probes_rolls_back_then_bounces_to_build(workflow):
    close = _stages(workflow)["S6_DEPLOY_CLOSE"]
    assert close["bounce_to"] == "S3_BUILD"
    assert close["command"] == f"python3 scripts/s6_deploy.py --packet {PACKET}"
    assert close["exit_gate"] == f"python3 scripts/s6_exit.py --packet {PACKET} --report {close['cd']['report']}"
    assert close["rollback_command"] == "python3 scripts/s6_rollback.py --execute"
    steps = " ".join(close["on_failure"]).lower()
    assert steps.index("rollback_command") < steps.index("re-run exit_gate") < steps.index("bounce to s3_build")


def test_every_owner_is_on_the_roster_and_qa_is_independent(workflow, console):
    roster = {agent["id"] for agent in _load("config/agents.json")["agents"]}
    for doc in (workflow, console):
        assert all(set(stage["owners"]) <= roster for stage in doc["stages"])
    stages = _stages(workflow)
    verify = set(stages["S4_VERIFY_QA"]["owners"])
    assert COUNCIL <= verify and "core-iap-security" in verify
    assert not verify & set(stages["S3_BUILD"]["owners"])


def test_ci_and_cd_blocks_match_the_contract(workflow, console, cicd):
    stages, panels = _stages(workflow), _stages(console)
    ci, cd, contract = stages["S5_RELEASE_GATE"]["ci"], stages["S6_DEPLOY_CLOSE"]["cd"], cicd["cd"]
    assert (ci["provider"], ci["config"], ci["image_repository"]) == (
        cicd["ci"]["provider"], cicd["ci"]["config_file"], cicd["ci"]["image_repository"])
    assert ci["submit"] == f"gcloud builds submit --config={cicd['ci']['config_file']}"
    assert (cd["provider"], cd["config"], cd["skaffold"], cd["service_manifest"], cd["pipeline"], cd["target"]) == (
        contract["provider"], contract["config_file"], contract["skaffold_file"], contract["service_manifest"],
        contract["delivery_pipeline"], contract["target"])
    assert cd["register"] == f"gcloud deploy apply --file={contract['config_file']} --region={cicd['region']} --project={cicd['project_id']}"
    assert contract["execution_service_account"].startswith(cd["execution_service_account"] + "@")
    assert cicd["runtime_service_account"].startswith(cd["runtime_service_account"] + "@")
    for stage_id, block, kind in (("S5_RELEASE_GATE", ci, "ci"), ("S6_DEPLOY_CLOSE", cd, "cd")):
        assert panels[stage_id]["pipeline"] == {"kind": kind, "provider": block["provider"], "config_file": block["config"],
                                                "command": stages[stage_id]["command"]}


def test_failure_modes_follow_the_kit(workflow):
    modes = {mode["mode"]: mode for mode in workflow["failure_modes"]}
    assert sorted(modes) == [1, 2, 3, 4, 5] and all(mode["name"] and mode["action"] for mode in modes.values())
    assert modes[4]["exit_code"] == 75 and modes[4]["backoff_seconds"] == [1, 2, 4]
    assert all(stage["max_attempts"] == modes[5]["strikes"] for stage in workflow["stages"])
    assert {stage["id"] for stage in workflow["stages"] if stage["human_gate"] == "required"} == {"S1_SPEC", "S5_RELEASE_GATE"}


def test_workflow_commands_pass_the_guard_only_from_their_stage(workflow):
    doc = load_policy_document(DEFAULT_POLICY_PATH)
    policy, order, stages = resolve_policy(doc, doc["active_version"]), doc["stage_order"], _stages(workflow)
    gate, close = stages["S5_RELEASE_GATE"], stages["S6_DEPLOY_CLOSE"]
    commands = [(gate["command"], gate["id"]), (gate["ci"]["submit"], gate["id"]), (workflow["controller"], gate["id"]),
                (close["command"], close["id"]), (close["cd"]["register"], close["id"]), (close["rollback_command"], close["id"])]
    for command, home in commands:
        earlier = order[order.index(home) - 1]
        assert evaluate(bash(command, earlier), policy).blocked, (command, earlier)
        assert evaluate(bash(command, home), policy).decision == "allow", (command, home)


def test_cloud_deploy_file_matches_the_contract(cicd):
    docs = {doc["kind"]: doc for doc in yaml.safe_load_all((REPO_ROOT / cicd["cd"]["config_file"]).read_text(encoding="utf-8"))}
    pipeline, target = docs["DeliveryPipeline"], docs["Target"]
    assert pipeline["metadata"]["name"] == cicd["cd"]["delivery_pipeline"]
    assert [stage["targetId"] for stage in pipeline["serialPipeline"]["stages"]] == [cicd["cd"]["target"]]
    assert target["metadata"]["name"] == cicd["cd"]["target"]
    assert target["run"]["location"] == f"projects/{cicd['project_id']}/locations/{cicd['region']}"
    assert {cfg["serviceAccount"] for cfg in target["executionConfigs"]} == {cicd["cd"]["execution_service_account"]}


@pytest.mark.skipif(not CLOUDBUILD.exists(), reason="cloudbuild.yaml comes from the CI/CD lane; active after integration")
def test_cloud_build_steps_match_the_bounce_routes(workflow):
    steps = [step.get("id") for step in yaml.safe_load(CLOUDBUILD.read_text(encoding="utf-8"))["steps"]]
    assert steps == _stages(workflow)["S5_RELEASE_GATE"]["ci"]["steps"]


def _step_route_tables(node):
    """Mappings in config/cicd.json keyed by exactly the Cloud Build step ids (the controller's route table)."""
    if isinstance(node, dict):
        if set(node) == set(CI_STEPS):
            yield node
        for value in node.values():
            yield from _step_route_tables(value)
    elif isinstance(node, list):
        for value in node:
            yield from _step_route_tables(value)


def _route_target(value, stage_ids):
    if isinstance(value, dict):
        value = next((value[key] for key in ("bounce_to", "stage", "target") if key in value), value)
    return stage_ids.get(value, value) if isinstance(value, str) else value


def test_contract_route_table_agrees_when_declared(workflow, cicd):
    stages = _stages(workflow)
    tables = list(_step_route_tables(cicd))
    deploy = cicd.get("deploy") if isinstance(cicd.get("deploy"), dict) else {}
    if not tables and "bounce_to" not in deploy:
        pytest.skip("config/cicd.json declares no Cloud Build step route table or S6 bounce target")
    for table in tables:
        resolved = {step: _route_target(target, cicd["stage_ids"]) for step, target in table.items()}
        assert resolved == stages["S5_RELEASE_GATE"]["bounce_to"]
    if "bounce_to" in deploy:
        assert _route_target(deploy["bounce_to"], cicd["stage_ids"]) == stages["S6_DEPLOY_CLOSE"]["bounce_to"]


def test_workflow_doc_states_every_deterministic_command(workflow):
    text = (REPO_ROOT / "WORKFLOW.md").read_text(encoding="utf-8")
    stages = _stages(workflow)
    gate, close = stages["S5_RELEASE_GATE"], stages["S6_DEPLOY_CLOSE"]
    commands = [gate["command"], gate["exit_gate"], gate["ci"]["submit"], close["command"], close["exit_gate"],
                close["rollback_command"], close["cd"]["register"], workflow["controller"], workflow["pipeline_status_api"],
                "gcloud deploy releases create", "gcloud deploy targets rollback", *CI_STEPS, *stages]
    assert [command for command in commands if command not in text] == []


def test_readme_documents_ci_cd(cicd):
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    needles = [f"gcloud builds submit --config={cicd['ci']['config_file']}", f"gcloud deploy apply --file={cicd['cd']['config_file']}",
               "gcloud deploy releases create", "python3 scripts/s6_rollback.py --execute", "templates/", "13 agent templates"]
    assert [needle for needle in needles if needle not in text] == []


# Workflow, config, eval, and doc files that carry stage ids. templates/ keeps the Hub's legacy ids verbatim by design.
STAGE_ID_FILES = [".workflow/workflow.yaml", "config/workflow_stages.json", "config/task_backends.json",
                  "config/dp001_guard_policy.json", "config/audit_receipts.json", "config/agents.json",
                  "config/night_dreaming.json", "dreamstore/evals/ops_release_guardrails_shadow_replay.json",
                  "dreamstore/scorecard.json", "dreamstore/proposals/DP-001.json", "WORKFLOW.md", "SPEC.md", "AGENTS.md",
                  "README.md", "team-memory/LESSONS_LEARNED.md"]
# Built from pieces so a repository grep for the legacy ids does not match this file.
LEGACY_STAGE_IDS = re.compile(r"\bS2_" r"CODE\b|\bS3_" r"TEST\b|\bS4_" r"REVIEW\b")


def test_legacy_stage_ids_are_gone_from_workflow_files():
    paths = [REPO_ROOT / relpath for relpath in STAGE_ID_FILES] + sorted((REPO_ROOT / "team-memory" / "dreams").glob("*.md"))
    hits = [f"{path.relative_to(REPO_ROOT)}:{number}" for path in paths
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if LEGACY_STAGE_IDS.search(line)]
    assert hits == []
