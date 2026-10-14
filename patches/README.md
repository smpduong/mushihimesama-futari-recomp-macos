# Preserving the locally fixed SDK

The SDK source tree is not vendored here. Its local changes are saved in
`rexglue-sdk-local.patch`, against:

- Repository: https://github.com/rexglue/rexglue-sdk
- Base commit: `c94f5ebdcb3c9d1a460ca48e04f9758448f8d518`
- MoltenVK override: `db66022459ffb663aa2b50f6b018bc2e124f5edf` (v1.4.2)

The patch includes dispatcher/loader fixes, code-generation changes, their SDK
tests, and the MoltenVK gitlink update. `sdk-submodules.txt` records the pinned
submodule state. The three source fragments under `tools/rexglue-sdk/` and the
three project headers are newer lifecycle integration material. They are not
part of this patch and require review against the pinned SDK before integration.

For a **fresh clone only**, from the project root:

```sh
git clone https://github.com/rexglue/rexglue-sdk tools/rexglue-sdk
git -C tools/rexglue-sdk checkout --detach c94f5ebdcb3c9d1a460ca48e04f9758448f8d518
git -C tools/rexglue-sdk apply --check ../../patches/rexglue-sdk-local.patch
git -C tools/rexglue-sdk apply --index ../../patches/rexglue-sdk-local.patch
git -C tools/rexglue-sdk submodule update --init --recursive
```

These commands restore a reference SDK tree. They have not been validated as a
complete build workflow for this source snapshot. Do not apply the patch twice
to the same checkout. On a fresh checkout, `--index` also records the patched
MoltenVK commit so submodule initialization selects the intended version.

These steps restore sources, not installed dependencies or CMake caches. Follow
the pinned SDK's build instructions to prepare the host toolchain and caches.
The source fragments and patch are an older/newer combination that has not been
validated together in a clean-machine build. This is not a complete guest build
or decompilation recipe.

The SDK license is reproduced in `rexglue-sdk-LICENSE.txt`.
