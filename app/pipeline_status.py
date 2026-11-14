"""Live CI/CD status for the deterministic stages: Cloud Build (release gate) and Cloud Deploy (deploy close).

GET /api/pipeline serves PipelineStatusService.status(). Provenance comes from the env vars named in
config/cicd.json `provenance_env` (Cloud Deploy deploy parameters, plus K_REVISION and K_SERVICE from
Cloud Run). With provenance the service reads, with a metadata-server token, the Cloud Build build behind
the serving image and the newest Cloud Deploy rollout of the serving release on the configured target; the
two reads run in parallel inside one deadline (config/pipeline_status.json `http.budget_ms`).

Fail closed and invent nothing: missing provenance -> source not_configured; any upstream failure -> status
UNKNOWN, source unavailable, typed error; only invalid configuration raises (ConfigError -> 503). The
document is cached per TTL (`cache.ttl_ms`, or the shorter `cache.error_ttl_ms` after a failure); one
refresh runs at a time (thread-safe). Transport, clock, and environment are injectable for tests.
"""

from __future__ import annotations

import collections
import contextvars
import copy
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable, Mapping

from app import telemetry
from app.config_store import ConfigStore
from app.pipeline_clients import ApiClient, Clock, Transport, urllib_transport
from app.pipeline_model import (
    SOURCE_UNAVAILABLE, UNAVAILABLE, UNKNOWN, PipelineError, cd_base, ci_base, degraded, fmt, live, map_build,
    map_rollouts, read_provenance, seconds, url,
)

OUTBOUND_OP = {"ci": "pipeline.cloud_build.get_build", "cd": "pipeline.cloud_deploy.list_rollouts"}
_ONE_MS = timedelta(milliseconds=1)
_Entry = collections.namedtuple("_Entry", "key doc at ttl_s")
Job = tuple[dict[str, Any], str, Callable[[Any], dict[str, Any]]]


class PipelineStatusService:
    """Builds and caches the /api/pipeline document."""

    def __init__(self, store: ConfigStore, transport: Transport | None = None, clock: Clock | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self._store = store
        self._clock = clock or time.monotonic
        self._client = ApiClient(transport or urllib_transport, self._clock)
        self._env = env
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._entry: Any = None

    def status(self) -> dict[str, Any]:
        """The current document, cached per TTL; raises ConfigError only for invalid configuration."""
        cfg, cicd = self._store.get("pipeline_status"), self._store.get("cicd")
        env = os.environ if self._env is None else self._env
        provenance = read_provenance(cicd, env, cfg["provenance_unset_values"])
        key = json.dumps([cfg, cicd, provenance], sort_keys=True)
        entry = self._fresh(key)
        if entry is None:
            with self._refresh_lock:
                entry = self._fresh(key) or self._refresh(cfg, cicd, provenance, env, key)
        doc = copy.deepcopy(entry.doc)
        doc["cache_age_ms"] = max(round(timedelta(seconds=self._clock() - entry.at) / _ONE_MS), 0)
        return doc

    def _fresh(self, key: str) -> Any:
        with self._lock:
            entry = self._entry
        if entry is None or entry.key != key or self._clock() - entry.at >= entry.ttl_s:
            return None
        return entry

    def _refresh(self, cfg: dict[str, Any], cicd: dict[str, Any], provenance: dict[str, Any], env: Mapping[str, str],
                 key: str) -> Any:
        started, trace_id = time.perf_counter(), telemetry.current_trace_id()
        fmt(cfg["labels"]["unavailable"], taxonomy=UNKNOWN)
        ci, cd = ci_base(cfg, cicd, provenance), cd_base(cfg, cicd, provenance)
        place = {"project": cicd["project_id"], "region": cicd["region"]}
        jobs: dict[str, Job] = {}
        if ci["build_id"]:
            build_url = url(cfg["ci"]["build_url_template"], build_id=ci["build_id"], **place)
            jobs["ci"] = (ci, build_url, lambda body: map_build(body, cfg, cicd, provenance))
        if provenance["deployed_by_cloud_deploy"]:
            rollouts_url = url(cfg["cd"]["rollouts_url_template"], pipeline=cd["pipeline"], release=cd["release"],
                               page_size=cfg["cd"]["page_size"], **place)
            jobs["cd"] = (cd, rollouts_url, lambda body: map_rollouts(body, cfg, cicd, cd["release"]))
        blocks = {"ci": ci, "cd": cd, **self._run(cfg, env, jobs, trace_id)}
        healthy = all(block["source"] != SOURCE_UNAVAILABLE for block in blocks.values())
        doc = {
            "source_label": cfg["source_label"],
            "stage_ids": {"release_gate": ci["stage"], "deploy_close": cd["stage"]},
            "provenance": provenance,
            "ci": blocks["ci"],
            "cd": blocks["cd"],
            "fetched_at": telemetry.utc_now_iso(),
            "cache_age_ms": 0,
        }
        entry = _Entry(key, doc, self._clock(), seconds(cfg["cache"]["ttl_ms" if healthy else "error_ttl_ms"]))
        with self._lock:
            self._entry = entry
        telemetry.record_core("pipeline.refresh", started, telemetry.STATUS_OK if healthy else telemetry.STATUS_ERROR,
                              telemetry.NO_ERROR if healthy else UNAVAILABLE, ci_source=blocks["ci"]["source"],
                              cd_source=blocks["cd"]["source"])
        return entry

    def _run(self, cfg: dict[str, Any], env: Mapping[str, str], jobs: dict[str, Job], trace_id: str) -> dict[str, Any]:
        """Token once, then the Cloud Build and Cloud Deploy reads in parallel; each job yields a block, never raises."""
        if not jobs:
            return {}
        started = time.perf_counter()
        deadline = self._clock() + seconds(cfg["http"]["budget_ms"])
        try:
            headers = self._client.auth_headers(cfg, self._client.access_token(cfg, env, deadline, trace_id))
        except PipelineError as exc:
            return {name: degraded(cfg, name, job[0], exc, started) for name, job in jobs.items()}

        def run_job(name: str) -> dict[str, Any]:
            base, job_url, mapper = jobs[name]
            try:
                fields = mapper(self._client.get_json(OUTBOUND_OP[name], job_url, headers, cfg, deadline, trace_id))
            except PipelineError as exc:
                return degraded(cfg, name, base, exc, started)
            except Exception as exc:  # never 500: an unexpected response shape degrades this block only
                telemetry.record_core(OUTBOUND_OP[name], started, telemetry.STATUS_ERROR, UNAVAILABLE,
                                      error_type=type(exc).__name__)
                return degraded(cfg, name, base, PipelineError(UNAVAILABLE, f"unexpected {type(exc).__name__}"), started)
            return live(cfg, name, base, fields, started)

        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="pipeline-status") as pool:
            futures = {name: pool.submit(contextvars.copy_context().run, run_job, name) for name in jobs}
            return {name: future.result() for name, future in futures.items()}
