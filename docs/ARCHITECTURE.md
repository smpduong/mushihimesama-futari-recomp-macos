# Architecture and source boundaries

This snapshot focuses on host-side support for a locally prepared Futari
build. The included code is organized around three areas:

- The native launcher selects a content profile and starts a caller-provided
  executable/runtime.
- The native replay helper validates and transfers explicitly selected replay
  packages. It does not include replay data.
- The Python tools validate caller-supplied inputs and assemble a personal
  macOS app from an already-prepared build and user-supplied game data.

The three `game-work/futari15-x360/src/` headers and the three files under
`tools/rexglue-sdk/` are lifecycle integration fragments. They postdate the
SDK patch checkpoint and must be reconciled with the pinned SDK source before
integration. The patch and fragments have not been validated together as a
clean rebuild in this repository.

The repository intentionally omits translated guest code, generated module
outputs, game and DLC data, compiled executables and libraries, shaders, build
caches, saves, replays, local logs, and machine-specific receipts. Host-side
tests exercise bounded contracts; they do not establish guest initialization,
rendering, input, or general gameplay support.
