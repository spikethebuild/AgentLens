#!/usr/bin/env python3
"""Release checks and notes for AgentLens (standard library only).

    python scripts/release_tools.py check [--tag vX.Y.Z]   # exit 1 with reasons on mismatch
    python scripts/release_tools.py notes [--tag vX.Y.Z]   # print that version's CHANGELOG section

`check` verifies that the single version source (src/agentlens/__init__.py), the
plugin manifest and CHANGELOG.md agree. With --tag (a real release) it also
requires the tag to be "v" + version and the CHANGELOG heading to carry a date.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>.+?))?\s*$", re.MULTILINE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def package_version(root: Path = ROOT) -> str:
    m = _VERSION.search((root / "src" / "agentlens" / "__init__.py").read_text())
    if not m:
        raise ValueError("no __version__ in src/agentlens/__init__.py")
    return m.group(1)


def changelog_section(text: str, version: str) -> tuple[str | None, str] | None:
    """(date or None, body) of the ``## [version]`` section, or None if absent."""
    headings = list(_HEADING.finditer(text))
    for i, h in enumerate(headings):
        if h.group("version") == version:
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return h.group("date"), text[h.end():end].strip()
    return None


def check(root: Path = ROOT, tag: str | None = None) -> list[str]:
    problems: list[str] = []
    version = package_version(root)
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text()).get("version")
    if plugin != version:
        problems.append(f".claude-plugin/plugin.json version {plugin!r} != package version {version!r}")
    section = changelog_section((root / "CHANGELOG.md").read_text(), version)
    if section is None:
        problems.append(f"CHANGELOG.md has no '## [{version}]' section")
    elif not section[1]:
        problems.append(f"CHANGELOG.md section for {version} is empty")
    if tag is not None:
        if tag != f"v{version}":
            problems.append(f"tag {tag!r} does not match package version (expected 'v{version}')")
        if section is not None and not (section[0] and _DATE.match(section[0])):
            problems.append(f"CHANGELOG.md heading for {version} needs a release date (YYYY-MM-DD), "
                            f"found {section[0]!r}")
    return problems


def notes(root: Path = ROOT, tag: str | None = None) -> str:
    version = tag[1:] if tag and tag.startswith("v") else package_version(root)
    section = changelog_section((root / "CHANGELOG.md").read_text(), version)
    if section is None:
        raise ValueError(f"CHANGELOG.md has no '## [{version}]' section")
    return section[1] + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["check", "notes"])
    p.add_argument("--tag", default=None, help="release tag, e.g. v0.1.1")
    args = p.parse_args(argv)
    tag = args.tag or None
    if args.command == "check":
        problems = check(tag=tag)
        for line in problems:
            print(f"release check: {line}", file=sys.stderr)
        if not problems:
            print(f"release check: ok (version {package_version()}{', tag ' + tag if tag else ''})")
        return 1 if problems else 0
    sys.stdout.write(notes(tag=tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
