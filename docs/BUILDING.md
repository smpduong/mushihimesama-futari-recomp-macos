# Building from user-supplied game files

This workflow builds a pure recompilation candidate from a supported Ver. 1.5
XEX set and its extracted game-data tree. It generates guest code from the
supplied XEX files during the build. It does not use private decompilation
overrides, generated guest sources from another checkout, or a prebuilt
game/runtime cache. No game files are included here.

## Build validation result

An Apple-silicon build completed from a fresh source clone at commit `f9d2e3a`
(tree `6bc4db49`), a cold SDK checkout and dependency cache, and the supported
nine user-supplied XEX inputs. The worker first resolved source-level template,
SDK-patch, and resume issues, then resumed the same tool-owned external build
lane. Compilation and verification of all 15 expected deliverables passed:
the host executable, eight module libraries, runtime, GPU and Tracy libraries,
two Vulkan libraries, and the Vulkan ICD file.

No game launch, live rendering/input, gameplay, or new personal-app packaging
was performed. The build result does not show equivalence to the separately
reported historical personal app or establish compatibility across modes.

## Supported inputs

Provide a read-only extracted game directory with this exact layout:

```text
<game-dir>/
  default.xex
  media/
    Module/
      reco_normal.xex
      reco_abnormal.xex
      palm_normal.xex
      palm_abnormal.xex
      kiniro_normal.xex
      kiniro_abnormal.xex
      hirow_normal.xex
      hirow_abnormal.xex
```

All nine files must match this supported Ver. 1.5 SHA-256 set; the build stops
if any file is missing or differs.

| Relative path | SHA-256 |
| --- | --- |
| `default.xex` | `6e61934468463c8d023588a1e14b2269002dec6987b51538154843ea6869a434` |
| `media/Module/reco_normal.xex` | `cf13982ed0af96769a432666604322a759e4247a46e40259c575036925e38fcf` |
| `media/Module/reco_abnormal.xex` | `c55f633ff8e1cba140279f4ab0d6bb7389a2151ef80d0da3245a8d1dd068e754` |
| `media/Module/palm_normal.xex` | `b8857eeb0a814a550176746d2dfc05d3d9c5866ab05a2716911dbbdf49ec73d6` |
| `media/Module/palm_abnormal.xex` | `e37ec86e0d2c2f176a6c9dc4486e982235008628bd4765e33fc717c95da34222` |
| `media/Module/kiniro_normal.xex` | `8d1df7da086e7ce36e804cef43bf5afadaa5e66f7196a54bb2757be535c3681c` |
| `media/Module/kiniro_abnormal.xex` | `da3ab8cb8edb287a10866ad262b7bc5ceeacd431044d59bdf0b85067a5792f20` |
| `media/Module/hirow_normal.xex` | `9c3c9d8b56709c0b50a3953fcc8d42ea950f5cb05f9339cc68a197e643cc3bd4` |
| `media/Module/hirow_abnormal.xex` | `92ee27efd7e6c0ed4e4ea086fe53ac015bb018be658c0689dfd771936ff39050` |

The same structured metadata is maintained in
[`../config/futari15x360.json`](../config/futari15x360.json). The remaining
game-data tree is supplied unchanged for local use; it is not copied into the
source checkout. This base compile does not require DLC packages.

## Host prerequisites

- Apple silicon Mac running macOS.
- Python 3.11 or later, CMake 3.25 or later, Ninja, Git, and Apple Command Line
  Tools (`/usr/bin/clang` and `/usr/bin/clang++`).
- At least 12 GiB free on the filesystem containing the external build root.
- Network access during a fresh build to obtain the pinned ReXGlue source,
  submodules, and build dependencies, unless they are already available through
  the workflow's external build-root cache. The workflow never downloads game
  files and does not launch the game.

All generated project files, SDK source, dependency cache, object files,
executables, libraries, logs, and receipts must stay in a new external
build-root directory outside Git worktrees. Keep the supplied game directory
separate and read-only. Do not point the build root inside this repository.

## Preflight and build

From the repository root, choose paths that exist for the game directory and
its parent, and a new name for the external build root. First run the read-only
preflight and plan:

```sh
python3 -B scripts/futari.py \
  --game-dir "/path/to/extracted-game" \
  --build-root "/path/to/external-builds/futari-ver15" \
  --stage doctor

python3 -B scripts/futari.py \
  --game-dir "/path/to/extracted-game" \
  --build-root "/path/to/external-builds/futari-ver15" \
  --stage plan
```

`doctor` validates host prerequisites, path separation, all nine XEX hashes,
and the source patch pin. `plan` prints the planned commands. Both are
read-only. Build only after reviewing the selected paths and plan:

```sh
python3 -B scripts/futari.py \
  --game-dir "/path/to/extracted-game" \
  --build-root "/path/to/external-builds/futari-ver15" \
  --stage build --jobs 2
```

The job count is limited to 1–8 and defaults to 2. The build root is
tool-owned: a fresh build creates it and records its identity before writing
the SDK checkout, generated project, build tree, logs, and receipts. The SDK
is built from the pinned source and this repository's reviewed patch; guest
translations are generated locally before guest compilation. Optional upstream
SDK tests are disabled. The resulting candidate executable, if the build
succeeds, is under `<build-root>/build/futari15x360`; all build artifacts
remain outside the repository.

To continue a previously initialized build root, use `--resume`:

```sh
python3 -B scripts/futari.py \
  --game-dir "/path/to/extracted-game" \
  --build-root "/path/to/external-builds/futari-ver15" \
  --stage build --resume --jobs 2
```

Resume is accepted only when the tool-owned root marker matches the canonical
build-root and game paths, the exact nine input hashes, structured build
configuration, SDK patch, and source recipe identity (including Git HEAD when
available). Changed or unrecognized state is refused rather than overwritten.
Do not manually edit generated project files or reuse another checkout's SDK,
cache, guest sources, or build outputs.

The pinned SDK's code generator refreshes `project/generated/rexglue.cmake`.
On resume, that one file is accepted only when it matches either the checked-in
seed template or the exact pinned emitter output, SHA-256
`ef1ff108d9753c5ca3be1cacf74d8f84e78cdfc0d8aaa8b994237ceeac19505c`. After
codegen, the build checks that the file is regular, not a symlink, and matches
that hash before continuing to the next configure step. Other generated
project inputs retain their strict template/content checks.

## Optional personal app packaging

Packaging is a separate step after a suitable build and verified external SDK
exist. It requires user-supplied game data and, for the full four-profile
personal app, both local Ver. 1.01 and Black Label DLC packages. This optional
content work is not needed for the base nine-XEX compile. The tools do not
download or provide either package. They read package inputs and guard output
paths to keep outputs outside the source, SDK, build, and input trees. Never
commit or distribute a resulting app, package, game data, or content files
through this repository. This optional packaging path has not yet been
validated on the new pure-recompilation build.

Example paths below are placeholders. `--sdk-dir` must point to the verified
external SDK checkout at `<build-root>/sdk`.

```sh
python3 -B scripts/stage_content_profiles.py \
  --sdk-dir "/path/to/external-builds/futari-ver15/sdk" \
  --output-dir "/path/to/external-packaging/futari-profiles" \
  --build-dir "/path/to/external-builds/futari-ver15/build" \
  --runtime-dir "/path/to/external-builds/futari-ver15/build" \
  --ver101-package "/path/to/local/Ver101.package" \
  --blacklabel-package "/path/to/local/BlackLabel.package"

python3 -B scripts/package_macos.py \
  --sdk-dir "/path/to/external-builds/futari-ver15/sdk" \
  --output "/path/to/external-packaging/Futari.app" \
  --build-dir "/path/to/external-builds/futari-ver15/build" \
  --game-executable "/path/to/external-builds/futari-ver15/build/futari15x360" \
  --runtime-dir "/path/to/external-builds/futari-ver15/build" \
  --game-data-dir "/path/to/extracted-game" \
  --content-profiles-dir "/path/to/external-packaging/futari-profiles/ContentProfiles"
```

Keep the profile-staging output and app as siblings of, not children of, the
marker-owned build root; that root intentionally rejects unrelated outputs so
it remains safely resumable. Here `stage_content_profiles.py` writes the
`ContentProfiles/` directory beneath the selected `--output-dir`.

The optional `scripts/install_dlc_via_runtime.py` helper also requires an
explicit `--sdk-dir`; it installs only to a caller-selected external
destination. No install or packaging action is performed by the base build.

## Validation boundaries

CI checks the exact public source tree and selected synthetic tests only. It
does not fetch dependencies, compile the game, run the app, or test gameplay.
The successful build described above is a local compile/artifact result only.
Historical user-reported acceptance applies to a specific older personal app
and remains separate from this new pure-recompilation workflow. Report build,
launch, packaging, and gameplay evidence as separate gates.
