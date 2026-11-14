"""DP-001 `ops-release-guardrails` policy model: validation, version resolution, and pure evaluation.

`evaluate(event, policy)` is the single decision function shared by the PreToolUse hook
(dreamstore/hooks/dp001_pre_tool_guard.py), the shadow replay (dreamstore/replay.py), and the API.
It performs no I/O and reads no clock, so replaying curated scenario traces is deterministic.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from typing import Any, Mapping

if __package__:
    from dreamstore.hooks.dp001_normalize import normalize_shell
else:  # imported as a sibling module when the hook runs as a script
    from dp001_normalize import normalize_shell  # type: ignore[no-redef]

ALLOW = "allow"
BLOCK = "block"
SHELL = "shell"
FILE_WRITE = "file_write"
UNKNOWN_VERSION = "unknown"
RULE_KINDS = frozenset({"command_regex", "path_glob", "stage_gate"})


class PolicyError(Exception):
    """The policy document is missing, unreadable, malformed, or semantically invalid."""

    taxonomy = "POLICY_INVALID"


@dataclass(frozen=True)
class Decision:
    """Outcome of evaluating one tool call against one policy version."""

    decision: str
    rule_id: str
    reason: str
    policy_version: str

    @property
    def blocked(self) -> bool:
        return self.decision != ALLOW

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class _Subject:
    text: str
    paths: tuple[str, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)


def _is_str_list(value: Any, allow_empty: bool = False) -> bool:
    if not isinstance(value, list) or (not value and not allow_empty):
        return False
    return all(isinstance(item, str) and item for item in value)


def _require_text(doc: Mapping[str, Any], key: str) -> None:
    _require(isinstance(doc.get(key), str) and bool(doc[key]), f"{key} must be a non-empty string")


def _require_positive(io_cfg: Mapping[str, Any], key: str, integer: bool) -> None:
    value = io_cfg.get(key)
    numeric = (int,) if integer else (int, float)
    ok = isinstance(value, numeric) and not isinstance(value, bool) and value > 0
    _require(ok, f"io.{key} must be a positive {'integer' if integer else 'number'}")


def regex_flags(rule: Mapping[str, Any]) -> int:
    """Combine the rule's named `re` flags (only real re.RegexFlag members are accepted)."""
    flags = 0
    for name in rule.get("flags", []):
        flag = getattr(re, str(name), None)
        _require(isinstance(flag, re.RegexFlag), f"rule {rule.get('id')}: unknown regex flag {name!r}")
        flags |= flag
    return flags


def _validate_rule(rule: Any, stage_order: list[str], seen: set[str], version: str) -> None:
    _require(isinstance(rule, dict), f"version {version}: every rule must be an object")
    rule_id = rule.get("id")
    _require(isinstance(rule_id, str) and bool(rule_id), f"version {version}: rule without an id")
    _require(rule_id not in seen, f"version {version}: duplicate rule id {rule_id}")
    seen.add(rule_id)
    kind = rule.get("kind")
    _require(kind in RULE_KINDS, f"rule {rule_id}: unknown kind {kind!r}")
    _require(isinstance(rule.get("reason"), str) and bool(rule["reason"]), f"rule {rule_id}: reason is required")
    expected_class = FILE_WRITE if kind == "path_glob" else SHELL
    _require(rule.get("tool_class") == expected_class, f"rule {rule_id}: kind {kind} needs tool_class {expected_class}")
    if kind == "path_glob":
        allowed = rule.get("allowed_stages")
        _require(_is_str_list(rule.get("globs")), f"rule {rule_id}: globs must be a non-empty string list")
        _require(_is_str_list(allowed, allow_empty=True), f"rule {rule_id}: allowed_stages must be a string list")
        _require(set(allowed) <= set(stage_order), f"rule {rule_id}: allowed_stages must come from stage_order")
        return
    flags = regex_flags(rule)
    patterns, unless = rule.get("patterns"), rule.get("unless_patterns", [])
    _require(_is_str_list(patterns), f"rule {rule_id}: patterns must be a non-empty string list")
    _require(_is_str_list(unless, allow_empty=True), f"rule {rule_id}: unless_patterns must be a string list")
    for pattern in [*patterns, *unless]:
        try:
            re.compile(pattern, flags)
        except re.error as exc:
            raise PolicyError(f"rule {rule_id}: invalid regex {pattern!r}: {exc}") from exc
    if kind == "stage_gate":
        _require(rule.get("min_stage") in stage_order, f"rule {rule_id}: min_stage must come from stage_order")


def validate_policy_document(doc: Any) -> dict[str, Any]:
    """Deep semantic validation of the versioned policy; raises PolicyError on any defect."""
    _require(isinstance(doc, dict), "policy must be a JSON object")
    stage_order = doc.get("stage_order")
    _require(_is_str_list(stage_order), "stage_order must be a non-empty string list")
    _require(len(set(stage_order)) == len(stage_order), "stage_order contains duplicates")
    _require(doc.get("default_decision") in (ALLOW, BLOCK), "default_decision must be allow or block")
    _require_text(doc, "policy_id")
    _require_text(doc, "active_version")
    _require_text(doc, "stage_env_var")
    _require_text(doc, "version_env_var")
    io_cfg = doc.get("io")
    _require(isinstance(io_cfg, dict), "io must be an object")
    _require_positive(io_cfg, "max_stdin_bytes", integer=True)
    _require_positive(io_cfg, "read_chunk_bytes", integer=True)
    _require_positive(io_cfg, "stdin_timeout_seconds", integer=False)
    classes = doc.get("tool_classes")
    _require(isinstance(classes, dict), "tool_classes must be an object")
    for name in (SHELL, FILE_WRITE):
        spec = classes.get(name)
        ok = isinstance(spec, dict) and _is_str_list(spec.get("tool_names")) and _is_str_list(spec.get("input_keys"))
        _require(ok, f"tool_classes.{name} needs non-empty tool_names and input_keys")
    versions = doc.get("versions")
    _require(isinstance(versions, dict) and bool(versions), "versions must be a non-empty object")
    for version, body in versions.items():
        _require(isinstance(body, dict) and isinstance(body.get("rules"), list), f"version {version}: rules list required")
        seen: set[str] = set()
        for rule in body["rules"]:
            _validate_rule(rule, stage_order, seen, version)
    for key in ("active_version", "baseline_version", "candidate_version"):
        if key in doc:
            _require(doc[key] in versions, f"{key} {doc[key]!r} is not a defined version")
    return doc


def resolve_policy(doc: Mapping[str, Any], version: str) -> dict[str, Any]:
    """Select one immutable version of a validated policy document."""
    versions = doc.get("versions") if isinstance(doc, Mapping) else None
    if not isinstance(versions, Mapping) or version not in versions:
        raise PolicyError(f"policy version {version!r} is not defined")
    return {
        "policy_id": doc["policy_id"],
        "version": version,
        "default_decision": doc["default_decision"],
        "stage_order": list(doc["stage_order"]),
        "tool_classes": doc["tool_classes"],
        "rules": versions[version]["rules"],
    }


def diff_rules(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, list[str]]:
    """Rule ids added, removed, and modified between two resolved policy versions."""
    old = {rule["id"]: rule for rule in baseline["rules"]}
    new = {rule["id"]: rule for rule in candidate["rules"]}
    return {
        "added": [rule_id for rule_id in new if rule_id not in old],
        "removed": [rule_id for rule_id in old if rule_id not in new],
        "modified": [rule_id for rule_id in new if rule_id in old and new[rule_id] != old[rule_id]],
    }


def _path_candidates(raw: str) -> tuple[str, ...]:
    """Normalized, lower-cased path plus every suffix, so absolute paths match repo-relative globs."""
    norm = posixpath.normpath(raw.strip().replace("\\", "/")).lower()
    parts = [part for part in norm.split("/") if part and part != "."]
    return tuple("/".join(parts[index:]) for index in range(len(parts)))


def _tool_class(tool_name: str, classes: Mapping[str, Any]) -> str | None:
    for name, spec in classes.items():
        if tool_name in spec["tool_names"]:
            return name
    return None


def _subject(tool_class: str, tool_input: Mapping[str, Any], input_keys: list[str], cwd: Any = None) -> _Subject | None:
    for key in input_keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            if tool_class == FILE_WRITE:
                return _Subject(text=value, paths=_path_candidates(value))
            return _Subject(text=normalize_shell(value, cwd), paths=())
    return None


def _regex_hit(rule: Mapping[str, Any], text: str) -> bool:
    flags = regex_flags(rule)
    if not any(re.search(pattern, text, flags) for pattern in rule["patterns"]):
        return False
    return not any(re.search(pattern, text, flags) for pattern in rule.get("unless_patterns", []))


def _rule_hit(rule: Mapping[str, Any], subject: _Subject, stage: str | None, stage_order: list[str]) -> str | None:
    kind = rule["kind"]
    if kind == "path_glob":
        if stage in rule["allowed_stages"]:
            return None
        globs = [glob.lower() for glob in rule["globs"]]
        if any(fnmatchcase(candidate, glob) for candidate in subject.paths for glob in globs):
            return f"{rule['reason']} (stage {stage or 'unset'} may not write this path)"
        return None
    if kind not in RULE_KINDS:
        raise PolicyError(f"rule {rule.get('id')}: unknown kind {kind!r}")
    if not _regex_hit(rule, subject.text):
        return None
    if kind == "command_regex":
        return str(rule["reason"])
    min_stage = rule["min_stage"]
    if stage not in stage_order:
        return f"{rule['reason']} (workflow stage is {stage or 'unset'}; {min_stage} or later required)"
    if stage_order.index(stage) < stage_order.index(min_stage):
        return f"{rule['reason']} (current stage {stage} is before {min_stage})"
    return None


def _no_match(policy: Mapping[str, Any], version: str, rule_id: str, why: str) -> Decision:
    if policy["default_decision"] == ALLOW:
        return Decision(ALLOW, rule_id, why, version)
    return Decision(BLOCK, "BLOCK_DEFAULT_DECISION", f"{why}; policy default_decision is block", version)


def _evaluate(event: Any, policy: Mapping[str, Any], version: str) -> Decision:
    if not isinstance(event, Mapping):
        return Decision(BLOCK, "FAIL_CLOSED_MALFORMED_EVENT", "event must be a JSON object", version)
    tool_name, tool_input, stage = event.get("tool_name"), event.get("tool_input"), event.get("stage")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return Decision(BLOCK, "FAIL_CLOSED_MALFORMED_EVENT", "tool_name must be a non-empty string", version)
    if not isinstance(tool_input, Mapping):
        return Decision(BLOCK, "FAIL_CLOSED_MALFORMED_EVENT", "tool_input must be a JSON object", version)
    if stage is not None and not isinstance(stage, str):
        return Decision(BLOCK, "FAIL_CLOSED_MALFORMED_EVENT", "stage must be a string when present", version)
    tool_class = _tool_class(tool_name, policy["tool_classes"])
    if tool_class is None:
        return _no_match(policy, version, "ALLOW_UNGUARDED_TOOL", f"{tool_name} is neither a shell nor a file-write tool")
    subject = _subject(tool_class, tool_input, policy["tool_classes"][tool_class]["input_keys"], event.get("cwd"))
    if subject is None:
        return Decision(BLOCK, "FAIL_CLOSED_MISSING_INPUT", f"{tool_name} input has no non-empty command or path", version)
    for rule in policy["rules"]:
        if rule["tool_class"] != tool_class:
            continue
        reason = _rule_hit(rule, subject, stage or None, policy["stage_order"])
        if reason is not None:
            return Decision(BLOCK, str(rule["id"]), reason, version)
    return _no_match(policy, version, "ALLOW_NO_RULE_MATCHED", "no ops-release-guardrails rule matched")


def evaluate(event: Mapping[str, Any], policy: Mapping[str, Any]) -> Decision:
    """Pure decision function (no I/O, no clock). A defective policy blocks instead of raising."""
    version = str(policy.get("version", UNKNOWN_VERSION)) if isinstance(policy, Mapping) else UNKNOWN_VERSION
    try:
        return _evaluate(event, policy, version)
    except (KeyError, TypeError, ValueError, AttributeError, re.error, PolicyError) as exc:
        return Decision(BLOCK, "FAIL_CLOSED_POLICY_INVALID", f"policy could not be applied: {type(exc).__name__}: {exc}", version)
