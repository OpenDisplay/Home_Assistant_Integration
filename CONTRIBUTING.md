# Contributing

Feature requests and bug reports are welcome, and pull requests are encouraged.
The [Discord server](https://discord.com/invite/tw48NCrRxH) is the place to
discuss ideas or get help.

## Development

The test suite runs against a real Home Assistant instance via
[pytest-homeassistant-custom-component][phcc]. Dependencies are managed with
[uv][uv].

```bash
scripts/setup            # create the environment
scripts/test             # run the suite
scripts/test --min-ha    # run it against the oldest supported Home Assistant
scripts/test -k config   # any other argument goes through to pytest
scripts/lint             # ruff, fixing what it can
```

PHCC pins one exact Home Assistant version per release, so the version under
test is chosen by the `pytest-homeassistant-custom-component` pin in
`pyproject.toml`. There are two dependency groups: `dev` tracks the newest
release, and `min-ha` tracks the `hacs.json` floor. CI runs both, so a change
that only works on the newest Home Assistant fails before it ships. The `dev`
pin is bumped weekly by an automated pull request; `min-ha` moves by hand,
whenever the floor in `hacs.json` moves.

If a Home Assistant bump breaks the suite with a missing module, it is probably
a *component* requirement. Home Assistant installs those at runtime from each
component's own `manifest.json`, so they are invisible to uv and have to be
pinned by hand. `scripts/ha-component-reqs` prints the full set to compare
against `pyproject.toml`.

### Running the integration

Two ways to try changes against a real, running Home Assistant, picked by
what you already have available — both put
`custom_components/opendisplay` in front of a real `hass` process, so a
debugger attaches directly either way, same as any other native Python
program.

**You already have a Home Assistant checkout and real OpenDisplay
hardware**: symlink the component in and start Home Assistant from there:

```bash
ln -s "$PWD/custom_components/opendisplay" /path/to/core/config/custom_components/
```

**You don't have either** (no live HA, no OpenDisplay hardware): `dev/ha`
is this repo's own disposable-Home-Assistant harness — one entry point,
`dev/ha <subcommand>`, native Python (`uv run hass` under the hood, no
Docker, no container runtime; you never type the `uv run` yourself).
`dev/ha inject` fabricates config entries for a few realistic panels
(small mono / medium BWR / large BWRY) that set up entirely from cache —
no BLE connection, no pairing needed.

```bash
dev/ha run      # bring up HA, onboard
dev/ha stop     # stop (storage can't be rewritten under a live process)
dev/ha inject   # fabricate 3 devices
dev/ha run      # bring HA back up
```

See [`dev/README.md`](dev/README.md) for the full workflow (including
`dev/ha`'s other subcommands — `logs`, `token`, `snapshot`/`restore` for
carrying a real device's state between instances), why no BLE discovery
ever happens (the harness's `configuration.yaml` never loads the
`bluetooth` integration — no `default_config`, no explicit `bluetooth:`
key), and the real-hardware snapshot/restore path (`dev/ha snapshot`/
`dev/ha restore`) if you do have a device but want to capture its state
for a teammate who doesn't.

## Translations

English is written by hand; every other language is filled in by
`.github/workflows/translate.yml`. Corrections are welcome and are never
overwritten, which the README explains.

If you need to run or change the tooling, `scripts/translate.py` documents
itself: its module docstring covers provider selection, why `en.json` rather
than `strings.json` is the source of truth, and how manual edits are protected.
`--dry-run` previews a run without writing. `scripts/verify_translations.py`
checks the files on disk and is what CI runs.

## Commits and releases

Commit messages follow [Conventional Commits][cc]. Releases are cut
automatically by release-please on every push to `main`, so the commit type
decides both the next version and what the release notes say.

| type | version | in the notes |
|---|---|---|
| `feat` | minor | yes |
| `fix` | patch | yes |
| `perf`, `revert` | none | yes |
| `chore`, `refactor`, `docs`, `test`, `ci`, `build`, `style` | none | no |

A `!` after the type, or a `BREAKING CHANGE:` footer, bumps the major version.

Release notes are rendered on the HACS page, so they are written for someone
deciding whether to install, not for someone reading the diff. That is why the
internal types are hidden.

**Bumping `py-opendisplay` or `odl-renderer` is a `fix:`, not a `chore:`** (or a
`feat:` if the bump adds capability). Those are `manifest.json` requirements
that Home Assistant installs at runtime, so the bump changes what users
actually get. As a `chore:` it would neither appear in the release notes nor
trigger the release that ships it, and would sit unreleased until some
unrelated change landed.

Pull requests are merged with a merge commit, so every commit on the branch
lands on `main` and each one's type is read. Keep them all conventional, not
just the pull request title, which is not read at all.

[phcc]: https://github.com/MatthewFlamm/pytest-homeassistant-custom-component
[uv]: https://docs.astral.sh/uv/
[cc]: https://www.conventionalcommits.org/
