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

To try changes against a real device, symlink the component into a Home
Assistant checkout and start it from there:

```bash
ln -s "$PWD/custom_components/opendisplay" /path/to/core/config/custom_components/
```

**No live HA? No OpenDisplay hardware?** `dev/run.sh` brings up a disposable
Home Assistant in Docker with this branch's integration mounted, and
`dev/inject-displays.py` fabricates config entries for a few realistic panels
(small mono / medium BWR / large BWRY) that set up entirely from cache — no
BLE connection, no pairing:

```bash
dev/run.sh                                        # bring up HA, onboard
docker compose -f dev/docker-compose.yml down     # stop (storage can't be
                                                   # rewritten under a live
                                                   # process)
uv run --group dev python dev/inject-displays.py  # fabricate 3 devices
dev/run.sh                                        # bring HA back up
```

See [`dev/README.md`](dev/README.md) for the full workflow, why no BLE
connection ever happens in this container, and the real-hardware
snapshot/restore path (`dev/snapshot.sh`/`dev/restore.sh`) if you do have a
device.

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
