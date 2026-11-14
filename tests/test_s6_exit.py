"""S6 exit gate (IAP probe): verify/reprobe/preflight checks, cold starts, token secrecy, and fault injection."""

from __future__ import annotations

import io
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import s6_exit
from scripts.cicd_common import CicdError, Deps, RunResult, seal
from tests.helpers import load_json

CICD = load_json("config/cicd.json")
COMMIT = "3f1c2a9b8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a"
SHORT = COMMIT[:7]
BUILD_ID = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
DIGEST = "sha256:" + "4d" * 32
REPO = CICD["ci"]["image_repository"]
IMAGE, OLD_IMAGE = f"{REPO}@{DIGEST}", f"{REPO}@sha256:" + "0a" * 32
RELEASE, REVISION, OLD_REVISION = f"rel-{SHORT}-20261114-224500", "osprey-core-00007-abc", "osprey-core-00006-ng5"
URLS = ["https://osprey-core.example.test", "https://osprey-core-alt.example.test"]
TOKEN = ".".join(["test-header", "test-payload", "test-signature"])
ONBOARD = "serviceAccount:osprey-onboard-sa@gement02.iam.gserviceaccount.com"
IAP_AGENT = "serviceAccount:service-1234@gcp-sa-iap.iam.gserviceaccount.com"
LOGIN = "https://accounts.google.com/o/oauth2/v2/auth?client=redacted"


def service_doc(revision=REVISION, image=IMAGE, traffic=None, annotations=None, template=None, account=None):
    meta = {"run.googleapis.com/iap-enabled": "true", "run.googleapis.com/ingress": "all",
            "run.googleapis.com/urls": json.dumps(URLS), **(annotations or {})}
    scale = {"autoscaling.knative.dev/minScale": "1", "autoscaling.knative.dev/maxScale": "1", **(template or {})}
    return {"metadata": {"annotations": {k: v for k, v in meta.items() if v is not None}},
            "spec": {"template": {"metadata": {"annotations": {k: v for k, v in scale.items() if v is not None}},
                                  "spec": {"serviceAccountName": account or CICD["runtime_service_account"],
                                           "containers": [{"image": image}]}}},
            "status": {"url": URLS[0], "latestReadyRevisionName": revision,
                       "traffic": traffic or [{"revisionName": revision, "latestRevision": True, "percent": 100}]}}


def policy(role, members):
    return {"bindings": [{"role": role, "members": members}, {"role": "roles/viewer", "members": ["domain:example.test"]}]}


class FakeGcloud:
    def __init__(self, service=None, revision_image=IMAGE, invokers=None, accessors=None, token=TOKEN, fail=None):
        self.calls, self.fail = [], fail or {}
        self.bodies = {"service": service or service_doc(), "revision": {"spec": {"containers": [{"image": revision_image}]}},
                       "run_iam": policy("roles/run.invoker", [ONBOARD, IAP_AGENT] if invokers is None else invokers),
                       "iap_iam": policy("roles/iap.httpsResourceAccessor", [ONBOARD] if accessors is None else accessors),
                       "token": token}

    def __call__(self, cmd, timeout):
        key = "token" if cmd[0] == "print-token" else {("run", "services", "describe"): "service",
                                                       ("run", "revisions", "describe"): "revision",
                                                       ("run", "services", "get-iam-policy"): "run_iam",
                                                       ("iap", "web", "get-iam-policy"): "iap_iam"}[tuple(cmd[1:4])]
        self.calls.append(key)
        if key in self.fail:
            return RunResult(1, "", self.fail[key])
        body = self.bodies[key]
        return RunResult(0, body if isinstance(body, str) else json.dumps(body), "")


def provenance(**override):
    doc = {"commit_sha": COMMIT, "short_sha": SHORT, "build_id": BUILD_ID, "image_digest": DIGEST, "release": RELEASE,
           "pipeline": "osprey-core-cd", "target": "osprey-core-prod", "revision": REVISION, "service": "osprey-core"}
    return {"provenance": {**doc, **override}}


class FakeHttp:
    """Responses keyed by (authenticated, path); a list is consumed in order (last one repeats)."""

    def __init__(self, routes=None):
        self.calls = []
        self.routes = {(False, "/"): s6_exit.HttpResponse(302, {"location": LOGIN}, b""),
                       (True, "/"): s6_exit.HttpResponse(200, {}, b"<html></html>"),
                       (True, "/health"): s6_exit.HttpResponse(200, {}, b'{"status": "ok"}'),
                       (True, "/ready"): s6_exit.HttpResponse(200, {}, b'{"status": "ready"}'),
                       (True, "/api/pipeline"): s6_exit.HttpResponse(200, {}, json.dumps(provenance()).encode()),
                       **(routes or {})}

    def __call__(self, url, headers, timeout, max_bytes):
        base = next(prefix for prefix in URLS if url.startswith(prefix))
        auth = headers.get("Proxy-Authorization")
        self.calls.append((base, url[len(base):], auth))
        answer = self.routes[(auth is not None, url[len(base):])]
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def resp(status, body=b"", **headers):
    return s6_exit.HttpResponse(status, headers, body if isinstance(body, bytes) else json.dumps(body).encode())


@pytest.fixture
def files(tmp_path):
    packet = seal({"schema": CICD["release_gate"]["packet_schema"], "commit_sha": COMMIT, "short_sha": SHORT,
                   "build_id": BUILD_ID, "project_id": "gement02", "image_tag_ref": f"{REPO}:{SHORT}",
                   "image_ref": IMAGE, "image_digest": DIGEST, "pytest_summary": "561 passed", "created_at": "x"})
    (tmp_path / "packet.json").write_text(json.dumps(packet))
    (tmp_path / "report.json").write_text(json.dumps({"ok": True, "release": RELEASE, "revision": REVISION, "image": IMAGE}))
    return tmp_path


def probe(files, mode="verify", gcloud=None, http=None, token_command="print-token --audience iap", environ=None):
    sleeps = []
    deps = Deps(runner=gcloud or FakeGcloud(), http=http or FakeHttp(), sleep=sleeps.append, clock=lambda: 0.0,
                utcnow=lambda: datetime(2026, 11, 14, 23, 0, tzinfo=timezone.utc), environ=environ or {})
    rc, summary = s6_exit.run_exit(files / "packet.json", files / "report.json", mode, token_command,
                                   files / "exit.json", deps=deps)
    return rc, summary, json.loads((files / "exit.json").read_text()), sleeps


def test_verify_passes_every_check_and_never_leaks_the_token(files):
    gcloud, http = FakeGcloud(), FakeHttp()
    rc, summary, report, _ = probe(files, gcloud=gcloud, http=http)
    assert rc == 0 and report["ok"] is True and report["failed_checks"] == [] and summary["checks_passed"] > 20
    assert gcloud.calls == ["service", "revision", "token", "run_iam", "iap_iam"]
    assert [path for _, path, auth in http.calls if auth is None] == ["/", "/"]
    assert sorted({base for base, _, _ in http.calls}) == sorted(URLS) and len(http.calls) == 10
    assert {auth for _, _, auth in http.calls if auth} == {f"Bearer {TOKEN}"}
    checks = {check["check"] for check in report["checks"]}
    assert {"unauthenticated_gate", "authenticated_get", "provenance", "service_annotation", "template_annotation",
            "runtime_service_account", "full_traffic", "template_image", "serving_revision", "serving_image",
            "iam_run_invoker", "iam_iap_accessor", "iam_iap_agent_invoker"} == checks
    assert TOKEN not in (files / "exit.json").read_text() and TOKEN not in json.dumps(summary)


def test_cli_output_and_telemetry_never_contain_the_token(files, capsys):
    out = io.StringIO()
    deps = Deps(runner=FakeGcloud(), http=FakeHttp(), sleep=lambda _: None, clock=lambda: 0.0, environ={})
    code = s6_exit.main(["--packet", str(files / "packet.json"), "--report", str(files / "report.json"),
                         "--token-command", "print-token", "--out", str(files / "exit.json")], deps=deps, stdout=out)
    captured = capsys.readouterr()
    assert code == 0 and len(out.getvalue().splitlines()) == 1
    assert TOKEN not in out.getvalue() + captured.err + captured.out and URLS[0] not in captured.err
    calls = [json.loads(line) for line in captured.err.splitlines() if '"external_call"' in line]
    assert len(calls) == 15 and all(isinstance(event["latency_ms"], int) and event["trace_id"] for event in calls)


def test_token_command_falls_back_to_the_environment(files):
    rc, _, _, _ = probe(files, token_command=None, environ={"IAP_TOKEN_COMMAND": "print-token"})
    assert rc == 0


def test_401_is_an_acceptable_anonymous_answer(files):
    assert probe(files, http=FakeHttp({(False, "/"): resp(401)}))[0] == 0


def test_preflight_probes_health_only_without_image_checks(files):
    gcloud, http = FakeGcloud(service=service_doc(image=OLD_IMAGE, revision=OLD_REVISION)), FakeHttp()
    rc, _, report, _ = probe(files, mode="preflight", gcloud=gcloud, http=http)
    assert rc == 0 and "revision" not in gcloud.calls
    assert sorted(path for _, path, auth in http.calls if auth) == ["/health", "/health"]
    assert not {"template_image", "serving_image", "provenance"} & {check["check"] for check in report["checks"]}


def test_reprobe_passes_once_the_rejected_image_is_gone(files):
    http = FakeHttp({(True, "/api/pipeline"): resp(404)})  # a restored revision may predate /api/pipeline
    gcloud = FakeGcloud(service=service_doc(revision=OLD_REVISION), revision_image=OLD_IMAGE)
    rc, _, report, _ = probe(files, mode="reprobe", gcloud=gcloud, http=http)
    assert rc == 0 and report["serving_revision"] == OLD_REVISION
    assert sorted({path for _, path, auth in http.calls if auth}) == ["/", "/health", "/ready"]


def test_reprobe_fails_while_the_rejected_image_still_serves(files):
    rc, _, report, _ = probe(files, mode="reprobe")
    assert rc == 1 and report["failed_checks"] == ["rejected_image_gone"]


@pytest.mark.parametrize("routes,failed", [
    ({(False, "/"): resp(200, b"<html>open</html>")}, "unauthenticated_gate"),
    ({(False, "/"): resp(302, location="https://evil.example.test/login")}, "unauthenticated_gate"),
    ({(False, "/"): resp(302)}, "unauthenticated_gate"),
    ({(False, "/"): resp(301, location=LOGIN)}, "unauthenticated_gate"),
    ({(False, "/"): resp(403)}, "unauthenticated_gate"),
    ({(True, "/api/pipeline"): resp(403)}, "authenticated_get"),
    ({(True, "/"): resp(401)}, "authenticated_get"),
    ({(True, "/ready"): resp(404)}, "authenticated_get"),
    ({(True, "/api/pipeline"): resp(200, provenance(commit_sha="f" * 40))}, "provenance"),
    ({(True, "/api/pipeline"): resp(200, provenance(release="rel-other"))}, "provenance"),
    ({(True, "/api/pipeline"): resp(200, provenance(revision=OLD_REVISION))}, "provenance"),
    ({(True, "/api/pipeline"): resp(200, {"stages": []})}, "provenance"),
    ({(True, "/api/pipeline"): resp(200, b"<html>not json</html>")}, "provenance"),
    ({(True, "/api/pipeline"): resp(200, b"\xff\xfe")}, "provenance"),
])
def test_http_gate_failures(files, routes, failed):
    rc, summary, report, _ = probe(files, http=FakeHttp(routes))
    assert rc == 1 and report["failed_checks"] == [failed] and summary["error_taxonomy"] == "EXIT_GATE_FAILED"


@pytest.mark.parametrize("gcloud,failed", [
    (FakeGcloud(service=service_doc(annotations={"run.googleapis.com/iap-enabled": None})), "service_annotation"),
    (FakeGcloud(service=service_doc(annotations={"run.googleapis.com/ingress": "internal"})), "service_annotation"),
    (FakeGcloud(service=service_doc(template={"autoscaling.knative.dev/minScale": "0"})), "template_annotation"),
    (FakeGcloud(service=service_doc(template={"autoscaling.knative.dev/maxScale": "3"})), "template_annotation"),
    (FakeGcloud(service=service_doc(account="default-compute@gement02.iam.gserviceaccount.com")),
     "runtime_service_account"),
    (FakeGcloud(service=service_doc(image=OLD_IMAGE)), "template_image"),
    (FakeGcloud(revision_image=OLD_IMAGE), "serving_image"),
    (FakeGcloud(service=service_doc(revision=OLD_REVISION)), "serving_revision"),
    (FakeGcloud(service=service_doc(traffic=[{"revisionName": REVISION, "percent": 50},
                                             {"revisionName": OLD_REVISION, "percent": 50}])), "full_traffic"),
    (FakeGcloud(invokers=[IAP_AGENT]), "iam_run_invoker"),
    (FakeGcloud(accessors=[]), "iam_iap_accessor"),
    (FakeGcloud(invokers=[ONBOARD]), "iam_iap_agent_invoker"),
])
def test_service_and_iam_drift_fails(files, gcloud, failed):
    http = FakeHttp()
    if failed in ("serving_revision", "full_traffic"):
        http.routes[(True, "/api/pipeline")] = resp(200, provenance(revision=OLD_REVISION))
    rc, _, report, _ = probe(files, gcloud=gcloud, http=http)
    assert rc == 1 and failed in report["failed_checks"]
    if failed not in ("serving_revision", "full_traffic"):
        assert report["failed_checks"] == [failed]


def test_policy_without_bindings_fails_every_iam_check(files):
    gcloud = FakeGcloud()
    gcloud.bodies["run_iam"] = gcloud.bodies["iap_iam"] = {"etag": "x"}
    rc, _, report, _ = probe(files, gcloud=gcloud)
    assert rc == 1 and report["failed_checks"] == ["iam_iap_accessor", "iam_iap_agent_invoker", "iam_run_invoker"]


def test_cold_start_then_200_passes_with_configured_delay(files):
    http = FakeHttp({(True, "/health"): [resp(503), resp(429), resp(200, {"status": "ok"})],
                     (False, "/"): [CicdError("HTTP_UNREACHABLE", "connection reset", transient=True), resp(401)]})
    rc, _, _, sleeps = probe(files, http=http)
    assert rc == 0 and sleeps and set(sleeps) == {CICD["exit_probe"]["cold_start_delay_seconds"]}


@pytest.mark.parametrize("answer,taxonomy", [
    (resp(503), "COLD_START_EXHAUSTED"),
    (CicdError("HTTP_UNREACHABLE", "timed out", transient=True), "HTTP_UNREACHABLE"),
])
def test_persistent_cold_start_is_transient(files, answer, taxonomy):
    rc, summary, report, sleeps = probe(files, http=FakeHttp({(True, "/health"): answer}))
    assert rc == 75 and summary["error_taxonomy"] == taxonomy and report["ok"] is False
    assert len(sleeps) == CICD["exit_probe"]["cold_start_attempts"] - 1


@pytest.mark.parametrize("kwargs,rc,taxonomy", [
    ({"token_command": None}, 1, "TOKEN_COMMAND_MISSING"),
    ({"token_command": "print-token 'unbalanced"}, 1, "TOKEN_COMMAND_INVALID"),
    ({"gcloud": FakeGcloud(token="not a token\n")}, 1, "TOKEN_MALFORMED"),
    ({"gcloud": FakeGcloud(token="")}, 1, "TOKEN_MALFORMED"),
    ({"gcloud": FakeGcloud(fail={"token": "mint_id_token failed: refresh expired"})}, 75, "TOKEN_UNAVAILABLE"),
    ({"gcloud": FakeGcloud(fail={"service": "ERROR: HTTPError 503: Service Unavailable"})}, 75, "UNAVAILABLE"),
    ({"gcloud": FakeGcloud(fail={"run_iam": "ERROR: PERMISSION_DENIED: 403"})}, 1, "PERMISSION_DENIED"),
    ({"gcloud": FakeGcloud(fail={"iap_iam": "ERROR: HTTPError 429: Too Many Requests"})}, 75, "RATE_LIMITED"),
    ({"gcloud": FakeGcloud(service="{oops")}, 1, "MALFORMED_GCLOUD_JSON"),
    ({"gcloud": FakeGcloud(service=[1, 2])}, 1, "MALFORMED_GCLOUD_JSON"),
    ({"mode": "sideways"}, 1, "BAD_MODE"),
])
def test_tooling_faults(files, kwargs, rc, taxonomy):
    code, summary, report, _ = probe(files, **kwargs)
    assert code == rc and summary["error_taxonomy"] == taxonomy and report["ok"] is False


def test_unverified_deploy_report_blocks_verify(files):
    (files / "report.json").write_text(json.dumps({"ok": False, "release": RELEASE}))
    assert probe(files)[1]["error_taxonomy"] == "REPORT_NOT_OK"
    (files / "packet.json").unlink()
    assert probe(files)[1]["error_taxonomy"] == "PACKET_MISSING"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server hook name
        self.send_response(302 if self.path == "/" else 200)
        self.send_header("Location", LOGIN)
        self.end_headers()
        self.wfile.write(b"x" * 64)

    def log_message(self, *args):
        return None


def test_real_client_never_follows_redirects_and_bounds_the_body():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        redirect = s6_exit.urllib_client(base + "/", {}, 5, 16)
        ok = s6_exit.urllib_client(base + "/health", {"Proxy-Authorization": "Bearer x"}, 5, 16)
    finally:
        server.shutdown()
        server.server_close()
    assert redirect.status == 302 and redirect.headers["location"] == LOGIN
    assert ok.status == 200 and ok.body == b"x" * 16


def test_real_client_connection_refused_is_transient():
    probe_socket = HTTPServer(("127.0.0.1", 0), _Handler)
    port = probe_socket.server_port
    probe_socket.server_close()
    with pytest.raises(CicdError) as info:
        s6_exit.urllib_client(f"http://127.0.0.1:{port}/", {}, 2, 16)
    assert info.value.taxonomy == "HTTP_UNREACHABLE" and info.value.transient is True
