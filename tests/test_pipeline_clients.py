"""urllib_transport and ApiClient edge cases with a monkeypatched opener (no sockets, no network)."""

from __future__ import annotations

import email.message
import http.client
import io
import urllib.error
import urllib.request

import pytest

from app.pipeline_clients import ApiClient, urllib_transport
from app.pipeline_model import PipelineError
from tests.pipeline_fakes import CFG, FakeClock, google_error, ok

URL = "https://cloudbuild.googleapis.com/v1/projects/p/locations/r/builds/b"


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status


class FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.seen = []

    def open(self, request, timeout):
        self.seen.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture
def opener(monkeypatch):
    holder = {}

    def install(outcome):
        fake = FakeOpener(outcome)
        handlers = []

        def build_opener(*args):
            handlers.extend(args)
            return fake

        monkeypatch.setattr(urllib.request, "build_opener", build_opener)
        holder.update(fake=fake, handlers=handlers)
        return holder

    return install


def http_error(status: int, body: bytes | None):
    fp = io.BytesIO(body) if body is not None else None
    return urllib.error.HTTPError(URL, status, "error", email.message.Message(), fp)


def test_success_returns_status_and_body_and_bypasses_proxies(opener):
    holder = opener(FakeResponse(b'{"status": "SUCCESS"}'))
    assert urllib_transport(URL, {"Accept": "application/json"}, 1.5, 1024) == (200, b'{"status": "SUCCESS"}')
    request, timeout = holder["fake"].seen[0]
    assert timeout == 1.5 and request.get_method() == "GET" and request.get_header("Accept") == "application/json"
    proxies = [h for h in holder["handlers"] if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies and proxies[0].proxies == {}


def test_http_errors_are_returned_with_their_body(opener):
    opener(http_error(403, b'{"error": {"message": "denied"}}'))
    assert urllib_transport(URL, {}, 1.0, 1024) == (403, b'{"error": {"message": "denied"}}')


def test_http_error_without_body(opener):
    opener(http_error(503, None))
    assert urllib_transport(URL, {}, 1.0, 1024) == (503, b"")


@pytest.mark.parametrize("outcome,taxonomy", [
    (urllib.error.URLError(TimeoutError("timed out")), "TIMEOUT"),
    (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), "UNAVAILABLE"),
    (urllib.error.URLError("Name or service not known"), "UNAVAILABLE"),
    (TimeoutError("read timed out"), "TIMEOUT"),
    (http.client.RemoteDisconnected("closed"), "UNAVAILABLE"),
    (ConnectionResetError(104, "reset"), "UNAVAILABLE"),
    (ValueError("unknown url type"), "UNAVAILABLE"),
])
def test_network_failures_raise_typed_errors(opener, outcome, taxonomy):
    opener(outcome)
    with pytest.raises(PipelineError) as info:
        urllib_transport(URL, {}, 1.0, 1024)
    assert info.value.taxonomy == taxonomy and info.value.http_status is None


@pytest.mark.parametrize("outcome", [FakeResponse(b"x" * 11), http_error(500, b"y" * 11)])
def test_oversized_bodies_are_malformed(opener, outcome):
    opener(outcome)
    with pytest.raises(PipelineError) as info:
        urllib_transport(URL, {}, 1.0, 10)
    assert info.value.taxonomy == "MALFORMED_RESPONSE"


def _client(result):
    calls = []

    def transport(url, headers, timeout_s, max_bytes):
        calls.append(timeout_s)
        if isinstance(result, BaseException):
            raise result
        return result

    return ApiClient(transport, FakeClock()), calls


def test_get_json_decodes_and_caps_the_timeout_by_the_deadline():
    client, calls = _client(ok({"a": 1}))
    assert client.get_json("op", URL, {}, CFG, deadline=FakeClock()() + 0.25, trace_id="t") == {"a": 1}
    assert calls == [0.25]


def test_get_json_maps_google_error_messages_and_truncates_them():
    client, _ = _client(google_error(403, "x" * 1000))
    with pytest.raises(PipelineError) as info:
        client.get_json("op", URL, {}, CFG, deadline=FakeClock()() + 5, trace_id="t")
    assert info.value.taxonomy == "PERMISSION_DENIED" and info.value.http_status == 403
    assert len(info.value.detail) == CFG["http"]["max_detail_chars"]


def test_get_json_never_leaks_unexpected_exceptions():
    client, _ = _client(MemoryError("transport exploded"))
    with pytest.raises(PipelineError) as info:
        client.get_json("op", URL, {}, CFG, deadline=FakeClock()() + 5, trace_id="t")
    assert info.value.taxonomy == "UNAVAILABLE" and "MemoryError" in info.value.detail


def test_failure_taxonomy_override_keeps_the_cause():
    client, _ = _client(ok({}, 429))
    with pytest.raises(PipelineError) as info:
        client.get_json("op", URL, {}, CFG, deadline=FakeClock()() + 5, trace_id="t", failure_taxonomy="NO_CREDENTIALS")
    assert (info.value.taxonomy, info.value.detail, info.value.http_status) == ("NO_CREDENTIALS", "RATE_LIMITED: HTTP 429", 429)


def test_auth_headers_use_the_configured_scheme():
    client, _ = _client(ok({}))
    assert client.auth_headers(CFG, "tok") == {"Authorization": f"{CFG['http']['authorization_scheme']} tok",
                                               "Accept": CFG["http"]["accept"]}
