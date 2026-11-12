"""Test isolation: every test gets a temp AGENTLENS_HOME, a temp Claude settings
path and a temp HOME, so nothing can reach the real ~/.claude or ~/.agentlens."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REAL_HOME = Path(os.path.expanduser("~")).resolve()
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    al_home = tmp_path / "agentlens"
    settings = fake_home / ".claude" / "settings.json"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AGENTLENS_HOME", str(al_home))
    monkeypatch.setenv("AGENTLENS_CLAUDE_SETTINGS", str(settings))

    from agentlens import paths

    sandbox = tmp_path.resolve()
    for p in (paths.home(), paths.claude_settings_path(), Path.home()):
        resolved = p.resolve()
        assert resolved == sandbox or sandbox in resolved.parents, (
            f"test path escaped sandbox: {p}"
        )
    yield {"home": fake_home, "agentlens_home": al_home, "settings": settings}


@pytest.fixture
def subprocess_env(isolated_env):
    """Environment for running the real CLI in a subprocess."""
    env = dict(os.environ)
    src = str(Path(__file__).parent.parent / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    return env


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / "claude_code" / f"{name}.json").read_text())


@pytest.fixture
def fixture():
    return load_fixture


PYTHON = sys.executable
