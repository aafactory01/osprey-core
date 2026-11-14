"""Honesty gates: illustrative data is labelled everywhere, provenance is truthful, references resolve."""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from app.config_store import REPO_ROOT
from tests.helpers import load_json

LABELLED = [
    ("config/workflow_stages.json", "illustrative stage progress, not live workflow state"),
    ("config/audit_receipts.json", "not a record of real gate decisions"),
    ("config/rollouts.json", "not live Cloud Run traffic"),
    ("config/task_backends.json", "nothing is created in the selected backend"),
    ("dreamstore/evals/ops_release_guardrails_shadow_replay.json", "not recorded from live sessions"),
]
PROVENANCE_FILES = [
    "config/night_dreaming.json", "config/workflow_stages.json", "config/audit_receipts.json",
    "dreamstore/evals/ops_release_guardrails_shadow_replay.json", "dreamstore/scorecard.json",
    "dreamstore/proposals/DP-001.json", "app/templates/index.html", ".workflow/workflow.yaml",
    "SPEC.md", "README.md", "WORKFLOW.md", "AGENTS.md",
]
REFERENCE_SOURCES = [
    "SPEC.md", "README.md", "WORKFLOW.md", "AGENTS.md", "team-memory/LESSONS_LEARNED.md", ".workflow/workflow.yaml",
    "config/workflow_stages.json", "config/audit_receipts.json", "config/rollouts.json", "config/night_dreaming.json",
    "config/agents.json", "config/task_backends.json",
]
# Harness settings files that operators create in their own checkout; documented as snippets, never committed.
HARNESS_SNIPPETS = {".claude/settings.json", ".gemini/settings.json"}
PATH_TOKEN = re.compile(r"(?<![\w./$-])((?:\.?[\w-]+/)*[\w.-]+\.(?:md|json|py|yaml|yml|js|css|html|txt|ini))(?![\w/-])")
SPEC_REF = re.compile(r"(tests/test_\w+\.py)::(test_\w+)")


@pytest.mark.parametrize("relpath,phrase", LABELLED)
def test_illustrative_configs_declare_their_provenance(relpath, phrase):
    label = load_json(relpath)["source_label"]
    assert phrase in label and relpath in label


def test_sample_scenarios_are_flagged():
    for relpath in ("config/workflow_stages.json", "config/audit_receipts.json", "config/rollouts.json"):
        assert load_json(relpath)["scenario"] == "sample", relpath
    assert "not recorded from live sessions" in load_json("config/night_dreaming.json")["proposal"]["evidence_basis"]


def test_apis_return_source_labels(client):
    workflow = client.get("/api/workflow").get_json()
    assert workflow["scenario"] == "sample" and "not live workflow state" in workflow["source_label"]
    assert "not live Cloud Run traffic" in client.get("/api/rollouts").get_json()["source_label"]
    for path in ("/api/task-backends", "/api/task-backend"):
        assert client.get(path).get_json()["source_label"].startswith("Projection only"), path
    selected = client.post("/api/task-backend", json={"backend": "beads"}).get_json()
    assert selected["source_label"].startswith("Projection only")
    dreams = client.get("/api/dreams").get_json()
    assert "not recorded from live sessions" in dreams["replay"]["eval_source_label"]
    assert "Curated scenario traces" in dreams["proposal"]["evidence_basis"]
    receipts = client.get("/api/receipts").get_json()
    assert receipts["scenario"] == "sample" and "not a record of real gate decisions" in receipts["source_label"]
    assert all(item["receipt"]["scenario"] == "sample" for item in receipts["receipts"])


def test_scenario_flag_is_signed(client):
    item = client.get("/api/receipts").get_json()["receipts"][0]
    forged = copy.deepcopy(item["receipt"])
    forged["scenario"] = "live"
    body = client.post("/api/receipts/verify", json={"receipt": forged, "signature": item["signature"]}).get_json()
    assert body["verdict"] == "FAIL" and body["checks"]["signature"] is False


def test_ui_renders_every_label():
    js = {name: (REPO_ROOT / "app" / "static" / "js" / name).read_text() for name in
          ("workflow.js", "receipts.js", "overview.js", "backends.js", "dreams.js")}
    assert "sourceNotice(workflow.source_label, 'workflow')" in js["workflow.js"]
    assert "sourceNotice(workflow.source_label, 'workflow-stage')" in js["workflow.js"]
    assert "sourceNotice(data.source_label, 'receipts')" in js["receipts.js"]
    assert "sourceNotice(data.source_label, 'rollouts')" in js["overview.js"]
    assert "sourceNotice(view.source_label, 'task-backend')" in js["backends.js"]
    assert "sourceNotice(replay.eval_source_label, 'dreams')" in js["dreams.js"]
    assert "sourceNotice(replay.eval_source_label, 'dreams-drawer')" in js["dreams.js"]


@pytest.mark.parametrize("relpath", PROVENANCE_FILES + [str(p.relative_to(REPO_ROOT)) for p in
                                                        sorted((REPO_ROOT / "app" / "static" / "js").glob("*.js"))]
                         + [str(p.relative_to(REPO_ROOT)) for p in sorted((REPO_ROOT / "team-memory" / "dreams").glob("*.md"))])
def test_no_recorded_provenance_claims(relpath):
    lines = (REPO_ROOT / relpath).read_text(encoding="utf-8").splitlines()
    claims = [line.strip() for line in lines if re.search(r"\brecord(ed|ing|ings)\b", line, re.IGNORECASE)
              and "not recorded" not in line.lower()]
    assert claims == [], claims


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _reference_text(relpath: str) -> str:
    path = REPO_ROOT / relpath
    if path.suffix == ".json":
        return "\n".join(_strings(json.loads(path.read_text(encoding="utf-8"))))
    if path.suffix in (".yaml", ".yml"):
        return "\n".join(_strings(yaml.safe_load(path.read_text(encoding="utf-8"))))
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("relpath", REFERENCE_SOURCES)
def test_path_references_resolve(relpath):
    text = _reference_text(relpath)
    tokens = {re.sub(r"^[A-Z_]+/", "", token) for token in PATH_TOKEN.findall(text)}
    missing = sorted(token for token in tokens - HARNESS_SNIPPETS if not (REPO_ROOT / token).exists())
    assert missing == [], f"{relpath} references files that do not exist: {missing}"


def test_path_checker_catches_missing_files():
    tokens = set(PATH_TOKEN.findall("see SPEC.md, docs/NOPE.md and config/*.json"))
    assert tokens == {"SPEC.md", "docs/NOPE.md"} and not (REPO_ROOT / "docs/NOPE.md").exists()


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


def test_spec_acceptance_criteria_map_to_real_tests():
    spec = (REPO_ROOT / "SPEC.md").read_text(encoding="utf-8")
    blocks = re.split(r"^### ", spec, flags=re.MULTILINE)[1:]
    ids = [block.split()[0] for block in blocks]
    assert ids == [f"AC-{n:02d}" for n in range(1, len(ids) + 1)] and len(ids) >= 12
    for block in blocks:
        refs = SPEC_REF.findall(block)
        assert refs, f"{block.split()[0]} lists no tests"
        for file, name in refs:
            path = REPO_ROOT / file
            assert path.is_file() and name in _test_names(path), f"{block.split()[0]}: {file}::{name} not found"
