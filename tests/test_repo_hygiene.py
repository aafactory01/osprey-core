"""Repository hygiene: LOC cap, scrub, git tracking, container contract, UI contract, and doc/config parity."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.config_store import REPO_ROOT

MAX_LINES = 400
SOURCE_SUFFIXES = {".py", ".js", ".css", ".html", ".json", ".md", ".yaml", ".yml", ".txt", ".ini", ".svg"}
# Built without the literal words so this file passes its own scan.
BANNED = "|".join(["l" + "eo", "c" + "me", "s" + "pan", "f" + "cm"])
UI_FILES = sorted([*(REPO_ROOT / "app" / "static").rglob("*.js"), *(REPO_ROOT / "app" / "static").rglob("*.css"),
                   *(REPO_ROOT / "app" / "templates").rglob("*.html")])
GIT = shutil.which("git")


def repo_files() -> list[Path]:
    """Tracked plus untracked-but-not-ignored files (what the next commit can contain)."""
    if GIT and (REPO_ROOT / ".git").exists():
        out = subprocess.run([GIT, "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=REPO_ROOT,
                             capture_output=True, check=True).stdout.decode("utf-8")
        return [REPO_ROOT / name for name in out.split("\0") if name and (REPO_ROOT / name).is_file()]
    return [p for p in REPO_ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]


def test_every_source_file_is_at_most_400_lines():
    too_long = []
    for path in repo_files():
        if path.suffix in SOURCE_SUFFIXES or path.name == "Dockerfile":
            count = len(path.read_text(encoding="utf-8").splitlines())
            if count > MAX_LINES:
                too_long.append(f"{path.relative_to(REPO_ROOT)}: {count}")
    assert too_long == []


def test_scrub_banned_terms_case_insensitive_substring():
    hits = []
    pattern = re.compile(BANNED, re.IGNORECASE)
    for path in repo_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        hits += [f"{path.relative_to(REPO_ROOT)}:{n}" for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
        if pattern.search(path.relative_to(REPO_ROOT).as_posix()):
            hits.append(f"path {path.relative_to(REPO_ROOT)}")
    assert hits == []


@pytest.mark.skipif(not GIT, reason="git not installed")
def test_git_grep_scrub_is_empty():
    proc = subprocess.run([GIT, "grep", "--untracked", "-n", "-I", "-i", "-E", BANNED], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 1 and proc.stdout == "", proc.stdout + proc.stderr


def test_no_virtualenv_or_bytecode_in_repo():
    bad = [p for p in repo_files() if re.search(r"(^|/)(venv|\.venv)/|__pycache__|\.py[cod]$", p.relative_to(REPO_ROOT).as_posix())]
    assert bad == []


def test_gitignore_covers_local_artifacts():
    entries = set((REPO_ROOT / ".gitignore").read_text().split())
    assert {"venv/", ".venv/", "__pycache__/", "*.py[cod]", ".pytest_cache/", ".coverage", "htmlcov/", ".env", "*.log",
            "node_modules/"} <= entries


def test_dockerignore_and_gcloudignore_keep_context_small():
    docker = set((REPO_ROOT / ".dockerignore").read_text().split())
    assert {".git", "venv/", ".venv/", "**/__pycache__/", "**/*.py[cod]", ".pytest_cache/", "evidence/"} <= docker
    gcloud = (REPO_ROOT / ".gcloudignore").read_text()
    assert ".git" in gcloud.split() and "#!include:.dockerignore" in gcloud


def test_dockerfile_contract():
    text = (REPO_ROOT / "Dockerfile").read_text()
    assert text.splitlines()[1].strip() == "FROM python:3.12-slim"
    for needle in ("PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "pip install --no-cache-dir -r requirements.txt",
                   '--bind "0.0.0.0:${PORT:-8080}"', "GUNICORN_WORKERS=", "GUNICORN_THREADS=", "GUNICORN_TIMEOUT=",
                   '"${GUNICORN_WORKERS:-1}"', "app.app:app", "HEALTHCHECK"):
        assert needle in text, needle
    user = re.findall(r"^USER\s+(\S+)", text, re.MULTILINE)
    assert user and user[-1].split(":")[0] not in ("root", "0")


def test_requirements_are_pinned_and_split():
    runtime = [line for line in (REPO_ROOT / "requirements.txt").read_text().splitlines() if line and not line.startswith("#")]
    assert runtime and all(re.match(r"^[A-Za-z0-9_.-]+==\d", line) for line in runtime)
    assert any(line.startswith("Flask==3.") for line in runtime) and any(line.startswith("gunicorn==") for line in runtime)
    assert not any(line.lower().startswith("pytest") for line in runtime)
    dev = (REPO_ROOT / "requirements-dev.txt").read_text()
    assert "-r requirements.txt" in dev and "pytest==" in dev


@pytest.mark.parametrize("path", UI_FILES, ids=lambda p: p.name)
def test_ui_files_are_ascii_without_external_resources(path):
    text = path.read_text(encoding="utf-8")
    assert text.isascii(), "UI files must not contain emoji or unicode icon glyphs"
    assert 'target="_blank"' not in text and "_blank" not in text
    assert not re.search(r"(src|href)\s*=\s*[\"']?(https?:)?//", text)
    assert "Math.random" not in text


def test_index_html_accessibility_contract():
    html = (REPO_ROOT / "app" / "templates" / "index.html").read_text()
    assert re.search(r'<button id="theme-toggle"[^>]*aria-pressed="false"', html)
    tabs = re.findall(r'role="tab"[^>]*id="tab-([a-z-]+)"[^>]*aria-controls="panel-([a-z-]+)"', html)
    assert [t for t, _ in tabs] == ["overview", "workflow", "task-backend", "dreams", "receipts"]
    assert all(tab == panel for tab, panel in tabs) and html.count('role="tabpanel"') == 5
    assert html.count('aria-selected="true"') == 1
    dialogs = re.findall(r"<dialog[^>]*>", html)
    assert len(dialogs) == 2 and all('aria-modal="true"' in d and "aria-labelledby" in d for d in dialogs)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline scripts violate the CSP"
    assert " style=" not in html


def test_css_defines_both_themes_and_hides_nothing_essential():
    css = (REPO_ROOT / "app" / "static" / "css" / "style.css").read_text().lower()
    assert "--bg: #f8fafc" in css and '[data-theme="dark"]' in css and "--bg: #0c0e14" in css


def test_workflow_yaml_matches_config():
    yml = yaml.safe_load((REPO_ROOT / ".workflow" / "workflow.yaml").read_text())
    cfg = json.loads((REPO_ROOT / "config" / "workflow_stages.json").read_text())
    assert [s["id"] for s in yml["stages"]] == [s["id"] for s in cfg["stages"]]
    assert [s["mode"] for s in yml["stages"]] == [s["mode"] for s in cfg["stages"]]
    assert [s["owners"] for s in yml["stages"]] == [s["owners"] for s in cfg["stages"]]


def test_policy_stage_order_matches_workflow():
    policy = json.loads((REPO_ROOT / "config" / "dp001_guard_policy.json").read_text())
    cfg = json.loads((REPO_ROOT / "config" / "workflow_stages.json").read_text())
    assert policy["stage_order"] == [s["id"] for s in cfg["stages"]]


def test_docs_cover_roster_and_stages():
    agents = json.loads((REPO_ROOT / "config" / "agents.json").read_text())["agents"]
    roster = (REPO_ROOT / "AGENTS.md").read_text()
    assert len(agents) == 25 and all(f"`{agent['id']}`" in roster for agent in agents)
    workflow = (REPO_ROOT / "WORKFLOW.md").read_text()
    assert all(stage in workflow for stage in ("S1_SPEC", "S2_CODE", "S3_TEST", "S4_REVIEW", "S5_RELEASE_GATE", "S6_DEPLOY_CLOSE"))
    lessons = (REPO_ROOT / "team-memory" / "LESSONS_LEARNED.md").read_text()
    assert len([line for line in lessons.splitlines() if line.startswith("- ")]) >= 8


def test_guard_is_real_and_exports_evaluate():
    from dreamstore.hooks import dp001_pre_tool_guard as guard

    assert callable(guard.evaluate) and callable(guard.main)
    assert "def guard():" not in (REPO_ROOT / "dreamstore" / "hooks" / "dp001_pre_tool_guard.py").read_text()
