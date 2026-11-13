"""Docs stay in step with the code: every command is documented, release files exist."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from agentlens import cli

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()


def subcommands() -> list[str]:
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    hidden = {c.dest for c in action._choices_actions if c.help == argparse.SUPPRESS}
    return [name for name in action.choices if name not in hidden and name != "hook"]


def command_table() -> str:
    start = README.index("## Commands")
    return README[start:README.index("\n## ", start + 1)]


@pytest.mark.parametrize("name", subcommands())
def test_every_subcommand_is_in_the_readme_table(name):
    assert re.search(rf"`agentlens {re.escape(name)}\b", command_table()), f"README command table lacks {name}"


def test_release_docs_exist_and_link_up():
    for name in ("SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md", "RELEASING.md", "LICENSE"):
        assert (ROOT / name).is_file(), name
    assert "Report a vulnerability" in (ROOT / "SECURITY.md").read_text()
    for section in ("## Quick start", "## Troubleshooting", "## Uninstall", "## Privacy"):
        assert section in README
    assert "v0.3 cache coach" not in README  # stale status line


def test_issue_templates_parse_and_warn_about_secrets():
    yaml = pytest.importorskip("yaml")
    folder = ROOT / ".github" / "ISSUE_TEMPLATE"
    bug = yaml.safe_load((folder / "bug_report.yml").read_text())
    assert bug["name"] and any("agentlens doctor" in str(item) for item in bug["body"])
    assert "Do not paste secrets" in str(bug["body"][0])
    assert yaml.safe_load((folder / "feature_request.yml").read_text())["name"]
    assert yaml.safe_load((folder / "config.yml").read_text())["blank_issues_enabled"] is False
