"""Shell command normalization for the DP-001 guard (engine level, policy versions untouched).

Recursive force deletes are rewritten into one canonical form before policy regexes run:
split or long flags (`-r -f`, `-f -r`, `--recursive --force`) become `-rf`, and targets that resolve
to a protected location (the filesystem root, the home directory, the working tree root, any
ancestor of it, or a root-level glob such as `./*`) become `/`. Routine cleanup inside the working
tree (`build/`, `dist/`, `.pytest_cache`) is left as written. Pure functions: no I/O, no clock.
"""

from __future__ import annotations

import posixpath
import re
import shlex

CLAUSE_SPLIT = re.compile(r"(\s*(?:&&|\|\||;|\||\n)\s*)")
DELETE_COMMAND = "rm"
CANONICAL_FLAGS = "-rf"
CANONICAL_TARGET = "/"
DOT_SEGMENTS = frozenset({".", ".."})


def _env_forms(*names: str) -> frozenset[str]:
    """`$NAME` and `${NAME}` spellings of shell variables."""
    return frozenset(form for name in names for form in (f"${name}", f"${{{name}}}"))


HOME_FORMS = frozenset({"~"}) | _env_forms("HOME")
TREE_ROOT_FORMS = _env_forms("PWD", "REPO_ROOT")


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


def _normalize_clause(clause: str, cwd: object) -> str:
    try:
        tokens = shlex.split(clause, posix=True)
    except ValueError:
        return clause
    names = [posixpath.basename(token) for token in tokens]
    if DELETE_COMMAND not in names:
        return clause
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
        return clause
    protected = any(is_protected_target(target, cwd) for target in targets)
    rest = [CANONICAL_TARGET] if protected else targets
    extra = ["--no-preserve-root"] if no_preserve else []
    return " ".join([*tokens[:start], DELETE_COMMAND, CANONICAL_FLAGS, *rest, *extra])


def normalize_shell(text: str, cwd: object = None) -> str:
    """Canonicalize every recursive force delete in a (possibly compound) shell command."""
    parts = CLAUSE_SPLIT.split(text)
    return "".join(part if CLAUSE_SPLIT.fullmatch(part) else _normalize_clause(part, cwd) for part in parts)
