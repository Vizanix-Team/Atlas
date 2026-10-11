# Releasing

Atlas is published to PyPI as **`vizanix-atlas`**. The name `atlas` is taken by an unrelated project, so `pip install atlas` installs something else. The package provides the `atlas` command (and `vizanix-atlas`, the unambiguous spelling for scripts) and the `vizanix_atlas` module.

Four version numbers exist and are kept apart (see `core/versions.py`): the software version (this document), the schema version, the methodology version and the dataset format version.

## One-time setup (PyPI trusted publishing)

Publishing uses PyPI's trusted publishers (OIDC). No token is created, stored or rotated.

1. On <https://pypi.org/manage/account/publishing/>, add a **pending publisher** for a new project:
   - PyPI project name: `vizanix-atlas`
   - Owner: `Vizanix-Team`, repository: `Atlas`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`
2. In the repository, **Settings → Environments → New environment** named `pypi`. Add required reviewers if you want a manual gate before each upload.
3. In **Settings → Secrets and variables → Actions → Variables**, create `PUBLISH_TO_PYPI` with the value `true`. Until it exists the `pypi` job is skipped, so a tag push still produces the GitHub release.

## Cutting a release

1. Update `SOFTWARE_VERSION` in `src/vizanix_atlas/core/versions.py` **and** `version` in `pyproject.toml`. They must match: the release workflow refuses a tag that does not equal the package version.
2. Move the `[Unreleased]` notes in `CHANGELOG.md` under the new version.
3. Check locally:

   ```bash
   pip install build twine
   python -m build
   twine check --strict dist/*
   ```

4. Tag and push:

   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```

The `Release` workflow then:

1. runs `ruff`, `mypy` and the full test suite;
2. checks the tag equals the package version;
3. builds the sdist and the wheel and runs `twine check --strict`;
4. **installs the wheel on Linux, macOS and Windows** (Python 3.11 and 3.13), away from the source tree, and checks the entry point, the bundled configuration and the venue count, so a missing data file or a broken entry point is caught here and nowhere else;
5. creates the GitHub release with the artifacts;
6. publishes to PyPI.

A PyPI version can never be re-uploaded or replaced, only yanked. Check the smoke job's result before approving the `pypi` environment.

## README on PyPI

`pyproject.toml` points `readme` at `PYPI_README.md`, a text-first description. The repository `README.md` uses relative-path graphics that PyPI cannot resolve.
