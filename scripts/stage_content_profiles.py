#!/usr/bin/env python3
"""Build verified personal ContentProfiles from supplied local STFS packages.

This is a release staging utility, not a game launcher. It invokes the same
production InstallContent harness used by install_dlc_via_runtime.py and keeps
the exact compiled harness beside a hash-bound receipt. Every output directory
must be new; source packages are opened read-only and checked before/after.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_content_install as T  # noqa: E402
import package_macos as P  # noqa: E402
import build_from_game as B  # noqa: E402
import public_output_guard as G  # noqa: E402

PACKAGES = {
    "ver101": {
        "package_id": "9893A78CA6062B935BD1F53EA43CFD546B7246DF43",
        "filename": "9893A78CA6062B935BD1F53EA43CFD546B7246DF43",
    },
    "blacklabel": {
        "package_id": "496F816DD4B53DAEFF931838B6C69831AAD7663043",
        "filename": "496F816DD4B53DAEFF931838B6C69831AAD7663043",
    },
}


def installer_environment():
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("DYLD_"):
            env.pop(key)
    # Diagnostic only: no library-search override is inherited by the installer.
    env["DYLD_PRINT_LIBRARIES"] = "1"
    return env


def runtime_load_observed(output, runtime_library):
    return str(runtime_library) in output


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_exclusive(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="new release staging directory; existing paths are refused")
    parser.add_argument("--build-dir", type=Path, required=True,
                        help="selected game build containing executable and modules")
    parser.add_argument("--game-executable", type=Path,
                        help="optional selected executable outside --build-dir")
    parser.add_argument("--runtime-dir", type=Path, required=True,
                        help="selected runtime directory containing runtime/GPU/Vulkan inputs")
    parser.add_argument("--sdk-dir", type=Path, required=True,
                        help="external SDK checkout used to build the selected runtime")
    parser.add_argument("--ver101-package", type=Path, required=True,
                        help="user-supplied Ver. 1.01 STFS package; opened read-only")
    parser.add_argument("--blacklabel-package", type=Path, required=True,
                        help="user-supplied Black Label STFS package; opened read-only")
    args = parser.parse_args()
    output = args.output_dir.expanduser()
    if args.runtime_dir.is_symlink():
        parser.error(f"runtime directory must not be a symlink: {args.runtime_dir}")
    build_dir, runtime_dir = args.build_dir.resolve(strict=True), args.runtime_dir.resolve(strict=True)
    try:
        sdk_dir = B.canonical(args.sdk_dir)
    except B.BuildError as error:
        parser.error(str(error))
    if not sdk_dir.is_dir():
        parser.error("--sdk-dir must name an existing external SDK directory")
    if sdk_dir.name != "sdk" or sdk_dir.parent == ROOT:
        parser.error("--sdk-dir must be the external build root's sdk/ directory")
    try:
        B.validate_sdk_checkout(sdk_dir.parent, require_outputs=True)
    except (B.BuildError, OSError, subprocess.SubprocessError) as error:
        parser.error(f"external SDK checkout is not the verified pinned build: {error}")
    game_executable = args.game_executable.resolve(strict=True) if args.game_executable else None
    package_args = {"ver101": args.ver101_package, "blacklabel": args.blacklabel_package}
    for path in package_args.values():
        if path.is_symlink() or not path.is_file():
            parser.error(f"missing or symlinked source package: {path}")
    packages = {name: path.resolve(strict=True) for name, path in package_args.items()}

    try:
        output = G.validate_output_path(
            output, [ROOT, sdk_dir.parent, sdk_dir, build_dir, runtime_dir,
                     *G.package_input_paths(packages.values()),
                     *([game_executable] if game_executable else [])],
        )
    except B.BuildError as error:
        parser.error(str(error))

    try:
        runtime_library = T.selected_runtime_library(runtime_dir / "librexruntimed.dylib")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    runtime_sha = sha(runtime_library)
    runtime_uuid = P.macho_uuid(runtime_library)
    input_inventory = P.collect_source_inventory(build_dir, runtime_dir, game_executable)
    launcher_sources = P.launcher_source_inventory()
    helper_source_sha = sha(ROOT / "scripts/replay_bundle.mm")
    helper_header_sha = sha(ROOT / "scripts/libarchive_api_subset.h")

    source_rows = {}
    for name, path in packages.items():
        source_rows[name] = {"path": str(path), "package_id": PACKAGES[name]["package_id"],
                             "bytes": path.stat().st_size, "sha256": sha(path)}
    output.mkdir(parents=True)
    user_data = output / "user-data"
    provenance = output / "provenance"
    provenance.mkdir()
    content_manager = sdk_dir / "src/system/xam/content_manager.cpp"
    source_sha = sha(content_manager)
    driver_sha = sha(ROOT / "scripts/install_dlc_via_runtime.py")
    harness_sha = sha(ROOT / "scripts/test_content_install.py")
    helper, counts, copied_source_sha = T.prepare_sources(provenance, sdk_dir)
    if copied_source_sha != source_sha:
        raise RuntimeError("content-manager source changed during preparation")
    if counts["fwrite("] < 3 or counts["fclose("] < 4 or counts["kernel_state_->title_id()"] < 5:
        raise RuntimeError(f"production substitution count drift: {counts}")
    helper_binary = provenance / "install_cli"
    T.build(helper_binary, helper, sdk_dir, runtime_library=runtime_library)
    helper_sha = sha(helper_binary)
    helper_deps = P.dependencies(helper_binary)
    helper_rpaths = P.load_paths(helper_binary)
    if "@rpath/librexruntimed.dylib" not in helper_deps:
        raise RuntimeError("installer helper does not link the selected REX runtime")
    if str(runtime_library.parent) not in helper_rpaths:
        raise RuntimeError("installer helper rpath does not select the supplied runtime directory")
    run_rows = []
    for name in ("ver101", "blacklabel"):
        command = [str(helper_binary), "--install", str(packages[name]), str(user_data)]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=180,
                                env=installer_environment())
        combined_output = result.stdout + "\n" + result.stderr
        runtime_loaded = runtime_load_observed(combined_output, runtime_library)
        run_rows.append({"profile": name, "argv": command, "returncode": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr,
                         "selected_runtime_path_observed": runtime_loaded})
        if result.returncode:
            raise RuntimeError(f"InstallContent failed for {name}, rc={result.returncode}; see run receipt")
        if not runtime_loaded:
            raise RuntimeError(f"InstallContent did not log the selected runtime load for {name}")

    for name, path in packages.items():
        if sha(path) != source_rows[name]["sha256"]:
            raise RuntimeError(f"source package changed during install: {path}")
    if (sha(content_manager) != source_sha or sha(helper_binary) != helper_sha
            or sha(runtime_library) != runtime_sha or P.macho_uuid(runtime_library) != runtime_uuid):
        raise RuntimeError("installer inputs changed during staging")

    user_prefix = "0000000000000000/435A07D7"
    payload_root = user_data / user_prefix / "00000002"
    header_root = user_data / user_prefix / "Headers/00000002"
    profiles = {}
    for content, selected in (("base", []), ("ver101", ["ver101"]),
                              ("blacklabel", ["blacklabel"]),
                              ("both", ["ver101", "blacklabel"])):
        profile_root = output / "ContentProfiles" / content
        files = []
        for name in selected:
            package_id = PACKAGES[name]["package_id"]
            for source in sorted((payload_root / package_id).rglob("*")):
                if source.is_symlink():
                    raise RuntimeError(f"symlink in installed payload: {source}")
                if source.is_file():
                    rel = source.relative_to(user_data).as_posix()
                    files.append({"path": rel, "bytes": source.stat().st_size, "sha256": sha(source)})
            header = header_root / f"{package_id}.header"
            if header.is_symlink() or not header.is_file():
                raise RuntimeError(f"missing installed package header: {header}")
            rel = header.relative_to(user_data).as_posix()
            files.append({"path": rel, "bytes": header.stat().st_size, "sha256": sha(header)})
        files.sort(key=lambda row: row["path"])
        if len({row["path"] for row in files}) != len(files):
            raise RuntimeError(f"conflicting paths in {content} profile")
        for row in files:
            source = user_data / row["path"]
            target = profile_root / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if target.stat().st_size != row["bytes"] or sha(target) != row["sha256"]:
                raise RuntimeError(f"profile copy mismatch: {target}")
        manifest = {"schema_version": 1, "title_id": "435A07D7", "package_type": 2,
                    "content": content,
                    "source_packages": [{"package_id": PACKAGES[name]["package_id"],
                                         "sha256": source_rows[name]["sha256"]}
                                        for name in selected],
                    "installer": {"driver": "scripts/install_dlc_via_runtime.py",
                                  "driver_sha256": driver_sha,
                                  "harness": "scripts/test_content_install.py",
                                  "harness_sha256": harness_sha,
                                  "content_manager_source_sha256": source_sha,
                                  "helper_binary_sha256": helper_sha},
                    "build_identity_sha256": P.calculate_build_identity(
                        content, input_inventory, launcher_sources, helper_source_sha,
                        helper_header_sha, files),
                    "files": files}
        profile_root.mkdir(parents=True, exist_ok=True)
        write_exclusive(profile_root / "content-profile-manifest.json", manifest)
        profiles[content] = {"root": str(profile_root), "file_count": len(files),
                             "manifest_sha256": sha(profile_root / "content-profile-manifest.json"),
                             "build_identity_sha256": manifest["build_identity_sha256"]}

    if P.collect_source_inventory(build_dir, runtime_dir, game_executable) != input_inventory:
        raise RuntimeError("selected game/runtime inputs changed during profile staging")
    if (P.launcher_source_inventory() != launcher_sources
            or sha(ROOT / "scripts/replay_bundle.mm") != helper_source_sha
            or sha(ROOT / "scripts/libarchive_api_subset.h") != helper_header_sha):
        raise RuntimeError("launcher/replay-helper source changed during profile staging")
    P.validate_content_profiles(output / "ContentProfiles", input_inventory,
                                launcher_sources, helper_source_sha, helper_header_sha)

    receipt = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
               "output_dir": str(output), "user_data_root": str(user_data),
               "build_dir": str(build_dir), "runtime_dir": str(runtime_dir),
               "game_executable": str(game_executable or (build_dir / "futari15x360")),
               "package_input_source_hashes": input_inventory,
               "replay_build_sources": {"launcher_sources": launcher_sources,
                                         "replay_helper_source_sha256": helper_source_sha,
                                         "replay_helper_header_sha256": helper_header_sha},
               "installer": {"driver": str(ROOT / "scripts/install_dlc_via_runtime.py"),
                             "driver_sha256": driver_sha,
                             "harness": str(ROOT / "scripts/test_content_install.py"),
                             "harness_sha256": harness_sha,
                             "content_manager_source": str(content_manager),
                             "content_manager_source_sha256": source_sha,
                             "substitution_counts": counts,
                             "temporary_helper_source_sha256": sha(helper),
                             "retained_helper_binary": str(helper_binary),
                             "retained_helper_binary_sha256": helper_sha,
                             "runtime_library": str(runtime_library),
                             "runtime_library_sha256": runtime_sha,
                             "runtime_library_uuid": runtime_uuid,
                             "runtime_link_dependency": "@rpath/librexruntimed.dylib",
                             "runtime_rpath": str(runtime_library.parent),
                             "runtime_load_observed_by_dyld": True},
               "source_packages": source_rows, "runs": run_rows, "profiles": profiles}
    write_exclusive(output / "install-receipt.json", receipt)
    print(json.dumps({"output_dir": str(output), "helper_sha256": helper_sha,
                      "profiles": profiles, "receipt": str(output / "install-receipt.json")}, indent=2))


if __name__ == "__main__":
    main()
