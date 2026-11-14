"""Test helpers shared across modules (kept out of conftest so imports stay explicit)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from app.config_store import REPO_ROOT

# Deterministic test-only key (64 hex chars); production keys come from Secret Manager via AUDIT_HMAC_KEY.
TEST_HMAC_KEY = hashlib.sha256(b"osprey-core pytest audit key").hexdigest()
HEX32 = r"^[0-9a-f]{32}$"
HEX64 = r"^[0-9a-f]{64}$"


def rewrite(path: Path, text: str) -> None:
    """Write `text` and push mtime forward so the stat-signature cache always sees the change."""
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(text, encoding="utf-8")
    bumped = max(before, path.stat().st_mtime_ns) + 1_000_000_000
    os.utime(path, ns=(bumped, bumped))


def edit_json(path: Path, mutate: Callable[[Any], Any]) -> Any:
    """Load JSON, let `mutate` change it in place (its return value is ignored), and write it back."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    rewrite(path, json.dumps(doc, indent=2))
    return doc


def load_json(relpath: str) -> Any:
    return json.loads((REPO_ROOT / relpath).read_text(encoding="utf-8"))
