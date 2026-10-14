# ReXGlue SDK source patch

`rexglue-sdk-current.patch` is the reviewed source delta for the ReXGlue
repository at `c94f5ebdcb3c9d1a460ca48e04f9758448f8d518`. Its SHA-256 is
`763590f9ecacd28ac7babad27d301f0ddaf0db0df7862822d4cffef30d9beaea`. The
23 required submodule revisions, including the MoltenVK override, are listed
in [`sdk-submodules.txt`](sdk-submodules.txt).

The public build driver applies this patch only to a fresh SDK checkout at the
pinned base, initializes its recorded submodules, and builds the required SDK
targets in the external build root. It also uses an external dependency cache;
it does not use private SDK clones or caches. The patch is included as source
for the documented local workflow, not as a vendored SDK or prebuilt runtime.

`rexglue-sdk-local.patch` is an earlier historical checkpoint. It is not the
patch used by the current build workflow. Do not apply either patch twice to
the same checkout. For license scope and upstream notices, see
[`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) and the SDK license
file in this directory. Review the full notices at the exact pinned upstream
revisions before redistributing any locally built SDK or app.

The clean-source guest build completed and all expected compile artifacts were
verified on Apple silicon using a cold SDK/dependency setup and user-supplied
nine-XEX inputs. The game was not launched, and this is not gameplay or
personal-app packaging validation. Optional upstream SDK PPC/unit-test targets
are disabled by the workflow and are not claimed to pass.
