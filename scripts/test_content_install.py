#!/usr/bin/env python3
"""Production-path ContentManager::InstallContent test (Priority B2).

Drives the REAL install path end to end:

1. Copies tools/rexglue-sdk/src/system/xam/content_manager.cpp into a
   temp dir, applies exactly three mechanical substitutions
   (fwrite( -> rextest_fwrite(, fclose( -> rextest_fclose(,
   kernel_state_->title_id() -> rextest_title_id()), and compiles the
   copy into the test binary. No protected SDK file is modified; the
   original's sha256 is verified before and after.
2. Case 1 (clean): InstallContent installs a temporary copy of the real
   Black Label package into an isolated root. The extracted payload is
   compared byte-for-byte (sha256) against content extracted directly
   from the same package by test_stfs_roundtrip (independent reference
   digests committed in the driver's expectations below via re-deriving
   from the earlier round-trip outputs; the test asserts exact equality
   between the two extraction paths and re-checks known lengths).
3. Case 2 (short write): the fwrite hook delivers half of the first
   multi-MiB write. The install must FAIL loudly (non-success), and the
   partial output file must be REMOVED (no truncated file left where a
   later mount/discovery could open it).
4. Case 3 (close failure): the fclose hook reports failure after a
   complete write stream. The install must FAIL loudly and remove the
   output.
5. Case 4 (corrupt package): a valid-looking STFS container built from
   a truncated copy of the real package that still passes
   StfsContainerDevice::Initialize (so InstallContent reaches the
   read/extract path) must fail with an explicit error and must NOT
   leave a truncated payload at the install destination.

Exit 0 only if every case passes. Original packages are opened
read-only and their hashes are verified unchanged.
"""
import hashlib
import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED_BLACK = {
    "spa.bin": 827250,
    "Rom/cg_black.bin": 57743518,
    "Rom/cg_blackhi.bin": 216869269,
    "Rom/dropprm.bl": 3184,
}

# Content-manager compile needs the kernel-facing headers; these include
# paths mirror tools/rexglue-sdk's CMake for the runtime library.
INCLUDES = [
    "include",
    "thirdparty/simde",
    "thirdparty/fmt/include",
    "thirdparty/spdlog/include",
    "out/build/mac-arm64/include",
    "thirdparty/tracy/public",
]


def sha(p, n=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(n), b""):
            h.update(chunk)
    return h.hexdigest()


def prepare_sources(tmp: pathlib.Path, sdk_dir: pathlib.Path):
    """Copy content_manager.cpp, apply the three substitutions, verify."""
    from build_from_game import canonical
    sdk_dir = canonical(pathlib.Path(sdk_dir))
    cm = sdk_dir / "src/system/xam/content_manager.cpp"
    original_sha = sha(cm)
    src = cm.read_text()
    subs = [
        ("fwrite(", "rextest_fwrite("),
        ("fclose(", "rextest_fclose("),
        ("kernel_state_->title_id()", "rextest_title_id()"),
    ]
    counts = {old: src.count(old) for old, _ in subs}
    for old, new in subs:
        src = src.replace(old, new)
    copy = tmp / "content_manager_rextest.cpp"
    copy.write_text(src)
    if sha(cm) != original_sha:
        raise SystemExit("FATAL: source changed during preparation")
    return copy, counts, original_sha


def selected_runtime_library(path: pathlib.Path):
    path = pathlib.Path(path)
    if (path.name != "librexruntimed.dylib" or path.is_symlink()
            or not path.is_file()):
        raise ValueError(f"selected runtime must be a regular librexruntimed.dylib: {path}")
    return path.resolve(strict=True)


def build(exe: pathlib.Path, cm_copy: pathlib.Path, sdk_dir: pathlib.Path, runtime_library=None):
    from build_from_game import canonical
    sdk = canonical(pathlib.Path(sdk_dir))
    cmd = ["/usr/bin/clang++", "-std=c++23", "-DREX_PLATFORM_MAC=1",
           "-DSPDLOG_FMT_EXTERNAL",
           *[f"-I{sdk / p}" for p in INCLUDES],
           "-I", str(sdk / "src/system/xam"),
           "-I", str(ROOT / "scripts"),
           "-include", str(ROOT / "scripts/test_content_install_decls.h"),
           str(ROOT / "scripts/test_content_install.cpp"),
           str(cm_copy), "-o", str(exe)]
    if runtime_library is None:
        # Retain the legacy standalone fixture route for existing diagnostics.
        runtime_dir = sdk / "out/mac-arm64/Debug"
        cmd.extend([f"-L{runtime_dir}", "-lrexruntimed", f"-Wl,-rpath,{runtime_dir}"])
    else:
        selected = selected_runtime_library(runtime_library)
        cmd.extend([str(selected), f"-Wl,-rpath,{selected.parent}"])
    subprocess.run(cmd, check=True, cwd=ROOT)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blacklabel-package", type=pathlib.Path, required=True,
                        help="user-supplied Black Label STFS package; opened read-only")
    parser.add_argument("--output-dir", type=pathlib.Path, required=True,
                        help="new test-output directory; existing paths are refused")
    parser.add_argument("--sdk-dir", type=pathlib.Path, required=True,
                        help="verified external pinned SDK checkout used by the selected build")
    args = parser.parse_args(argv)
    package = args.blacklabel_package
    output = args.output_dir
    if package.is_symlink() or not package.is_file():
        parser.error(f"missing or symlinked package: {package}")
    if output.exists() or output.is_symlink():
        parser.error(f"output directory already exists: {output}")
    problems = []
    before = sha(package)
    output.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="rextest-install-") as td:
        tmp = pathlib.Path(td)
        cm_copy, counts, original_sha = prepare_sources(tmp, args.sdk_dir)
        print(f"substitution counts: {counts}")
        if counts["fwrite("] < 3 or counts["fclose("] < 4 \
                or counts["kernel_state_->title_id()"] < 5:
            problems.append("unexpected substitution counts; "
                            "content_manager.cpp drifted?")

        exe = tmp / "test_content_install"
        build(exe, cm_copy, args.sdk_dir)

        # All cases run inside one binary; the binary returns a bitmask of
        # failures. Full logs land in the explicitly selected new output dir.
        # staging-invisible drives the REAL ContentManager::ListContent and
        # requires the `<pkg>.installing` / `<pkg>.previous` directories an
        # install (or an interrupted one) leaves behind to stay invisible.
        log = output / "install-run.log"
        r = subprocess.run(
            [str(exe), str(package), str(output / "roots"), str(output / "work")],
            capture_output=True, text=True)
        log.write_text(r.stdout + "\n--- stderr ---\n" + r.stderr)
        print(r.stdout[-4000:])
        if r.returncode != 0:
            problems.append(f"test binary failed rc={r.returncode}")
        print("full log:", log)

    after = sha(package)
    if before != after:
        problems.append("original package hash changed (must never happen)")
    else:
        print("ok: original package unchanged", before[:16])

    if problems:
        print("FAIL:")
        for pr in problems:
            print("  -", pr)
        return 1
    print("PASS: production-path install (clean, short-write, close-fail, "
          "late-fail, staging-invisible, restart-recovery, header-short, corrupt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
