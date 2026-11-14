#!/usr/bin/env python3
"""DP-001 PreToolUse guard for the `ops-release-guardrails` skill.

Hook protocol (Claude Code, Gemini CLI, and Antigravity style PreToolUse hooks):
  stdin   one JSON tool-call event: {"tool_name": str, "tool_input": {...}, "stage"?: str, "cwd"?: str}
  stdout  one JSON decision: {"decision", "rule_id", "reason", "policy_version", "trace_id", "latency_ms"}
  stderr  a human-readable reason when the call is blocked
  exit    0 = allow, 2 = block

Fail closed: empty, oversized, slow, or malformed stdin and an unreadable or invalid policy all block
with exit 2. The policy is config/dp001_guard_policy.json (immutable versions); the enforced version
is `active_version` unless DP001_POLICY_VERSION overrides it. The workflow stage comes from the
event's `stage` field, else from the WORKFLOW_STAGE environment variable.

Example:
  echo '{"tool_name":"Bash","tool_input":{"command":"git push --force origin main"}}' \\
    | python3 dreamstore/hooks/dp001_pre_tool_guard.py ; echo "exit=$?"
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
import uuid
from datetime import timedelta
from enum import IntEnum
from pathlib import Path
from typing import IO, Any, Mapping

# Decision, diff_rules, evaluate, resolve_policy, and validate_policy_document are re-exported for callers.
if __package__:
    from dreamstore.hooks.dp001_policy import (
        BLOCK, UNKNOWN_VERSION, Decision, PolicyError, diff_rules, evaluate, resolve_policy, validate_policy_document,
    )
else:  # executed as a script by a harness: the sibling module is importable from sys.path[0]
    from dp001_policy import (  # type: ignore[no-redef]
        BLOCK, UNKNOWN_VERSION, Decision, PolicyError, diff_rules, evaluate, resolve_policy, validate_policy_document,
    )

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_POLICY_PATH = REPO_ROOT / "config" / "dp001_guard_policy.json"
POLICY_PATH_ENV = "DP001_POLICY_PATH"
_ONE_MS = timedelta(milliseconds=1)


class HookExit(IntEnum):
    """PreToolUse exit-code protocol.

    BLOCK is a protocol constant (like an HTTP status code), not a tunable: harnesses treat exit 2
    as "blocking error", while any other non-zero code lets the tool call proceed. It therefore must
    be available even when the policy file cannot be read.
    """

    ALLOW = 0
    BLOCK = 2


class HookInputError(Exception):
    """stdin could not be turned into a tool-call event; carries the fail-closed rule id."""

    def __init__(self, rule_id: str, reason: str) -> None:
        super().__init__(reason)
        self.rule_id = rule_id
        self.reason = reason


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant {token!r} is not allowed")


def load_policy_document(path: Path | str) -> dict[str, Any]:
    """Read and validate the policy file; any failure raises PolicyError."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"policy is not UTF-8 ({target.name})") from exc
    except OSError as exc:
        raise PolicyError(f"policy unreadable ({target.name}): {exc.strerror or type(exc).__name__}") from exc
    try:
        doc = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise PolicyError(f"policy is not valid JSON ({target.name}): {exc}") from exc
    return validate_policy_document(doc)


def _read_fd(fd: int, limit: int, chunk_bytes: int, timeout_s: float) -> bytes:
    deadline = time.monotonic() + timeout_s
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        ready = select.select([fd], [], [], remaining)[0] if remaining > 0 else []
        if not ready:
            raise HookInputError("FAIL_CLOSED_INPUT_TIMEOUT", f"no complete event on stdin within {timeout_s}s")
        chunk = os.read(fd, chunk_bytes)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise HookInputError("FAIL_CLOSED_INPUT_TOO_LARGE", f"stdin exceeds {limit} bytes")
        chunks.append(chunk)


def read_event_bytes(source: Any, io_cfg: Mapping[str, Any]) -> bytes:
    """Read stdin with a size cap and, for real file descriptors, a deadline."""
    limit = io_cfg["max_stdin_bytes"]
    try:
        fd = source.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is not None:
        return _read_fd(fd, limit, io_cfg["read_chunk_bytes"], io_cfg["stdin_timeout_seconds"])
    data = source.read(limit + 1)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(payload) > limit:
        raise HookInputError("FAIL_CLOSED_INPUT_TOO_LARGE", f"stdin exceeds {limit} bytes")
    return payload


def parse_event(raw: bytes) -> dict[str, Any]:
    """Decode one JSON object from stdin bytes or raise HookInputError."""
    if not raw.strip():
        raise HookInputError("FAIL_CLOSED_EMPTY_INPUT", "empty PreToolUse stdin; failing closed")
    try:
        event = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise HookInputError("FAIL_CLOSED_MALFORMED_INPUT", f"stdin is not valid UTF-8 JSON ({type(exc).__name__})") from exc
    if not isinstance(event, dict):
        raise HookInputError("FAIL_CLOSED_MALFORMED_INPUT", "stdin JSON must be an object")
    return event


def _decide(source: Any, env: Mapping[str, str]) -> Decision:
    try:
        doc = load_policy_document(env.get(POLICY_PATH_ENV) or DEFAULT_POLICY_PATH)
        version = env.get(doc["version_env_var"]) or doc["active_version"]
        policy = resolve_policy(doc, version)
    except PolicyError as exc:
        return Decision(BLOCK, "FAIL_CLOSED_POLICY_UNAVAILABLE", str(exc), UNKNOWN_VERSION)
    try:
        event = parse_event(read_event_bytes(source, doc["io"]))
    except HookInputError as exc:
        return Decision(BLOCK, exc.rule_id, exc.reason, version)
    env_stage = env.get(doc["stage_env_var"])
    if not event.get("stage") and env_stage:
        event = {**event, "stage": env_stage}
    return evaluate(event, policy)


def _safe_write(stream: IO[str], text: str) -> bool:
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        return False
    return True


def main(stdin: Any = None, stdout: IO[str] | None = None, stderr: IO[str] | None = None,
         environ: Mapping[str, str] | None = None) -> int:
    """CLI entrypoint: returns HookExit.ALLOW (0) or HookExit.BLOCK (2), never any other code."""
    started = time.perf_counter()
    trace_id = uuid.uuid4().hex
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        decision = _decide(sys.stdin if stdin is None else stdin, os.environ if environ is None else environ)
    except Exception as exc:  # last resort: a crash (exit 1) would let the tool call run, so block instead
        decision = Decision(BLOCK, "FAIL_CLOSED_INTERNAL_ERROR", f"guard error: {type(exc).__name__}: {exc}", UNKNOWN_VERSION)
    latency_ms = round(timedelta(seconds=time.perf_counter() - started) / _ONE_MS)
    payload = {**decision.to_dict(), "trace_id": trace_id, "latency_ms": latency_ms}
    if not _safe_write(out, json.dumps(payload, sort_keys=True) + "\n"):
        _safe_write(err, "[DP-001] stdout unavailable; decision reported by exit code only\n")
    if decision.blocked:
        _safe_write(err, f"[DP-001 guard v{decision.policy_version}] BLOCKED by {decision.rule_id}: {decision.reason}\n")
        return int(HookExit.BLOCK)
    return int(HookExit.ALLOW)


if __name__ == "__main__":
    raise SystemExit(main())
