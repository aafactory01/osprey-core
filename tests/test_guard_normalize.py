"""Engine-level normalization of recursive force deletes (policy versions unchanged, replay unchanged)."""

from __future__ import annotations

import hashlib

import pytest

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


@pytest.mark.parametrize("command", [
    "kubectl get -n prod secrets", "kubectl -n ns get secret x -o yaml", "k get secrets", "sudo k get secret db",
    "kubectl get secret/db-pass", "kubectl describe secrets", "kubectl get -o yaml secrets",
    "kubectl --context prod get pods,secrets -A", "kubectl edit secret app-token",
])
def test_kubectl_secret_reads_are_blocked_in_any_form(v11, command):
    assert evaluate(shell(command), v11).rule_id == "R-KUBECTL-SECRETS"


@pytest.mark.parametrize("command", ["k delete ns prod", "kubectl delete -n x namespace y", "kubectl delete namespace/prod"])
def test_kubectl_namespace_deletes_are_blocked_in_any_form(v11, command):
    assert evaluate(shell(command), v11).rule_id == "R-KUBECTL-DELETE-NS"


@pytest.mark.parametrize("command", [
    "kubectl get pods -n secrets", "kubectl get pods -l app=secret", "k get deployments",
    "kubectl create secret generic x --from-literal=a=b", "kubectl logs secret-rotator-0",
])
def test_routine_kubectl_stays_allowed(v11, command):
    assert evaluate(shell(command), v11).decision == "allow"


@pytest.mark.parametrize("cwd", [CWD, None], ids=["with-cwd", "no-cwd"])
@pytest.mark.parametrize("command", [
    'rm -rf "$(pwd)"', "rm -rf $PWD", 'rm -rf "$PWD"/*', "rm -rf `pwd`", "find / -delete", "find ~ -delete",
    "find . -delete", "find -L / -delete", "find / -exec rm -rf {} +", "find $HOME -type f -delete",
])
def test_working_tree_and_find_deletes_are_blocked(v11, command, cwd):
    decision = evaluate(shell(command, cwd), v11)
    assert decision.blocked and decision.rule_id == "R-RM-RF"


@pytest.mark.parametrize("command", ["find build -delete", "rm -rf build/", "find build -name '*.o' -delete",
                                     "find . -name '*.pyc' -print", "rm -rf $(pwd)/build"])
def test_scoped_find_and_cleanup_stay_allowed(v11, command):
    assert evaluate(shell(command), v11).decision == "allow"


# sha256 of config/dp001_guard_policy.json as committed in 1ba7d2a; the engine fix must not change it.
POLICY_SHA256 = "209c04e341ad07599397b4aa9b2cdafc5fff4d0314c73c041c465aecfb22a93c"


def test_policy_versions_are_byte_identical_to_the_pushed_baseline():
    """Pinned hash instead of git history, so the check also runs in shallow clones."""
    assert hashlib.sha256(DEFAULT_POLICY_PATH.read_bytes()).hexdigest() == POLICY_SHA256
