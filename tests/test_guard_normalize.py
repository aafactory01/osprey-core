"""Engine-level normalization of recursive force deletes (policy versions unchanged, replay unchanged)."""

from __future__ import annotations

import hashlib
import subprocess
import shutil

import pytest

from app.config_store import REPO_ROOT
from dreamstore.hooks.dp001_normalize import is_protected_target, normalize_shell
from dreamstore.hooks.dp001_pre_tool_guard import DEFAULT_POLICY_PATH, evaluate, load_policy_document, resolve_policy

CWD = "/workspace/osprey-core"


@pytest.fixture(scope="module")
def v11():
    return resolve_policy(load_policy_document(DEFAULT_POLICY_PATH), "1.1.0")


def shell(command, cwd=CWD):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "stage": "S2_CODE", "cwd": cwd}


@pytest.mark.parametrize("cwd", [CWD, None], ids=["with-cwd", "no-cwd"])
@pytest.mark.parametrize("command", [
    "rm -r -f /", "rm -f -r /", "rm -rf ../..", "rm -rf ./*", "rm --recursive --force ~", "rm -R -f $HOME",
    "rm -rf -- /", "cd x && rm -r -f ../.. ; echo done", "sudo rm -fr --no-preserve-root /*",
])
def test_delete_evasions_are_blocked(v11, command, cwd):
    decision = evaluate(shell(command, cwd), v11)
    assert decision.blocked and decision.rule_id == "R-RM-RF"


@pytest.mark.parametrize("cwd", [CWD, None], ids=["with-cwd", "no-cwd"])
@pytest.mark.parametrize("command", [
    "rm -rf build/ dist/ .pytest_cache node_modules", "rm -r -f build", "rm --recursive --force dist",
    "rm -rf ./dist/*", "rm -f notes.txt", "rm -rf ./build ./coverage",
])
def test_routine_cleanup_stays_allowed(v11, command, cwd):
    assert evaluate(shell(command, cwd), v11).decision == "allow"


@pytest.mark.parametrize("target,blocked", [
    ("/workspace", True), ("/workspace/osprey-core", True), ("/workspace/osprey-core/", True),
    ("/workspace/osprey-core/build", False), ("/tmp/scratch", False), ("../osprey-core", True), ("../other", False),
])
def test_ancestors_of_the_working_tree_are_protected(v11, target, blocked):
    assert evaluate(shell(f"rm -rf {target}"), v11).blocked is blocked


@pytest.mark.parametrize("target,cwd,expected", [
    ("/", None, True), ("/*", CWD, True), ("~/", None, True), ("${HOME}", None, True), ("~/projects", None, False),
    ("$REPO_ROOT", None, True), (".", None, True), ("..", None, True), ("*", CWD, True), ("dist", None, False),
])
def test_is_protected_target(target, cwd, expected):
    assert is_protected_target(target, cwd) is expected


def test_normalization_preserves_other_clauses():
    assert normalize_shell("make clean && rm -r -f ../.. || echo x", CWD) == "make clean && rm -rf / || echo x"
    assert normalize_shell("echo 'unbalanced", CWD) == "echo 'unbalanced"
    assert normalize_shell("git status; rm -f a.txt", CWD) == "git status; rm -f a.txt"


def test_protected_paths_rule_still_sees_delete_targets(v11):
    assert evaluate(shell("rm -r -f .workflow/"), v11).rule_id == "R-SHELL-PROTECTED-PATH-WRITE"


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_policy_versions_are_byte_identical_to_the_pushed_baseline():
    """The engine fix must not rewrite the immutable policy versions committed in 1ba7d2a."""
    proc = subprocess.run(["git", "cat-file", "-e", "1ba7d2a0bff498e566efd9cdc4faab339fe95d93"], cwd=REPO_ROOT,
                          capture_output=True, check=False)
    if proc.returncode != 0:
        pytest.skip("baseline commit not available in this clone")
    old = subprocess.run(["git", "show", "1ba7d2a0bff498e566efd9cdc4faab339fe95d93:config/dp001_guard_policy.json"],
                         cwd=REPO_ROOT, capture_output=True, check=True).stdout
    assert hashlib.sha256(old).hexdigest() == hashlib.sha256(DEFAULT_POLICY_PATH.read_bytes()).hexdigest()
