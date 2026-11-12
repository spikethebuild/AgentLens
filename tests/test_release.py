"""Packaging and release automation: version source, release checks, workflow safety."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

import agentlens

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

_spec = importlib.util.spec_from_file_location("release_tools", ROOT / "scripts" / "release_tools.py")
release_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_tools)


# ------------------------------------------------------------- version source


def test_single_version_source():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in text
    assert 'version = { attr = "agentlens.__version__" }' in text
    assert not re.search(r"^version\s*=\s*\"", text, re.MULTILINE)  # no second literal version
    assert release_tools.package_version() == agentlens.__version__
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == agentlens.__version__


def test_distribution_name_and_entry_point():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'name = "agentlens-local"' in text
    assert 'agentlens = "agentlens.cli:main"' in text
    assert "dependencies = []" in text  # runtime stays standard-library only


# ------------------------------------------------------------ release tools


def make_repo(tmp_path: Path, version="0.1.1", plugin="0.1.1", changelog=None) -> Path:
    (tmp_path / "src" / "agentlens").mkdir(parents=True)
    (tmp_path / "src" / "agentlens" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "agentlens", "version": plugin}))
    (tmp_path / "CHANGELOG.md").write_text(changelog if changelog is not None else
        "# Changelog\n\n## [0.1.1] - 2026-11-20\n\n### Added\n- thing\n\n## [0.1.0] - 2026-11-01\n\n- old\n")
    return tmp_path


def test_check_passes_for_matching_tag(tmp_path):
    assert release_tools.check(make_repo(tmp_path), tag="v0.1.1") == []


@pytest.mark.parametrize("kwargs, tag, needle", [
    ({}, "v0.1.2", "does not match"),
    ({"plugin": "0.1.0"}, None, "plugin.json"),
    ({"changelog": "# Changelog\n\n## [0.1.0] - 2026-11-01\n- old\n"}, None, "no '## [0.1.1]'"),
    ({"changelog": "## [0.1.1] - Unreleased\n\n- x\n"}, "v0.1.1", "needs a release date"),
    ({"changelog": "## [0.1.1] - 2026-11-20\n\n## [0.1.0]\n- x\n"}, None, "is empty"),
])
def test_check_reports_problems(tmp_path, kwargs, tag, needle):
    problems = release_tools.check(make_repo(tmp_path, **kwargs), tag=tag)
    assert any(needle in p for p in problems), problems


def test_unreleased_section_is_fine_for_a_rehearsal(tmp_path):
    repo = make_repo(tmp_path, changelog="## [0.1.1] - Unreleased\n\n- x\n")
    assert release_tools.check(repo) == []


def test_notes_extract_only_that_version(tmp_path):
    repo = make_repo(tmp_path)
    notes = release_tools.notes(repo, tag="v0.1.1")
    assert notes == "### Added\n- thing\n"


def test_repo_changelog_has_current_version():
    assert release_tools.check() == []


# --------------------------------------------------------------- workflows

yaml = pytest.importorskip("yaml")


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key `on` as True
    return data


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def test_ci_matrix_and_stdlib_job():
    ci = load("ci.yml")
    assert {"push", "pull_request", "workflow_call"} <= set(ci["on"])
    matrix = ci["jobs"]["test"]["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "macos-latest"}
    assert matrix["python"] == ["3.10", "3.11", "3.12", "3.13", "3.14"]
    assert "stdlib-only" in ci["jobs"]
    assert ci["permissions"] == {"contents": "read"}


def test_release_uses_trusted_publishing_without_tokens():
    text = (WORKFLOWS / "release.yml").read_text()
    rel = load("release.yml")
    assert rel["on"]["push"]["tags"] == ["v*"] and "workflow_dispatch" in rel["on"]
    assert rel["permissions"] == {"contents": "read"}  # nothing elevated at workflow level
    assert "secrets." not in text and "password:" not in text and "PYPI_API_TOKEN" not in text
    jobs = rel["jobs"]
    for name, env, url in (("publish-pypi", "pypi", "https://pypi.org/p/agentlens-local"),
                           ("publish-testpypi", "testpypi", "https://test.pypi.org/p/agentlens-local")):
        job = jobs[name]
        assert job["permissions"] == {"id-token": "write"}
        assert job["environment"] == {"name": env, "url": url}
        assert job["needs"] == "build" and job["runs-on"] == "ubuntu-latest"
        uses = [s.get("uses", "") for s in steps(job)]
        assert uses[-1] == "pypa/gh-action-pypi-publish@release/v1"
        assert not any("checkout" in u for u in uses)  # publish jobs only download built files
        assert not any("run" in s for s in steps(job))
    assert "repository-url" in steps(jobs["publish-testpypi"])[-1]["with"]
    assert jobs["publish-pypi"]["if"].startswith("github.event_name == 'push'")
    assert jobs["publish-testpypi"]["if"] == "github.event_name == 'workflow_dispatch'"
    assert jobs["build"]["needs"] == "test" and jobs["test"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["github-release"]["needs"] == "publish-pypi"
    assert jobs["github-release"]["permissions"] == {"contents": "write"}


def test_workflows_never_interpolate_refs_into_shell():
    for path in WORKFLOWS.glob("*.yml"):
        data = load(path.name)
        for job in data["jobs"].values():
            for step in steps(job):
                assert "${{" not in step.get("run", ""), (path.name, step.get("name"))
            for step in steps(job):
                if step.get("uses", "").startswith("actions/checkout"):
                    assert step["with"]["persist-credentials"] is False, path.name
