# Third-party notices

The SDK patch, the ReXGlue CMake integration template, and the included SDK
source fragments derive from the ReXGlue SDK, whose BSD-3-Clause license is reproduced in
`patches/rexglue-sdk-LICENSE.txt`. The patch targets ReXGlue commit
`c94f5ebdcb3c9d1a460ca48e04f9758448f8d518` and records a MoltenVK override at
`db66022459ffb663aa2b50f6b018bc2e124f5edf`.

`scripts/libarchive_api_subset.h` contains declarations only, based on
libarchive v3.7.4 headers. It retains the upstream BSD-2-Clause notice in the
file. The implementation is provided by the system libarchive library; it is
not included here.

The pinned ReXGlue dependency tree contains additional third-party projects.
They are not vendored here, and their complete notices have not been
reconciled for redistribution. Anyone preparing a build must obtain and review
the notices for the exact pinned dependencies before distributing that build.

The root `LICENSE` applies to original project-authored source and
documentation only. It does not replace the notices above or license the game,
game executables, modules, DLC, saves, replays, or game assets. No game or game
data license is provided.
