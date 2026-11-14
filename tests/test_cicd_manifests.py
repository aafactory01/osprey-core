"""Manifest <-> config sync for the delivery pipeline: cloudbuild.yaml, clouddeploy.yaml, skaffold.yaml,
run-service.yaml, .gcloudignore/.dockerignore, and config/cicd.json (no network; PyYAML only)."""

from __future__ import annotations

import re

import pytest
import yaml

from app.config_store import REPO_ROOT
from tests.helpers import load_json

CICD = load_json("config/cicd.json")
REPO = CICD["ci"]["image_repository"]
TAG = f"{REPO}:$SHORT_SHA"
STEP_IDS = ["guard-substitutions", "unit-tests", "docker-build", "container-smoke", "docker-push", "release-packet"]
BUILTINS = {"COMMIT_SHA", "SHORT_SHA", "BUILD_ID", "PROJECT_ID", "LOCATION"}
PACKET_FIELDS = ["schema", "commit_sha", "short_sha", "build_id", "project_id", "image_tag_ref", "image_ref",
                 "image_digest", "pytest_summary", "created_at"]
OWNED = ["cloudbuild.yaml", "clouddeploy.yaml", "skaffold.yaml", "run-service.yaml", ".gcloudignore", ".dockerignore",
         "config/cicd.json", "config/rollback.json", "scripts/cicd_common.py", "scripts/s5_release_gate.py",
         "scripts/s6_deploy.py", "scripts/s6_exit.py", "scripts/s6_rollback.py", "scripts/workflow_ctl.py"]


def text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def build() -> dict:
    return yaml.safe_load(text("cloudbuild.yaml"))


def steps() -> dict:
    return {step["id"]: step for step in build()["steps"]}


def script(step_id: str) -> str:
    return steps()[step_id]["args"][-1]


def dockerfile_base() -> str:
    return re.search(r"^FROM\s+(\S+)", text("Dockerfile"), re.MULTILINE).group(1)


def test_step_ids_match_the_bounce_routes():
    gate = CICD["release_gate"]
    assert [step["id"] for step in build()["steps"]] == STEP_IDS
    assert list(gate["bounce_routes"]) == STEP_IDS
    stages = set(CICD["stage_ids"].values())
    assert set(gate["bounce_routes"].values()) <= stages
    assert {gate["default_bounce"], gate["crosscheck_bounce"], CICD["deploy"]["bounce_to"]} <= stages
    assert gate["bounce_routes"] == {"guard-substitutions": "S4_VERIFY_QA", "unit-tests": "S3_BUILD",
                                     "docker-build": "S3_BUILD", "container-smoke": "S4_VERIFY_QA",
                                     "docker-push": "S3_BUILD", "release-packet": "S4_VERIFY_QA"}


def test_image_is_tagged_with_short_sha_only():
    raw = text("cloudbuild.yaml")
    assert REPO == "us-central1-docker.pkg.dev/gement02/osprey-scion/osprey-core"
    assert TAG in raw and ":latest" not in raw
    assert f"--tag={TAG}" in steps()["docker-build"]["args"]
    assert steps()["docker-push"]["args"] == ["push", TAG]
    assert build()["images"] == [TAG]
    assert "--label=org.opencontainers.image.revision=$COMMIT_SHA" in steps()["docker-build"]["args"]


def test_builders_are_digest_pinned_and_tests_use_the_dockerfile_base():
    for step in build()["steps"]:
        assert re.search(r"@sha256:[0-9a-f]{64}$", step["name"]), step["name"]
    assert steps()["unit-tests"]["name"] == dockerfile_base() == steps()["guard-substitutions"]["name"]


def test_only_builtin_substitutions_under_must_match():
    raw = text("cloudbuild.yaml")
    used = set(re.findall(r"(?<!\$)\$\{?([A-Z_][A-Z0-9_]*)", raw.replace("$$", "")))
    assert used == BUILTINS
    assert build()["options"]["substitutionOption"] == "MUST_MATCH"
    assert "substitutions" not in build()


@pytest.mark.parametrize("step_id", ["guard-substitutions", "unit-tests", "container-smoke", "release-packet"])
def test_bash_scripts_escape_every_dollar_and_take_values_from_env(step_id):
    body = script(step_id)
    assert "$" not in body.replace("$$", ""), "unescaped $ would be eaten by Cloud Build substitution"
    for env in steps()[step_id].get("env", []):
        assert "=" in env and not env.startswith("$")


def test_guard_enforces_formats_prefix_and_location():
    body, env = script("guard-substitutions"), steps()["guard-substitutions"]["env"]
    assert "CI_COMMIT_SHA=$COMMIT_SHA" in env and "CI_SHORT_SHA=$SHORT_SHA" in env
    assert "^[0-9a-f]{7}$$" in body and "^[0-9a-f]{40}$$" in body
    assert '$$CI_COMMIT_SHA == "$$CI_SHORT_SHA"*' in body
    assert f"$$CI_PROJECT_ID == {CICD['project_id']}" in body and f"$$CI_LOCATION == {CICD['region']}" in body


def test_unit_tests_run_full_pytest_as_non_root_with_pipefail():
    body = script("unit-tests")
    assert "set -euo pipefail" in body and "pip install --quiet -r requirements-dev.txt" in body
    assert "os.setuid(u.pw_uid)" in body and '"-m", "pytest"' in body
    assert body.rstrip().endswith("| tee /workspace/pytest_report.txt")


def test_container_smoke_probes_health_and_workflow_on_the_cloudbuild_network():
    body = script("container-smoke")
    assert body.count("--network cloudbuild") == 2 and "docker rm -f" in body and "trap " in body
    assert "/healthz /api/workflow" in body and '"application/json"' in body


def test_release_packet_carries_every_field_and_the_pushed_digest():
    body = script("release-packet")
    for field in PACKET_FIELDS:
        assert f'"{field}":' in body, field
    assert f'"schema":"{CICD["release_gate"]["packet_schema"]}"' in body
    assert "^sha256:[0-9a-f]{64}$$" in body and "RepoDigests" in body
    assert "> /workspace/release_packet.json" in body


def test_artifacts_upload_packet_and_report_per_short_sha():
    gate = CICD["release_gate"]
    objects = build()["artifacts"]["objects"]
    assert objects["location"] == f"{gate['artifact_prefix']}/$SHORT_SHA/"
    assert objects["paths"] == [gate["packet_artifact"], gate["pytest_artifact"]]
    assert gate["artifact_prefix"].startswith("gs://gement02_cloudbuild/")


def test_clouddeploy_pipeline_target_location_and_execution_sa():
    docs = list(yaml.safe_load_all(text("clouddeploy.yaml")))
    pipeline = next(doc for doc in docs if doc["kind"] == "DeliveryPipeline")
    target = next(doc for doc in docs if doc["kind"] == "Target")
    cd = CICD["cd"]
    assert pipeline["metadata"]["name"] == cd["delivery_pipeline"] == "osprey-core-cd"
    assert [stage["targetId"] for stage in pipeline["serialPipeline"]["stages"]] == [cd["target"]]
    assert target["metadata"]["name"] == cd["target"] == "osprey-core-prod"
    assert target["run"]["location"] == f"projects/{CICD['project_id']}/locations/{CICD['region']}"
    assert target["run"]["location"] == "projects/gement02/locations/us-central1"
    config = target["executionConfigs"][0]
    assert config["serviceAccount"] == cd["execution_service_account"] == "osprey-onboard-sa@gement02.iam.gserviceaccount.com"
    assert set(config["usages"]) == {"RENDER", "DEPLOY"} and target["requireApproval"] is False


def test_skaffold_renders_run_service_without_build():
    doc = yaml.safe_load(text("skaffold.yaml"))
    assert re.fullmatch(r"skaffold/v4beta\d+", doc["apiVersion"]) and doc["kind"] == "Config"
    assert "build" not in doc
    assert doc["manifests"]["rawYaml"] == [CICD["cd"]["service_manifest"]] == ["run-service.yaml"]
    assert doc["deploy"] == {"cloudrun": {}}


def service() -> dict:
    return yaml.safe_load(text("run-service.yaml"))


def container() -> dict:
    return service()["spec"]["template"]["spec"]["containers"][0]


def test_run_service_preserves_live_service_settings():
    svc = service()
    assert svc["apiVersion"] == "serving.knative.dev/v1" and svc["kind"] == "Service"
    assert svc["metadata"]["name"] == CICD["service"]
    assert svc["metadata"]["annotations"] == {"run.googleapis.com/ingress": "all", "run.googleapis.com/iap-enabled": "true"}
    template = svc["spec"]["template"]
    assert template["metadata"]["annotations"] == {"autoscaling.knative.dev/minScale": "1",
                                                   "autoscaling.knative.dev/maxScale": "1",
                                                   "run.googleapis.com/startup-cpu-boost": "true"}
    for labels in (svc["metadata"]["labels"], template["metadata"]["labels"]):
        assert labels["app"] == "osprey-core" and set(labels) == {"app", "commit"}
    spec = template["spec"]
    assert spec["containerConcurrency"] == 80 and spec["timeoutSeconds"] == 300
    assert spec["serviceAccountName"] == CICD["runtime_service_account"] == "osprey-core-run@gement02.iam.gserviceaccount.com"
    assert svc["spec"]["traffic"] == [{"latestRevision": True, "percent": 100}]


def test_run_service_container_matches_live_revision():
    box = container()
    assert box["image"] == CICD["cd"]["image_name"] == "osprey-core"
    assert box["ports"] == [{"name": "http1", "containerPort": 8080}]
    assert box["resources"] == {"limits": {"cpu": "1", "memory": "512Mi"}}
    assert box["startupProbe"] == {"tcpSocket": {"port": 8080}, "timeoutSeconds": 240, "periodSeconds": 240,
                                   "failureThreshold": 1}
    env = {item["name"]: item for item in box["env"]}
    assert env["OSPREY_PROJECT_SLUG"] == {"name": "OSPREY_PROJECT_SLUG", "value": "osprey-core"}
    assert env["AUDIT_HMAC_KEY"]["valueFrom"] == {"secretKeyRef": {"name": "osprey-core-audit-hmac-key", "key": "latest"}}
    names = CICD["provenance_env"]
    assert env[names["pipeline"]]["value"] == CICD["cd"]["delivery_pipeline"]
    assert env[names["target"]]["value"] == CICD["cd"]["target"]


def test_provenance_deploy_parameters_match_the_release_command():
    raw, names = text("run-service.yaml"), CICD["provenance_env"]
    params = set(re.findall(r"^\s*[^#\s].*# from-param: \$\{(\w+)\}$", raw, re.MULTILINE))
    create = " ".join(CICD["commands"]["release_create"])
    passed = set(re.findall(r"(\w+)=\{\w+\}", create.split("--deploy-parameters=", 1)[1].split(" ")[0]))
    assert params == passed == {"commit_sha", "short_sha", "build_id", "image_digest", "release"}
    for key in sorted(params - {"short_sha"}):
        assert re.search(rf"- name: {names[key]}\n\s+value: \"unset\" # from-param: \$\{{{key}\}}", raw), key
    assert len(re.findall(r'commit: "unset" # from-param: \$\{short_sha\}', raw)) == 2
    assert re.search(r'- name: OSPREY_SHORT_SHA\n\s+value: "unset" # from-param: \$\{short_sha\}', raw)
    assert "--images={image_name}={image_ref}" in create and "--skaffold-file={skaffold_file}" in create


def test_submit_command_overrides_builtin_substitutions():
    submit = CICD["commands"]["build_submit"]
    assert "--substitutions=COMMIT_SHA={commit_sha},SHORT_SHA={short_sha}" in submit
    assert "--async" in submit and "--format=value(id)" in submit and "--region={region}" in submit


def test_ignore_files_keep_image_small_but_upload_tests():
    gcloud = text(".gcloudignore").split()
    docker = text(".dockerignore").split()
    assert "#!include:.dockerignore" in gcloud and "#!include:.gitignore" in gcloud
    for needed in ("!tests/", "!requirements-dev.txt", "!pytest.ini", "!.gitignore", "!.gcloudignore",
                   "!skaffold.yaml", "!run-service.yaml", "!templates/"):
        assert needed in gcloud, needed
    assert gcloud.index("#!include:.dockerignore") < gcloud.index("!tests/")
    for excluded in ("cloudbuild.yaml", "clouddeploy.yaml", "skaffold.yaml", "run-service.yaml", "/templates/",
                     "**/evidence/", "pytest_report.txt", "release_packet.json", "tests/", "evidence/"):
        assert excluded in docker, excluded
    assert "templates/" not in docker, "an unanchored templates/ would drop app/templates from the image"


@pytest.mark.parametrize("name", OWNED)
def test_public_repo_hygiene_for_pipeline_files(name):
    body = text(name)
    assert ".run.app" not in body and not re.search(r"(?<!\d)\d{12}(?!\d)", body)
    emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", body)
    assert all(email.endswith(".iam.gserviceaccount.com") for email in emails), emails
    assert len(body.splitlines()) <= 400
