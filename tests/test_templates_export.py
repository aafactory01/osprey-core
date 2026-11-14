"""Exported agent templates: 13 verbatim Hub templates with sha256 parity, a matching MANIFEST, no secrets.

templates/ is written by the lane exporter (GET-only, kept outside this repository) straight from the
Scion Hub. These checks run offline: they re-hash every file, cross-check template.json against
MANIFEST.json, pin the QA council harness and model, scan for secret patterns, and prove with fault
injection on a temporary copy that the verifier catches tampering.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from app.config_store import REPO_ROOT

TEMPLATES = REPO_ROOT / "templates"
PROJECT_ID = "862ead69-1199-47d0-ba6c-16084cef482b"
CORE = ("core-builder", "core-coordinator", "core-deployer", "core-docs-writer", "core-planner", "core-reviewer",
        "core-security-auditor", "core-spec-writer", "core-sub-coordinator", "core-tester")
COUNCIL = {"qa-fable": ("claude", "claude-fable-5-1@default"), "qa-gemini": ("antigravity", "Gemini 4 Argon (High)"),
           "qa-opus": ("claude", "claude-opus-5-5@default")}
NAMES = tuple(sorted((*CORE, *COUNCIL)))
CONTENT_FILES = ["agents.md", "scion-agent.yaml", "system-prompt.md"]
META_KEYS = {"id", "name", "slug", "displayName", "description", "harness", "defaultHarnessConfig", "agentConfig",
             "scope", "contentHash", "status", "created", "updated", "files", "source"}
DROPPED_KEYS = {"updatedBy", "_capabilities", "storageUri", "storageBucket", "storagePath"}
MANIFEST_SOURCE = {"hub": "osprey-hub", "project_id": PROJECT_ID, "api": "/api/v1/templates"}
BLOB_SOURCE = {"kind": "hub-blob-store", "path": "templates/global/qa.blobs"}
# Documented in templates/README.md ("Metadata versus file differences"): yaml harness/model != metadata.
YAML_DIFFERS = {"core-security-auditor", "core-tester", "qa-fable", "qa-gemini"}
EARLIER_STAGES = ("S2_CODE", "S3_TEST", "S4_REVIEW")
CANONICAL_ONLY = ("S2_TESTS", "S3_BUILD", "S4_VERIFY_QA")
HEX64 = re.compile(r"[0-9a-f]{64}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Built from fragments so this file never contains a literal key header, token prefix, address or host.
SECRET_PATTERNS = {
    "private_key_header": re.compile("-----BEGIN [A-Z0-9 ]*" + "PRIVATE " + "KEY"),
    "google_access_token": re.compile(re.escape("ya" + "29.")),
    "google_api_key": re.compile("AI" + "za"),
    "github_token": re.compile("gh" + "[pousr]_|github" + "_pat_"),
    "oauth_client_id": re.compile(re.escape("apps." + "googleusercontent" + ".com")),
    "oauth_client_secret": re.compile("GOC" + "SPX-"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+" + "@" + r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "cloud_run_host": re.compile(re.escape("." + "run" + "." + "app"), re.IGNORECASE),
}
HEX_OR_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                         r"|(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")
# GCP project numbers are 12 digits: no run of 10+ digits may appear outside hashes and UUIDs.
LONG_NUMBER = re.compile(r"[0-9]{10,}")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical(doc: Any) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def meta_of(name: str, root: Path = TEMPLATES) -> dict[str, Any]:
    return load(root / name / "template.json")


def spec_of(name: str) -> dict[str, Any]:
    return yaml.safe_load((TEMPLATES / name / "scion-agent.yaml").read_text(encoding="utf-8"))


def manifest_row(meta: dict[str, Any]) -> dict[str, Any]:
    return {"name": meta["name"], "id": meta["id"], "scope": meta["scope"], "harness": meta["harness"],
            "model": meta["agentConfig"]["model"], "contentHash": meta["contentHash"],
            "files": [{"path": f["path"], "sha256": f["sha256"], "size": f["size"]} for f in meta["files"]]}


def problems(root: Path) -> list[str]:
    """Every integrity problem in an exported tree; an empty list means verified."""
    found: list[str] = []
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir())
    if dirs != list(NAMES):
        found.append(f"template dirs {dirs} != the expected 13")
    try:
        manifest = load(root / "MANIFEST.json")
    except (OSError, ValueError) as exc:
        return [*found, f"MANIFEST.json unreadable: {exc}"]
    rows = {row.get("name"): row for row in manifest.get("templates", [])}
    if manifest.get("count") != len(NAMES) or len(manifest.get("templates", [])) != len(NAMES) \
            or sorted(rows) != list(NAMES):
        found.append("MANIFEST names/count differ from the 13 templates")
    for name in (n for n in NAMES if n in dirs):
        tdir = root / name
        try:
            meta = meta_of(name, root)
            listed = [entry["path"] for entry in meta["files"]]
            row = manifest_row(meta)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            found.append(f"{name}: template.json unreadable or incomplete: {exc!r}")
            continue
        on_disk = sorted(p.relative_to(tdir).as_posix() for p in tdir.rglob("*") if p.is_file())
        if on_disk != sorted([*listed, "template.json"]):
            found.append(f"{name}: files on disk {on_disk} != listed {listed}")
        for entry in meta["files"]:
            path = tdir / entry["path"]
            data = path.read_bytes() if path.is_file() else None
            if data is None or hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["size"]:
                found.append(f"{name}/{entry['path']}: sha256/size mismatch")
        if rows.get(name) != row:
            found.append(f"{name}: MANIFEST row differs from template.json")
    return found


def scan_text(text: str) -> list[str]:
    """Secret-pattern kinds found in `text` (matched values are never returned or printed)."""
    kinds = [kind for kind, rx in SECRET_PATTERNS.items() if rx.search(text)]
    if LONG_NUMBER.search(HEX_OR_UUID.sub(" ", text)):
        kinds.append("long_number")
    # Optional exact values (e.g. the real project number) supplied by CI; never committed here.
    exact = [value.strip() for value in os.environ.get("OSPREY_FORBIDDEN_NUMBERS", "").split(",") if value.strip()]
    if any(value in text for value in exact):
        kinds.append("forbidden_value")
    return kinds


def test_exactly_13_template_dirs_plus_manifest_and_readme():
    assert len(NAMES) == 13 and len(set(NAMES)) == 13
    assert sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir()) == list(NAMES)
    assert sorted(p.name for p in TEMPLATES.iterdir() if p.is_file()) == ["MANIFEST.json", "README.md"]


def test_exported_tree_verifies():
    assert problems(TEMPLATES) == []


@pytest.mark.parametrize("name", NAMES)
def test_template_metadata_files_and_hashes(name):
    meta = meta_of(name)
    assert META_KEYS <= set(meta) <= META_KEYS | {"scopeId"} and not DROPPED_KEYS & set(meta)
    assert meta["name"] == meta["slug"] == name and UUID_RE.fullmatch(meta["id"]) and meta["status"] == "active"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", meta["contentHash"])
    assert meta["harness"] == meta["defaultHarnessConfig"] == meta["agentConfig"]["default_harness_config"]
    assert [entry["path"] for entry in meta["files"]] == CONTENT_FILES
    for entry in meta["files"]:
        assert set(entry) == {"path", "size", "sha256", "mode"}
        assert entry["mode"] is None or re.fullmatch(r"0[0-7]{3}", entry["mode"])
        data = (TEMPLATES / name / entry["path"]).read_bytes()
        assert HEX64.fullmatch(entry["sha256"]) and hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["size"] > 0
    for stamp in (meta["created"], meta["updated"]):
        assert datetime.fromisoformat(stamp.replace("Z", "+00:00")).utcoffset() == timedelta(0)


def test_manifest_matches_dirs_ids_hashes_and_count():
    manifest = load(TEMPLATES / "MANIFEST.json")
    assert set(manifest) == {"schema", "exported_at", "source", "count", "templates"}
    assert manifest["schema"] == "osprey-core.templates-manifest/v1" and manifest["source"] == MANIFEST_SOURCE
    assert manifest["count"] == 13 == len(manifest["templates"])
    assert [row["name"] for row in manifest["templates"]] == list(NAMES)
    assert datetime.fromisoformat(manifest["exported_at"].replace("Z", "+00:00")).utcoffset() == timedelta(0)
    assert manifest["templates"] == [manifest_row(meta_of(name)) for name in NAMES]
    assert len({row["id"] for row in manifest["templates"]}) == 13


@pytest.mark.parametrize("name", sorted(COUNCIL))
def test_qa_council_harness_model_and_blob_source(name):
    meta = meta_of(name)
    row = next(row for row in load(TEMPLATES / "MANIFEST.json")["templates"] if row["name"] == name)
    assert (meta["harness"], meta["agentConfig"]["model"]) == COUNCIL[name] == (row["harness"], row["model"])
    assert meta["scope"] == row["scope"] == "global" and "scopeId" not in meta and meta["source"] == BLOB_SOURCE
    if meta["harness"] == "claude":
        assert meta["agentConfig"]["env"]["ANTHROPIC_MODEL"] == meta["agentConfig"]["model"]


def test_qa_council_members_share_one_verbatim_file_set():
    file_sets = {tuple((f["path"], f["sha256"], f["size"]) for f in meta_of(name)["files"]) for name in COUNCIL}
    assert len(file_sets) == 1
    assert len({meta_of(name)["id"] for name in COUNCIL}) == len({meta_of(name)["contentHash"] for name in COUNCIL}) == 3


@pytest.mark.parametrize("name", CORE)
def test_core_templates_are_project_scoped_hub_api_exports(name):
    meta = meta_of(name)
    assert meta["scope"] == "project" and meta["scopeId"] == PROJECT_ID
    assert meta["source"] == {"kind": "hub-api", "path": f"/api/v1/templates/{meta['id']}"}
    assert all(entry["mode"] is None for entry in meta["files"])


@pytest.mark.parametrize("name", NAMES)
def test_scion_agent_yaml_parses_and_references_listed_files(name):
    spec, listed = spec_of(name), {entry["path"] for entry in meta_of(name)["files"]}
    assert spec["schema_version"] == "1"
    assert spec["agent_instructions"] in listed and spec["system_prompt"] in listed


def test_yaml_versus_metadata_differences_are_exactly_the_documented_set():
    differs = {name for name in NAMES if (spec_of(name)["default_harness_config"], spec_of(name)["model"])
               != (meta_of(name)["harness"], meta_of(name)["agentConfig"]["model"])}
    assert differs == YAML_DIFFERS
    readme = (TEMPLATES / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Metadata versus file differences", 1)[1].split("\n## ", 1)[0]
    assert all(f"`{name}`" in section for name in YAML_DIFFERS)


@pytest.mark.parametrize("name", CORE)
def test_core_texts_use_the_earlier_stage_names_documented_in_readme(name):
    text = (TEMPLATES / name / "agents.md").read_text(encoding="utf-8")
    assert all(stage in text for stage in EARLIER_STAGES) and not any(stage in text for stage in CANONICAL_ONLY)


def test_no_secret_patterns_or_project_numbers():
    files = sorted(p for p in TEMPLATES.rglob("*") if p.is_file())
    assert len(files) == 13 * 4 + 2
    hits = [f"{p.relative_to(REPO_ROOT)}: {kind}" for p in files for kind in scan_text(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_secret_scanner_flags_each_pattern_and_spares_hashes_and_ids():
    samples = {
        "private_key_header": "-----BEGIN " + "RSA PRIVATE " + "KEY-----",
        "google_access_token": "Bearer " + "ya" + "29." + "a0Abc",
        "google_api_key": "key=" + "AI" + "za" + "Sy" + "x" * 33,
        "github_token": "gh" + "p_" + "x" * 36,
        "oauth_client_id": "1234-abc." + "apps." + "googleusercontent" + ".com",
        "oauth_client_secret": "GOC" + "SPX-" + "x" * 28,
        "email": "someone" + "@" + "example" + ".org",
        "cloud_run_host": "svc-abc" + ".a.run" + ".app",
        "long_number": "projects/" + "4" * 12 + "/x",
    }
    for kind, text in samples.items():
        assert kind in scan_text(text), kind
    assert scan_text(f"sha256:{'1' * 64} id {PROJECT_ID} model claude-opus-5-5@default size 11402") == []


def test_json_is_canonical_and_sorted():
    for path in [TEMPLATES / "MANIFEST.json", *(TEMPLATES / name / "template.json" for name in NAMES)]:
        text = path.read_text(encoding="utf-8")
        assert text == canonical(json.loads(text)), path.relative_to(REPO_ROOT)
    for name in NAMES:
        paths = [entry["path"] for entry in meta_of(name)["files"]]
        assert paths == sorted(paths)


def test_readme_documents_templates_council_verification_and_stage_map():
    text = (TEMPLATES / "README.md").read_text(encoding="utf-8")
    assert all(f"`{name}`" in text for name in NAMES) and all(f"`{model}`" in text for _, model in COUNCIL.values())
    assert "pytest tests/test_templates_export.py" in text and "sha256" in text and "verbatim" in text
    for line in ("S2_CODE -> S3_BUILD", "S3_TEST -> S2_TESTS + S3_BUILD", "S4_REVIEW -> S4_VERIFY_QA"):
        assert line in text


def _flip_byte(root: Path) -> None:
    path = root / "core-builder" / "agents.md"
    data = bytearray(path.read_bytes())
    data[0] ^= 1
    path.write_bytes(bytes(data))


def _append_byte(root: Path) -> None:
    with (root / "qa-opus" / "system-prompt.md").open("ab") as handle:
        handle.write(b"\n")


def _edit_manifest(mutate: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        doc = load(root / "MANIFEST.json")
        mutate(doc)
        (root / "MANIFEST.json").write_text(canonical(doc), encoding="utf-8")
    return apply


def _set_planner_id(doc: dict[str, Any]) -> None:
    next(row for row in doc["templates"] if row["name"] == "core-planner")["id"] = PROJECT_ID


FAULTS: dict[str, tuple[Callable[[Path], None], str]] = {
    "flipped_byte": (_flip_byte, "core-builder/agents.md: sha256/size mismatch"),
    "appended_byte": (_append_byte, "qa-opus/system-prompt.md: sha256/size mismatch"),
    "deleted_file": (lambda root: (root / "core-tester" / "scion-agent.yaml").unlink(), "core-tester: files on disk"),
    "extra_file": (lambda root: (root / "qa-gemini" / "notes.md").write_text("x\n", encoding="utf-8"),
                   "qa-gemini: files on disk"),
    "missing_template": (lambda root: shutil.rmtree(root / "core-deployer"), "template dirs"),
    "manifest_count": (_edit_manifest(lambda doc: doc.update(count=12)), "MANIFEST names/count"),
    "manifest_id": (_edit_manifest(_set_planner_id), "core-planner: MANIFEST row differs"),
    "broken_metadata": (lambda root: (root / "qa-fable" / "template.json").write_text("{", encoding="utf-8"),
                        "qa-fable: template.json unreadable"),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_verifier_catches_injected_faults(tmp_path, fault):
    root = tmp_path / "templates"
    shutil.copytree(TEMPLATES, root)
    assert problems(root) == []
    mutate, expected = FAULTS[fault]
    mutate(root)
    found = problems(root)
    assert any(expected in line for line in found), found
