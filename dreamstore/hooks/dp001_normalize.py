"""Shell command normalization for the DP-001 guard (engine level, policy versions untouched).

Risky commands are rewritten into one canonical form before policy regexes run:
- Recursive force deletes: split or long flags (`-r -f`, `--recursive --force`) become `-rf`, and
  targets that resolve to a protected location (the filesystem root, the home directory, the
  working tree root or any ancestor of it, `$PWD`/`$(pwd)`, or a root-level glob such as `./*`)
  become `/`. `find <protected> -delete` (or `-exec rm`) is treated as the same delete.
- kubectl: the `k` alias becomes `kubectl`; reading secrets (`get`/`describe`/`edit` with a
  `secret`, `secrets`, or `secret/<name>` resource, flags in any position) becomes
  `kubectl get secrets`; deleting a namespace becomes `kubectl delete namespace`.
Routine cleanup inside the working tree (`rm -rf build/`, `find build -delete`) is left as written.
Pure functions: no I/O, no clock.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import Callable

CLAUSE_SPLIT = re.compile(r"(\s*(?:&&|\|\||;|\||\n)\s*)")
SECRET_RESOURCE = re.compile(r"^secrets?(?:/\S*)?$")
NAMESPACE_RESOURCE = re.compile(r"^(?:ns|namespaces?)(?:/\S*)?$")
DELETE_COMMAND = "rm"
FIND_COMMAND = "find"
KUBECTL = "kubectl"
CANONICAL_FLAGS = "-rf"
CANONICAL_TARGET = "/"
CANONICAL_DELETE = f"{DELETE_COMMAND} {CANONICAL_FLAGS} {CANONICAL_TARGET}"
DOT_SEGMENTS = frozenset({".", ".."})
FIND_LEADING_OPTIONS = frozenset({"-H", "-L", "-P"})
KUBECTL_NAMES = frozenset({KUBECTL, "k"})
SECRET_READ_VERBS = frozenset({"get", "describe", "edit"})
NAMESPACE_FLAGS = frozenset({"-n", "--namespace"})


def _env_forms(*names: str) -> frozenset[str]:
    """`$NAME` and `${NAME}` spellings of shell variables."""
    return frozenset(form for name in names for form in (f"${name}", f"${{{name}}}"))


HOME_FORMS = frozenset({"~"}) | _env_forms("HOME")
TREE_ROOT_FORMS = _env_forms("PWD", "REPO_ROOT") | frozenset({"$(pwd)", "`pwd`"})


def _base(target: str) -> str:
    """Drop a trailing glob or slash so `./*`, `~/*`, `/*`, and `dist/` resolve to their directory."""
    base = target.removesuffix("/*")
    if not base and target:
        return CANONICAL_TARGET
    if base in ("*", ".*"):
        return "."
    stripped = base.rstrip("/")
    return stripped if stripped else (CANONICAL_TARGET if base.startswith("/") else base)


def _covers(path: str, cwd: str) -> bool:
    """True when `path` is the root, the working tree root, or one of its ancestors."""
    norm = posixpath.normpath(path)
    return norm == CANONICAL_TARGET or cwd == norm or cwd.startswith(norm.rstrip("/") + "/")


def is_protected_target(target: str, cwd: object) -> bool:
    """Decide whether deleting `target` (as typed) would destroy a protected location."""
    base = _base(target)
    home = base.split("/")[0]
    if home in HOME_FORMS and not base[len(home):].strip("/"):
        return True
    if base in TREE_ROOT_FORMS:
        return True
    known_cwd = posixpath.normpath(cwd) if isinstance(cwd, str) and cwd.startswith("/") else None
    if base.startswith("/"):
        return _covers(base, known_cwd) if known_cwd else posixpath.normpath(base) == CANONICAL_TARGET
    if known_cwd:
        return _covers(posixpath.join(known_cwd, base), known_cwd)
    return all(part in DOT_SEGMENTS for part in posixpath.normpath(base).split("/"))


def _find_clause(tokens: list[str], names: list[str], cwd: object) -> str | None:
    """`find <protected root> ... -delete` (or `-exec rm`) is a recursive delete of that root."""
    if FIND_COMMAND not in names:
        return None
    args = tokens[names.index(FIND_COMMAND) + 1:]
    while args and args[0] in FIND_LEADING_OPTIONS:
        args = args[1:]
    roots: list[str] = []
    for token in args:
        if token.startswith(("-", "(", "!")):
            break
        roots.append(token)
    deletes = "-delete" in args or any(posixpath.basename(token) == DELETE_COMMAND for token in args)
    if deletes and any(is_protected_target(root, cwd) for root in roots or ["."]):
        return CANONICAL_DELETE
    return None


def _kubectl_clause(tokens: list[str], names: list[str], cwd: object) -> str | None:
    """Canonical kubectl form, independent of the `k` alias and of flag positions."""
    hits = [index for index, name in enumerate(names)
            if name == KUBECTL or (name in KUBECTL_NAMES and (index == 0 or names[index - 1] == "sudo"))]
    if not hits:
        return None
    args = tokens[hits[0] + 1:]
    words: list[str] = []
    skip_value = False
    for token in args:
        if skip_value:
            skip_value = False
        elif token in NAMESPACE_FLAGS:
            skip_value = True
        elif not token.startswith("-"):
            words.append(token)
    parts = [part for word in words for part in word.split(",") if part]
    if any(word in SECRET_READ_VERBS for word in words) and any(SECRET_RESOURCE.match(part) for part in parts):
        return f"{KUBECTL} get secrets"
    if "delete" in words and any(NAMESPACE_RESOURCE.match(part) for part in parts):
        return f"{KUBECTL} delete namespace"
    return " ".join([*tokens[:hits[0]], KUBECTL, *args])


def _delete_clause(tokens: list[str], names: list[str], cwd: object) -> str | None:
    if DELETE_COMMAND not in names:
        return None
    start = names.index(DELETE_COMMAND)
    recursive = force = no_preserve = options_done = False
    targets: list[str] = []
    for token in tokens[start + 1:]:
        if options_done or not token.startswith("-") or token == "-":
            targets.append(token)
        elif token == "--":
            options_done = True
        elif token.startswith("--"):
            recursive |= token == "--recursive"
            force |= token == "--force"
            no_preserve |= token == "--no-preserve-root"
        else:
            letters = set(token.lstrip("-"))
            recursive |= bool(letters & set("rR"))
            force |= "f" in letters
    if not (recursive and force):
        return None
    protected = any(is_protected_target(target, cwd) for target in targets)
    rest = [CANONICAL_TARGET] if protected else targets
    extra = ["--no-preserve-root"] if no_preserve else []
    return " ".join([*tokens[:start], DELETE_COMMAND, CANONICAL_FLAGS, *rest, *extra])


HANDLERS: tuple[Callable[[list[str], list[str], object], str | None], ...] = (_find_clause, _kubectl_clause, _delete_clause)


def _normalize_clause(clause: str, cwd: object) -> str:
    try:
        tokens = shlex.split(clause, posix=True)
    except ValueError:
        return clause
    names = [posixpath.basename(token) for token in tokens]
    for handler in HANDLERS:
        result = handler(tokens, names, cwd)
        if result is not None:
            return result
    return clause


def normalize_shell(text: str, cwd: object = None) -> str:
    """Canonicalize risky invocations in a (possibly compound) shell command."""
    parts = CLAUSE_SPLIT.split(text)
    return "".join(part if CLAUSE_SPLIT.fullmatch(part) else _normalize_clause(part, cwd) for part in parts)
