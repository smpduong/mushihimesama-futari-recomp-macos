# Build and packaging scope

This source snapshot is not a complete clean-machine build recipe for the
game. It omits the game project manifests, generated guest modules, original
game inputs, and prepared dependency caches needed by the local guest build.
It is also not a decompilation release.

The packaging tools can assemble a personal macOS app only when supplied with
an already-prepared matching executable/runtime, game data, and any required
content profiles. The caller must own or have permission to use those inputs.
Nothing in this repository downloads or supplies them.

## SDK reference points

The SDK patch is based on ReXGlue commit
`c94f5ebdcb3c9d1a460ca48e04f9758448f8d518`. Its recorded MoltenVK override is
`db66022459ffb663aa2b50f6b018bc2e124f5edf` (v1.4.2); the remaining pinned
submodule revisions are listed in [`../patches/sdk-submodules.txt`](../patches/sdk-submodules.txt).
The patch is an older checkpoint. The included launcher lifecycle source
fragments are newer and require integration review against that pinned tree.
No combined clean rebuild has been validated here.

Restoring or building the upstream SDK requires obtaining its source and
submodules from their upstream repositories and following their build
instructions. This repository does not vendor those dependencies. Review the
complete license and notice files at the exact pinned revisions before
redistributing any resulting build; see
[`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

## Local packaging tools

Use Python 3.11 or later. Native app packaging also requires macOS and the
Apple command-line toolchain, including Clang and the system frameworks used
by the launcher and replay helper. Inspect the available arguments with:

```sh
python3 -B scripts/futari.py --help
python3 -B scripts/stage_content_profiles.py --help
python3 -B scripts/package_macos.py --help
```

The following shows the explicit path inputs expected by the app packager.
Replace every placeholder with a local path to an already-prepared build and
game data that you are entitled to use; this command does not fetch or create
those inputs:

```sh
python3 -B scripts/package_macos.py \
  --output /path/to/new-output/Futari.app \
  --build-dir /path/to/prepared-build \
  --game-executable /path/to/prepared-build/futari15x360 \
  --runtime-dir /path/to/prepared-build \
  --game-data-dir /path/to/user-provided-game-data \
  --content-profiles-dir /path/to/staged-content-profiles
```

The repository CI runs only source-tree hygiene and synthetic Python tests.
It does not download or build the SDK or game, compile a game executable, or
run the application.
