# Dev Packaging Notes

## Backend
- Build backend: `hatchling` (backend only).
- Workflow frontend: `uv` (env, run, build, test).
- Versioning strategy: static in `pyproject.toml` (`[project].version`).
- NOX is intentionally not used in this repository.

## Build Commands
- Build both sdist and wheel:
  - `uv build`
- Install editable dev environment:
  - `uv sync --extra dev`

## Runtime Version Behavior
- `video_duperz.__version__` is resolved from installed package metadata.
- When package metadata is unavailable (direct source execution), fallback is `0.0.0+local`.

## Lockfile
- Commit `uv.lock` to version control.
- Use `uv sync --extra dev --frozen` in CI for deterministic resolution.

## Release Expectations
- Update `[project].version` in `pyproject.toml` before release.
- Ensure `uv build` and `uv run pytest -q` pass before publishing artifacts.
