"""S6 deploy: packet validation, Cloud Deploy release + rollout polling, serving verification, and chaos."""

from __future__ import annotations

import io
import json
import re
import subprocess
from datetime import datetime, timezone

import pytest

from scripts import s6_deploy
from scripts.cicd_common import Deps, RunResult, seal
from tests.helpers import load_json

CICD = load_json("config/cicd.json")
COMMIT = "3f1c2a9b8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a"
SHORT = COMMIT[:7]
BUILD_ID = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
DIGEST = "sha256:" + "4d" * 32
OLD_DIGEST = "sha256:" + "0a" * 32
REPO = CICD["ci"]["image_repository"]
IMAGE = f"{REPO}@{DIGEST}"
RELEASE = f"rel-{SHORT}-20261114-224500"
REVISION = "osprey-core-00007-abc"
URLS = ["https://osprey-core.example.test", "https://osprey-core-alt.example.test"]
NOW = datetime(2026, 11, 14, 22, 45, tzinfo=timezone.utc)


def packet_doc(**override):
    doc = {"schema": CICD["release_gate"]["packet_schema"], "commit_sha": COMMIT, "short_sha": SHORT,
           "build_id": BUILD_ID, "project_id": "gement02", "image_tag_ref": f"{REPO}:{SHORT}", "image_ref": IMAGE,
           "image_digest": DIGEST, "pytest_summary": "561 passed in 9.84s", "created_at": "2026-11-14T22:40:00Z"}
    doc.update(override)
    return seal(doc)


def env_for(release=RELEASE, **override):
    values = {"OSPREY_COMMIT_SHA": COMMIT, "OSPREY_SHORT_SHA": SHORT, "OSPREY_CI_BUILD_ID": BUILD_ID,
              "OSPREY_IMAGE_DIGEST": DIGEST, "OSPREY_CD_RELEASE": release, "OSPREY_CD_PIPELINE": "osprey-core-cd",
              "OSPREY_CD_TARGET": "osprey-core-prod", **override}
    return [{"name": key, "value": value} for key, value in values.items() if value is not None]


def service_doc(traffic=None, urls=URLS):
    return {"metadata": {"annotations": {"run.googleapis.com/urls": json.dumps(urls)}},
            "status": {"url": urls[0], "latestReadyRevisionName": REVISION,
                       "traffic": traffic or [{"revisionName": REVISION, "latestRevision": True, "percent": 100}]}}


def revision_doc(image=IMAGE, env=None):
    return {"metadata": {"name": REVISION}, "spec": {"containers": [{"image": image, "env": env_for() if env is None else env}]}}


def rollout(state, target="osprey-core-prod", name="0001"):
    return {"name": f"projects/gement02/locations/us-central1/deliveryPipelines/osprey-core-cd/releases/{RELEASE}"
                    f"/rollouts/{RELEASE}-to-{target}-{name}", "targetId": target, "state": state,
            "createTime": f"2026-11-14T22:45:{name[-2:]}Z", "failureReason": None if state != "FAILED" else "RELEASE_FAILED"}


class FakeCloud:
    """Scripted git + gcloud for the deploy path; rollouts list answers come from a queue."""

    def __init__(self, rollouts=None, build=None, tag_digest=DIGEST, service=None, revision=None, status="",
                 head=COMMIT, fail=None, raise_on=None):
        self.calls, self.status, self.head, self.fail, self.raise_on = [], status, head, fail or {}, raise_on or {}
        self.rollouts = list(rollouts or [[rollout("IN_PROGRESS")], [rollout("SUCCEEDED")]])
        self.build = build or {"status": "SUCCESS", "results": {"images": [{"name": f"{REPO}:{SHORT}", "digest": DIGEST}]}}
        self.tag = {"image_summary": {"digest": tag_digest}} if tag_digest else {}
        self.service, self.revision = service or service_doc(), revision or revision_doc()

    def key(self, cmd):
        if cmd[0] == "git":
            return "git_status" if "status" in cmd else "git_head"
        table = {("builds", "describe"): "build", ("artifacts", "docker"): "tag", ("deploy", "releases"): "create",
                 ("deploy", "rollouts"): "rollouts", ("run", "services"): "service", ("run", "revisions"): "revision"}
        return table[(cmd[1], cmd[2])]

    def __call__(self, cmd, timeout):
        key = self.key(cmd)
        self.calls.append((key, cmd, timeout))
        if key in self.raise_on:
            raise self.raise_on[key]
        if key in self.fail:
            return RunResult(1, "", self.fail[key])
        if key == "rollouts":
            body = self.rollouts.pop(0) if len(self.rollouts) > 1 else self.rollouts[0]
        else:
            body = {"git_status": self.status, "git_head": self.head, "build": self.build, "tag": self.tag,
                    "create": "", "service": self.service, "revision": self.revision}[key]
        return RunResult(0, body if isinstance(body, str) else json.dumps(body), "")

    def keys(self):
        return [key for key, _, _ in self.calls]


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def write_packet(tmp_path, doc=None):
    path = tmp_path / "packet.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(packet_doc() if doc is None else doc), encoding="utf-8")
    return path


def deploy(runner, tmp_path, packet=None, resume=None, clock=None):
    clock = clock or Clock()
    deps = Deps(runner=runner, sleep=clock.sleep, clock=lambda: clock.now, utcnow=lambda: NOW)
    path = packet if packet is not None else write_packet(tmp_path)
    rc, summary = s6_deploy.run_deploy(path, tmp_path / "report.json", tmp_path, resume_release=resume, deps=deps)
    report = json.loads((tmp_path / "report.json").read_text())
    return rc, summary, report


def test_happy_path_creates_release_polls_rollout_and_verifies_serving(tmp_path):
    runner, clock = FakeCloud(), Clock()
    rc, summary, report = deploy(runner, tmp_path, clock=clock)
    assert rc == 0 and report["ok"] is True and summary["ok"] is True
    assert runner.keys() == ["git_status", "git_head", "build", "tag", "create", "rollouts", "rollouts", "service",
                             "revision"]
    create = runner.calls[4][1]
    assert create[:5] == ["gcloud", "deploy", "releases", "create", RELEASE]
    assert f"--images=osprey-core={IMAGE}" in create and f"--source={tmp_path.resolve()}" in create
    assert "--skaffold-file=skaffold.yaml" in create and "--delivery-pipeline=osprey-core-cd" in create
    assert (f"--deploy-parameters=commit_sha={COMMIT},short_sha={SHORT},build_id={BUILD_ID},"
            f"image_digest={DIGEST},release={RELEASE}") in create
    assert runner.calls[4][2] == CICD["deploy"]["create_timeout_seconds"]
    assert clock.sleeps == [CICD["deploy"]["poll_interval_seconds"]]
    assert report["release"] == RELEASE and report["rollout"] == f"{RELEASE}-to-osprey-core-prod-0001"
    assert report["state"] == "SUCCEEDED" and report["revision"] == REVISION and report["image"] == IMAGE
    assert report["new_revision_serving"] is True and report["urls"] == URLS and "urls" not in summary
    assert all(isinstance(value, int) for value in report["timings"].values())
    assert report["packet_sha256"] == packet_doc()["packet_sha256"]


def test_release_name_is_a_valid_cloud_deploy_id():
    assert len(RELEASE) <= 63 and re.fullmatch(CICD["formats"]["resource_name"], RELEASE)


def test_resume_release_polls_without_creating(tmp_path):
    runner = FakeCloud(rollouts=[[rollout("SUCCEEDED")]])
    rc, _, report = deploy(runner, tmp_path, resume=RELEASE)
    assert rc == 0 and "create" not in runner.keys() and report["release"] == RELEASE


def test_cli_requires_packet_and_prints_one_line(tmp_path):
    with pytest.raises(SystemExit):
        s6_deploy.parse_args([])
    out = io.StringIO()
    deps = Deps(runner=FakeCloud(rollouts=[[rollout("SUCCEEDED")]]), sleep=lambda _: None, clock=lambda: 0.0,
                utcnow=lambda: NOW)
    code = s6_deploy.main(["--packet", str(write_packet(tmp_path)), "--report", str(tmp_path / "r.json"),
                           "--source", str(tmp_path)], deps=deps, stdout=out)
    assert code == 0 and len(out.getvalue().splitlines()) == 1 and json.loads(out.getvalue())["release"] == RELEASE


@pytest.mark.parametrize("doc,taxonomy", [
    ({**packet_doc(), "image_digest": "sha256:" + "1" * 64}, "PACKET_MISMATCH"),
    ({**packet_doc(), "pytest_summary": "edited after sealing"}, "PACKET_TAMPERED"),
    ({**packet_doc(), "packet_sha256": "0" * 64}, "PACKET_TAMPERED"),
    (packet_doc(schema="osprey-core/release-packet/v0"), "PACKET_SCHEMA"),
    (packet_doc(image_digest="sha256:XYZ"), "BAD_IMAGE_DIGEST"),
    (packet_doc(short_sha="aaaaaaa"), "PACKET_MISMATCH"),
    (packet_doc(image_ref=f"us-docker.pkg.dev/other/repo/osprey-core@{DIGEST}"), "PACKET_MISMATCH"),
    (packet_doc(project_id="another-project"), "PACKET_MISMATCH"),
    (packet_doc(build_id="not-a-uuid"), "BAD_BUILD_ID"),
    ("{broken", "PACKET_MALFORMED"),
    ("[1, 2, 3]", "PACKET_MALFORMED"),
])
def test_bad_packets_fail_before_any_cloud_call(tmp_path, doc, taxonomy):
    runner = FakeCloud()
    rc, summary, report = deploy(runner, tmp_path, packet=write_packet(tmp_path, doc))
    assert rc == 1 and summary["error_taxonomy"] == taxonomy and report["ok"] is False and runner.calls == []


def test_missing_packet(tmp_path):
    rc, summary, _ = deploy(FakeCloud(), tmp_path, packet=tmp_path / "nope.json")
    assert rc == 1 and summary["error_taxonomy"] == "PACKET_MISSING"


@pytest.mark.parametrize("kwargs,taxonomy", [
    ({"status": " M run-service.yaml\n"}, "DIRTY_SOURCE"),
    ({"head": "e" * 40}, "HEAD_MISMATCH"),
    ({"build": {"status": "FAILURE"}}, "BUILD_NOT_SUCCESS"),
    ({"build": ["not", "a", "dict"]}, "BUILD_NOT_SUCCESS"),
    ({"build": {"status": "SUCCESS", "results": {"images": [{"name": f"{REPO}:{SHORT}", "digest": OLD_DIGEST}]}}},
     "BUILD_DIGEST_MISMATCH"),
    ({"build": {"status": "SUCCESS"}}, "BUILD_DIGEST_MISMATCH"),
    ({"tag_digest": OLD_DIGEST}, "TAG_DIGEST_MISMATCH"),
    ({"tag_digest": None}, "TAG_DIGEST_MISMATCH"),
])
def test_prechecks_fail_closed_before_creating_a_release(tmp_path, kwargs, taxonomy):
    runner = FakeCloud(**kwargs)
    rc, summary, report = deploy(runner, tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == taxonomy and "create" not in runner.keys()
    assert report["release"] is None and report["new_revision_serving"] is None


def test_failed_rollout_with_old_revision_still_serving(tmp_path):
    runner = FakeCloud(rollouts=[[rollout("FAILED")]], revision=revision_doc(image=f"{REPO}@{OLD_DIGEST}"))
    rc, summary, report = deploy(runner, tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == "ROLLOUT_FAILED" and report["state"] == "FAILED"
    assert report["new_revision_serving"] is False and report["failure_reason"] == "RELEASE_FAILED"
    assert report["release"] == RELEASE


@pytest.mark.parametrize("revision,taxonomy,serving", [
    (revision_doc(image=f"{REPO}@{OLD_DIGEST}"), "IMAGE_MISMATCH", False),
    (revision_doc(env=env_for(release="rel-0000000-20260101-000000")), "PROVENANCE_ENV_MISMATCH", True),
    (revision_doc(env=env_for(OSPREY_IMAGE_DIGEST=None)), "PROVENANCE_ENV_MISMATCH", True),
    (revision_doc(env=env_for(OSPREY_COMMIT_SHA="unset")), "PROVENANCE_ENV_MISMATCH", True),
    (revision_doc(env=[]), "PROVENANCE_ENV_MISMATCH", True),
    ({"metadata": {"name": REVISION}, "spec": {}}, "MALFORMED_GCLOUD_JSON", None),
])
def test_succeeded_rollout_must_serve_the_packet_with_provenance(tmp_path, revision, taxonomy, serving):
    rc, summary, report = deploy(FakeCloud(rollouts=[[rollout("SUCCEEDED")]], revision=revision), tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == taxonomy and report["new_revision_serving"] is serving


@pytest.mark.parametrize("service,taxonomy", [
    (service_doc(traffic=[{"revisionName": REVISION, "percent": 90}, {"revisionName": "old", "percent": 10}]),
     "TRAFFIC_SPLIT"),
    (service_doc(traffic=[{"percent": 100}]), "NO_SERVING_REVISION"),
    ({"metadata": {"annotations": {"run.googleapis.com/urls": "not-json"}}, "status": service_doc()["status"]},
     "MALFORMED_GCLOUD_JSON"),
    ({"status": {"traffic": [{"revisionName": REVISION, "percent": 100}]}}, "NO_SERVICE_URL"),
])
def test_service_shape_problems_fail_closed(tmp_path, service, taxonomy):
    rc, summary, _ = deploy(FakeCloud(rollouts=[[rollout("SUCCEEDED")]], service=service), tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == taxonomy


@pytest.mark.parametrize("rollouts", [[[]], [[rollout("SUCCEEDED", target="osprey-core-staging")]],
                                      [[rollout("PENDING_RELEASE")]]])
def test_rollout_that_never_finishes_is_transient_and_resumable(tmp_path, rollouts):
    clock = Clock()
    rc, summary, report = deploy(FakeCloud(rollouts=rollouts), tmp_path, clock=clock)
    assert rc == 75 and summary["error_taxonomy"] == "POLL_DEADLINE" and report["release"] == RELEASE
    assert sum(clock.sleeps) <= CICD["deploy"]["max_wait_seconds"]


@pytest.mark.parametrize("fail,raise_on,rc,taxonomy", [
    ({"create": "ERROR: (gcloud.deploy.releases.create) HTTPError 429: Too Many Requests"}, {}, 75, "RATE_LIMITED"),
    ({"create": "ERROR: (gcloud.deploy.releases.create) PERMISSION_DENIED: caller lacks permission"}, {}, 1,
     "PERMISSION_DENIED"),
    ({"create": "ERROR: ALREADY_EXISTS: release exists"}, {}, 1, "FAILED_PRECONDITION"),
    ({"rollouts": "ERROR: HTTPError 503: Service Unavailable"}, {}, 75, "UNAVAILABLE"),
    ({}, {"rollouts": subprocess.TimeoutExpired(["gcloud"], 120)}, 75, "TIMEOUT"),
    ({"service": "ERROR: HTTPError 401: Request had invalid authentication credentials."}, {}, 1, "UNAUTHENTICATED"),
    ({"build": "ERROR: HTTPError 503: Service Unavailable"}, {}, 75, "UNAVAILABLE"),
    ({"tag": "ERROR: NOT_FOUND: image could not be found"}, {}, 1, "NOT_FOUND"),
])
def test_gcloud_chaos(tmp_path, fail, raise_on, rc, taxonomy):
    code, summary, report = deploy(FakeCloud(fail=fail, raise_on=raise_on), tmp_path)
    assert code == rc and summary["error_taxonomy"] == taxonomy and report["ok"] is False
    assert summary["transient"] is (rc == 75)


@pytest.mark.parametrize("rollouts", [["not json"], [{"not": "a list"}]])
def test_malformed_rollout_listing(tmp_path, rollouts):
    rc, summary, _ = deploy(FakeCloud(rollouts=rollouts), tmp_path)
    assert rc == 1 and summary["error_taxonomy"] == "MALFORMED_GCLOUD_JSON"


def test_missing_config_fails_closed(tmp_path, temp_root):
    (temp_root / "config" / "cicd.json").unlink()
    rc, summary = s6_deploy.run_deploy(write_packet(tmp_path), tmp_path / "r.json", tmp_path, config_root=temp_root,
                                       deps=Deps(runner=FakeCloud()))
    assert rc == 1 and summary["error_taxonomy"] == "CONFIG_MISSING" and not (tmp_path / "r.json").exists()
