# Agent instructions

This file exists so an automated contributor or reviewer has this repo's
own facts on hand instead of guessing. Concrete example: a review bot
flagged `except AttributeError, IndexError, TypeError, ValueError:` in
`designer/image_entity.py` as invalid Python 2-style syntax — it is legal
Python 3 here, exactly the kind of version-knowledge false positive this
file exists to prevent.

Start with [`CONTRIBUTING.md`](CONTRIBUTING.md) — setup, the two-leg test
story, HA component requirements, translations, commit/release rules. This
file adds only what a tool with no memory of this repo tends to get wrong.

## Python

Floor is 3.14.2 (`pyproject.toml`'s `requires-python`), so 3.14 syntax is
in scope, not a mistake. [PEP 758](https://peps.python.org/pep-0758/)
legalizes unparenthesized `except A, B, C:`, already used in
`custom_components/opendisplay/__init__.py` and
`custom_components/opendisplay/designer/image_entity.py` — do not "fix" it
to `except (A, B, C):` or flag it as Python 2 syntax.

## Commits

Every commit on a branch, not just the PR title, must be a [Conventional
Commit](https://www.conventionalcommits.org/) — PRs merge with a merge
commit, and release-please reads each commit's type to decide the release.
A runtime dependency bump (`py-opendisplay`, `odl-renderer`) is
`fix:`/`feat:`, never `chore:`, or it ships silently, unreleased.

## Tests

`scripts/test` and `scripts/test --min-ha` both gate (`--min-ha` against
`hacs.json`'s floor HA version, plain `scripts/test` against the newest); a
`--min-ha`-only failure is a bug to fix or a reason to raise the floor,
never one to weaken or skip that leg. `scripts/lint` runs ruff. A missing
module after an HA bump is usually a *component* requirement (invisible to
`uv`, HA installs it at runtime) — pin it by hand; `scripts/ha-component-reqs`
prints the current set. `dev/ha run` brings up a real, disposable,
hardware-free Home Assistant against this checkout.

## Generated — do not hand-edit

- `custom_components/opendisplay/designer/frontend/vendor/` — regenerate
  only via `scripts/update-designer-vendor.py` (checksum-verified).
- `custom_components/opendisplay/translations/*.json` except `en.json` —
  written by `.github/workflows/translate.yml`; manual corrections are
  fingerprinted and protected (`.github/translation-state.json`).
- `uv.lock` — regenerate with `uv lock`/`uv sync`.
- `manifest.json`'s `"version"` — written by release-please
  (`.release-please-config.json`); the rest of the manifest is hand-edited.

## CI

`.github/workflows/preview-release.yml` cuts installable HACS builds from
branches pushed to a **fork** only — inert on this repository itself.
