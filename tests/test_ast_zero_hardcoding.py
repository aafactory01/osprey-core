"""AST gate: zero inline hardcoding, zero randomness, and zero silent failures in production code.

Scans app/, dreamstore/, scripts/, and .workflow/ Python plus app/static/js:
  (a) literal dict/list/set/tuple trees holding more than 3 constant data values (dict keys are structural);
  (b) numeric literals other than 0, 1, -1 (tunables live in config/*.json);
  (c) `import random`, `random.*`, and `Math.random(`;
  (d) silent handlers (`except ...: pass`).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.config_store import REPO_ROOT

PROD_DIRS = ("app", "dreamstore", "scripts", ".workflow")
MAX_DATA_ELEMENTS = 3
ALLOWED_NUMBERS = {0, 1}
# Protocol constant, not a tunable: PreToolUse harnesses treat exit 2 as "block" (see HookExit docstring).
PROTOCOL_CONSTANTS = {("dreamstore/hooks/dp001_pre_tool_guard.py", "HookExit", 2)}
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def prod_python_files() -> list[Path]:
    files = [path for name in PROD_DIRS for path in sorted((REPO_ROOT / name).rglob("*.py"))]
    return [path for path in files if "__pycache__" not in path.parts]


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _data_count(node: ast.AST) -> int:
    if isinstance(node, ast.Constant):
        return 0 if node.value is None else 1
    if isinstance(node, ast.Dict):
        return sum(_data_count(value) for value in node.values if value is not None)
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return sum(_data_count(element) for element in node.elts)
    return 0


def _outer_literals(tree: ast.AST) -> list[ast.AST]:
    containers = (ast.Dict, ast.List, ast.Set, ast.Tuple)
    nested = {id(child) for node in ast.walk(tree) if isinstance(node, containers)
              for child in ast.iter_child_nodes(node) if isinstance(child, containers)}
    return [node for node in ast.walk(tree) if isinstance(node, containers) and id(node) not in nested]


def _class_of(tree: ast.AST) -> dict[int, str]:
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in ast.walk(node):
                owner[id(child)] = node.name
    return owner


def test_production_tree_exists():
    assert len(prod_python_files()) >= 14


def test_no_inline_data_literals():
    offenders = []
    for path in prod_python_files():
        for node in _outer_literals(ast.parse(path.read_text(encoding="utf-8"))):
            count = _data_count(node)
            if count > MAX_DATA_ELEMENTS:
                offenders.append(f"{rel(path)}:{node.lineno} literal with {count} data values")
    assert offenders == [], "move data into config/*.json:\n" + "\n".join(offenders)


def test_no_magic_numbers():
    offenders = []
    for path in prod_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = _class_of(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
                continue
            if isinstance(node.value, (int, float, complex)) and node.value not in ALLOWED_NUMBERS:
                if (rel(path), owners.get(id(node)), node.value) in PROTOCOL_CONSTANTS:
                    continue
                offenders.append(f"{rel(path)}:{node.lineno} numeric literal {node.value!r}")
    assert offenders == [], "move tunables into config/*.json:\n" + "\n".join(offenders)


def test_protocol_constant_allowlist_is_minimal():
    source = (REPO_ROOT / "dreamstore/hooks/dp001_pre_tool_guard.py").read_text(encoding="utf-8")
    assert "BLOCK = 2" in source and len(PROTOCOL_CONSTANTS) == 1


def test_no_randomness_in_production_python():
    offenders = []
    for path in prod_python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "random" for alias in node.names):
                offenders.append(f"{rel(path)}:{node.lineno} import random")
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "random":
                offenders.append(f"{rel(path)}:{node.lineno} from random import")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "random":
                offenders.append(f"{rel(path)}:{node.lineno} random.{node.attr}")
    assert offenders == []


def test_no_silent_exception_handlers():
    offenders = []
    for path in prod_python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ExceptHandler) and all(isinstance(stmt, ast.Pass) for stmt in node.body):
                offenders.append(f"{rel(path)}:{node.lineno} silent except")
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                offenders.append(f"{rel(path)}:{node.lineno} bare except")
    assert offenders == []


def test_no_stub_functions():
    offenders = []
    for path in prod_python_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = [stmt for stmt in node.body if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))]
                if not body or all(isinstance(stmt, ast.Pass) for stmt in body):
                    offenders.append(f"{rel(path)}:{node.lineno} {node.name} is a stub")
    assert offenders == []


def _skip_quoted(src: str, index: int, quote: str) -> int:
    index += 1
    while index < len(src) and src[index] != quote:
        index += 2 if src[index] == "\\" else 1
    return index + 1


def _template(src: str, index: int, out: list[str]) -> int:
    index += 1
    while index < len(src) and src[index] != "`":
        if src[index] == "\\":
            index += 2
        elif src.startswith("${", index):
            depth, start = 1, index + 2
            index = start
            while index < len(src) and depth:
                if src[index] in "'\"":
                    index = _skip_quoted(src, index, src[index])
                    continue
                depth += {"{": 1, "}": -1}.get(src[index], 0)
                index += 1
            out.append(" " + strip_js(src[start:index - 1]) + " ")
        else:
            index += 1
    return index + 1


def strip_js(src: str) -> str:
    """Remove comments and string/template text from JavaScript, keeping template ${expressions}."""
    out: list[str] = []
    index = 0
    while index < len(src):
        if src.startswith("//", index):
            end = src.find("\n", index)
            index = len(src) if end < 0 else end
        elif src.startswith("/*", index):
            end = src.find("*/", index + 2)
            index = len(src) if end < 0 else end + 2
        elif src[index] in "'\"":
            index = _skip_quoted(src, index, src[index])
            out.append(" '' ")
        elif src[index] == "`":
            index = _template(src, index, out)
        else:
            out.append(src[index])
            index += 1
    return "".join(out)


def js_files() -> list[Path]:
    return sorted((REPO_ROOT / "app" / "static" / "js").glob("*.js"))


def test_js_stripper_handles_templates_and_comments():
    code = strip_js("const a = `x ${b + 7} y`; // 42\nconst c = '9'; /* 8 */")
    assert "7" in code and "42" not in code and "9" not in code and "8" not in code


def test_js_has_no_magic_numbers_random_or_inline_string_tables():
    number = re.compile(r"(?<![\w$.])(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)(?![\w$])")
    string_table = re.compile(r"\[\s*(?:(['\"])[^'\"]*\1\s*,\s*){3,}")
    offenders = []
    assert js_files(), "no JavaScript found"
    for path in js_files():
        raw = path.read_text(encoding="utf-8")
        code = strip_js(raw)
        offenders += [f"{rel(path)} numeric literal {m.group(1)}" for m in number.finditer(code) if float(m.group(1)) not in ALLOWED_NUMBERS]
        if "Math.random" in code:
            offenders.append(f"{rel(path)} Math.random")
        if string_table.search(raw):
            offenders.append(f"{rel(path)} inline string table")
        if re.search(r"https?://", raw.replace(SVG_NAMESPACE, "")):
            offenders.append(f"{rel(path)} external URL")
    assert offenders == [], "\n".join(offenders)
