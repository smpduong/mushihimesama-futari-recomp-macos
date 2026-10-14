# Futari host and packaging sources

This repository contains selected handwritten host-side code, macOS packaging
and replay-transfer tools, focused synthetic tests, and a pinned ReXGlue SDK
patch with related lifecycle source fragments. It is an asset-free source
snapshot. It is not a complete decompilation, a standalone clean-machine game
build, or a game release.

You must provide your own game files and any prepared guest build/runtime when
using the packaging tools. No game or DLC files, generated guest translations,
compiled game modules, shaders, saves, or replays are included. See
[`docs/BUILDING.md`](docs/BUILDING.md) for the supported scope and its limits.

## Reported acceptance scope

The following user-reported observations apply to specific combinations only:

- One Base Arrange Arrange / Original difficulty / Reco full run.
- A real replay save, relaunch, export, import into a fresh second profile, and
  full playback of the imported replay.
- Black Label past the Stage 1 boss and replay in God mode.
- Ver. 1.01 and Both reported working.

These observations do not claim that every mode, profile, stage, or build has
been tested.

## License

The root [`LICENSE`](LICENSE) covers original project-authored source and
documentation only. It does not relicense ReXGlue-derived files or the
libarchive declaration subset; see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
No license for the game or its data is provided.

## Repository checks

Run the source-tree hygiene scan and synthetic archive tests with Python 3:

```sh
python3 -B scripts/check_public_tree.py
python3 -B -m unittest \
  scripts.test_public_source_hygiene \
  scripts.test_source_setup_archive.SourceSetupArchiveTests.test_archive_contains_exact_allowlist_and_no_unlisted_asset \
  scripts.test_source_setup_archive.SourceSetupArchiveTests.test_existing_archive_is_never_overwritten \
  scripts.test_source_setup_archive.SourceSetupArchiveTests.test_missing_allowlisted_file_fails_before_creation
```

These checks inspect the repository contents and exercise synthetic fixtures.
They do not download the SDK or game files, build the game, or establish
gameplay compatibility.
