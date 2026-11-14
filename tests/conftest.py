"""Shared fixtures: the real app, isolated writable config copies, and an injected test HMAC key."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from app import telemetry
from app.app import create_app
from app.config_store import REPO_ROOT
from tests.helpers import TEST_HMAC_KEY


@pytest.fixture(autouse=True)
def _isolate_telemetry() -> Any:
    telemetry.RING.clear()
    yield
    telemetry.use_stream(None)


@pytest.fixture
def audit_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("AUDIT_HMAC_KEY", TEST_HMAC_KEY)
    return TEST_HMAC_KEY


@pytest.fixture
def no_audit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)


@pytest.fixture
def app(audit_key: str) -> Any:
    flask_app = create_app()
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def client(app: Any) -> Any:
    return app.test_client()


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    """Writable copy of everything ConfigStore reads (config/, eval set, committed scorecard)."""
    root = tmp_path / "root"
    shutil.copytree(REPO_ROOT / "config", root / "config")
    shutil.copytree(REPO_ROOT / "dreamstore" / "evals", root / "dreamstore" / "evals")
    shutil.copy2(REPO_ROOT / "dreamstore" / "scorecard.json", root / "dreamstore" / "scorecard.json")
    return root


@pytest.fixture
def temp_app(temp_root: Path, audit_key: str) -> Any:
    flask_app = create_app(temp_root)
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def temp_client(temp_app: Any) -> Any:
    return temp_app.test_client()
