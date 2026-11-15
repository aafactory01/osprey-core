"""SLA / SLO health computed from real request telemetry (no hardcoded health numbers).

Targets come from config/health.json; measurements come from the in-process telemetry ring:
success ratio = share of sampled /api requests without a 5xx status, and latency percentile uses
the nearest-rank method over measured perf_counter latencies. With fewer samples than
`min_samples` an SLO reports INSUFFICIENT_DATA instead of guessing.
"""

from __future__ import annotations

import math
from http import HTTPStatus
from typing import Any, Mapping, Sequence

from app.config_store import CONFIG_SCHEMA, ConfigError

HEALTHY = "HEALTHY"
BREACHED = "BREACHED"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
MET = "MET"
HEALTH_FILE = "config/health.json"


def _samples(events: Sequence[Mapping[str, Any]], prefix: str) -> list[Mapping[str, Any]]:
    return [
        event for event in events
        if event.get("kind") == "http_request" and str(event.get("path", "")).startswith(prefix)
        and isinstance(event.get("http_status"), int) and isinstance(event.get("latency_ms"), int)
    ]


def _nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    rank = max(math.ceil(percentile * len(ordered)), 1)
    return ordered[rank - 1]


def error_budget_remaining(target: float, failures: int, total: int) -> float:
    """Compute remaining error budget fraction for a success-ratio SLO target."""
    allowed = 1 - target
    observed = failures / total
    return 1 - observed / allowed if allowed > 0 else (1 if failures == 0 else 0)


def _success_ratio(slo: Mapping[str, Any], samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    if "target" not in slo:
        raise ConfigError(CONFIG_SCHEMA, HEALTH_FILE, f"SLO {slo['id']} (success_ratio) needs a target")
    failures = sum(1 for event in samples if event["http_status"] >= HTTPStatus.INTERNAL_SERVER_ERROR)
    measured = (len(samples) - failures) / len(samples)
    remaining = error_budget_remaining(slo["target"], failures, len(samples))
    return {
        "target": slo["target"],
        "measured": measured,
        "state": MET if measured >= slo["target"] else BREACHED,
        "failures": failures,
        "error_budget_remaining": remaining,
    }


def _latency(slo: Mapping[str, Any], samples: list[Mapping[str, Any]]) -> dict[str, Any]:
    if "percentile" not in slo or "target_ms" not in slo:
        raise ConfigError(CONFIG_SCHEMA, HEALTH_FILE, f"SLO {slo['id']} (latency_percentile) needs percentile and target_ms")
    measured = _nearest_rank([event["latency_ms"] for event in samples], slo["percentile"])
    return {
        "percentile": slo["percentile"],
        "target_ms": slo["target_ms"],
        "measured_ms": measured,
        "state": MET if measured <= slo["target_ms"] else BREACHED,
    }


def evaluate_slo(slo: Mapping[str, Any], samples: list[Mapping[str, Any]], min_samples: int) -> dict[str, Any]:
    base = {"id": slo["id"], "name": slo["name"], "kind": slo["kind"], "description": slo["description"],
            "sample_size": len(samples), "min_samples": min_samples}
    if len(samples) < min_samples:
        return {**base, "state": INSUFFICIENT_DATA}
    if slo["kind"] == "success_ratio":
        return {**base, **_success_ratio(slo, samples)}
    return {**base, **_latency(slo, samples)}


def compute_health(cfg: Mapping[str, Any], events: Sequence[Mapping[str, Any]], service: str) -> dict[str, Any]:
    """Overall status: BREACHED if any SLO breached, else INSUFFICIENT_DATA if any lacks data, else HEALTHY."""
    samples = _samples(events, cfg["sample_path_prefix"])
    slos = [evaluate_slo(slo, samples, cfg["min_samples"]) for slo in cfg["slos"]]
    states = {slo["state"] for slo in slos}
    status = BREACHED if BREACHED in states else INSUFFICIENT_DATA if INSUFFICIENT_DATA in states else HEALTHY
    return {
        "service": service,
        "status": status,
        "sla": cfg["sla"],
        "slos": slos,
        "sample_size": len(samples),
        "source_label": cfg["source_label"],
    }
