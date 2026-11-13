# Releasing AgentLens

Releases go to PyPI as **`agentlens-local`** through `.github/workflows/release.yml`,
using PyPI Trusted Publishing (OIDC). No API token is stored anywhere.

## One-time setup (maintainer)

1. **PyPI pending publisher.** On pypi.org → Account → Publishing → "Add a new
   pending publisher": project `agentlens-local`, owner `spikethebuild`,
   repository `AgentLens`, workflow `release.yml`, environment `pypi`.
   A pending publisher does not reserve the name; the first successful upload does.
2. **TestPyPI (optional rehearsal).** Same on test.pypi.org with environment `testpypi`.
3. **GitHub environments.** Repo → Settings → Environments: create `pypi`
   (add yourself as a required reviewer) and `testpypi`.
4. **Tag protection.** Repo → Settings → Rules: a tag ruleset for `v*` so only
   maintainers can create release tags.

## Version source

The version lives in one place: `src/agentlens/__init__.py` (`__version__`).
`pyproject.toml` reads it dynamically. `.claude-plugin/plugin.json` must match;
`scripts/release_tools.py check` and the test suite enforce this.

## Cutting a release

1. Set `__version__` and the `version` in `.claude-plugin/plugin.json`.
2. In `CHANGELOG.md`, give the section a date: `## [X.Y.Z] - YYYY-MM-DD`.
3. Check locally:
   ```bash
   .venv/bin/pytest -q
   python scripts/release_tools.py check --tag vX.Y.Z
   ```
4. Optional rehearsal: Actions → Release → "Run workflow". This runs the tests,
   builds, smoke-tests the wheel and uploads to TestPyPI only.
5. Commit, then tag and push:
   ```bash
   git tag -a vX.Y.Z -m "AgentLens X.Y.Z"
   git push origin main vX.Y.Z
   ```
6. The workflow runs the CI matrix, checks that tag, version and changelog agree,
   builds and `twine check`s the sdist and wheel, smoke-tests the wheel in a fresh
   virtualenv, waits for approval in the `pypi` environment, publishes (with
   attestations) and creates a GitHub Release whose notes are that version's
   CHANGELOG section.

## If something goes wrong

- A failed check before publishing: fix, delete the tag
  (`git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z`), tag again.
- PyPI never accepts the same version twice. If a bad build was published, yank it
  on PyPI and release the next patch version.
