# Architecture and source boundaries

This repository combines a source-only host layer with a local pure
recompilation workflow for Mushihimesama Futari. The build driver validates a
user-supplied Ver. 1.5 game tree, writes a sanitized project manifest from
reviewed structured metadata, runs the pinned ReXGlue code generator against
the supplied XEX files, and compiles generated guest code with authored host
integration. It does not consume private decompilation overrides or generated
guest sources from another checkout.

The main components are:

- `config/futari15x360.json`: supported input SHA-256 values and reviewed
  function/switch hints. These are build metadata, not guest code or game
  payloads.
- `scripts/build_from_game.py` and its compatibility entrypoint
  `scripts/futari.py`: preflight, planning, external build-root ownership,
  pinned SDK setup, local code generation, guest compilation, and output
  verification. The `doctor` and `plan` stages do not write build artifacts.
- `templates/futari15-x360/` and `game-work/futari15-x360/src/`: the
  source-controlled CMake scaffold and handwritten host integration copied
  into the external generated project.
- `patches/rexglue-sdk-current.patch`: reviewed source changes against the
  pinned ReXGlue base; `patches/sdk-submodules.txt` records the required
  submodule revisions.
- `scripts/package_macos.py`, `scripts/stage_content_profiles.py`, and
  related helpers: optional local personal-app packaging and content staging.
  They require explicit external SDK/build paths and user-supplied inputs.

All SDK checkouts, generated manifests and guest translations, shader outputs,
build products, runtime libraries, package staging, caches, logs, receipts,
saves, and replays belong outside the repository. The source hygiene guard
rejects unreviewed paths, binary or opaque data, and non-text payloads. CI runs
source and synthetic checks, not the SDK, game generator, or application.

A successful compile establishes only that the selected source and user
inputs produced the expected artifacts. A fresh-source Apple-silicon build at
commit `f9d2e3a` (tree `6bc4db49`) did compile and verify all 15 expected
deliverables using a cold SDK/dependency setup and the supported user-provided
nine-XEX set. The game was not launched; rendering, input, gameplay, and
personal-app packaging remain untested. Historical acceptance reports refer
to the separately prepared older personal app, not to a build from this
repository.
