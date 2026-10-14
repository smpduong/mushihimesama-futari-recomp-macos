# Mushihimesama Futari recompilation sources

This repository contains the authored host/runtime integration, a pinned ReXGlue
SDK patch, and a source-only workflow for generating and compiling a supported
Mushihimesama Futari Ver. 1.5 build from game files supplied by the user. It
does not contain game files, DLC, generated guest code, shaders, compiled
outputs, caches, saves, or replays. It is not a decompilation release.

The build workflow accepts one exact nine-XEX Ver. 1.5 set; paths and SHA-256
values are recorded in [`config/futari15x360.json`](config/futari15x360.json).
It generates guest translations locally from those inputs. It does not use
private decompilation overrides or a prebuilt guest/runtime cache. The base
build does not require DLC. See [`docs/BUILDING.md`](docs/BUILDING.md) for
prerequisites, commands, and the current build-validation boundary.

The fresh-source Apple-silicon build and all 15 expected deliverables were
verified from the supported nine-XEX input set. This establishes compile and
artifact validation only: the build was not launched, gameplay and the new
personal-app package were not tested, and this result does not establish
equivalence to the separately reported older personal app. Those historical
reports apply only to specific combinations: One Base Arrange Arrange /
Original difficulty / Reco full run; a real replay saved, relaunched, exported,
imported into a fresh second profile, and played through; Black Label past the
Stage 1 boss and replay in God mode; and Ver. 1.01 and Both reported working.
They do not imply all modes, stages, profiles, or this new pure-recomp build
have been gameplay-tested.

The root [`LICENSE`](LICENSE) covers original project-authored material only.
Third-party notices and license scope are in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). No game or game-data license
is provided.

## Source-only checks

The CI workflow runs the exact-tree source hygiene guard and selected synthetic
tests. To run the same bounded checks locally:

```sh
python3 -B scripts/check_public_tree.py
python3 -B -m unittest \
  scripts.test_public_source_hygiene \
  scripts.test_build_from_game \
  scripts.test_public_output_guard \
  scripts.test_source_setup_archive.SourceSetupArchiveTests.test_archive_contains_exact_allowlist_and_no_unlisted_asset \
  scripts.test_source_setup_archive.SourceSetupArchiveTests.test_existing_archive_is_never_overwritten \
  scripts.test_source_setup_archive.SourceSetupArchiveTests.test_missing_allowlisted_file_fails_before_creation
```

These checks do not download the game or run a build, launch the game, or prove
gameplay compatibility. Optional upstream SDK test targets are disabled in the
documented build because they include PPC assembler and test configurations
not required by this project; this repository does not claim those tests pass.
