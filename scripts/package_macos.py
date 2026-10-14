#!/usr/bin/env python3
"""Package a local personal x360 app from explicit build/runtime/data inputs."""
import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_NAMES = ["librexruntimed.dylib", "librexgpu-xenosd.dylib", "libTracyClientd.dylib",
                 "vulkan/lib/libMoltenVK.dylib", "vulkan/lib/libvulkan.1.dylib",
                 "vulkan/share/vulkan/icd.d/MoltenVK_icd.json"]


def run(*args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_release_receipt(app, receipt_path):
    manifest_path = app / "Contents/Resources/package-manifest.json"
    rows = []
    for path in sorted(app.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Symlink in finalized app bundle: {path}")
        if path.is_file():
            rows.append({"path": path.relative_to(app).as_posix(), "bytes": path.stat().st_size,
                         "sha256": sha(path)})
    receipt = {"schema_version": 1,
               "receipt_type": "external_post_sign_bundle_identity_v1",
               "app_name": app.name,
               "package_manifest_sha256": sha(manifest_path),
               "codesign_verification": {"argv": ["/usr/bin/codesign", "--verify", "--deep", "--strict"],
                                          "returncode": 0},
               "files": rows}
    with receipt_path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write("\n")


def publish_exclusive(source, destination):
    """Atomically rename a finished bundle without replacing any destination."""
    if sys.platform != "darwin":
        raise RuntimeError("exclusive app publication requires macOS renameatx_np")
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        raise RuntimeError("renameatx_np unavailable; refusing non-atomic publication")
    renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameatx_np.restype = ctypes.c_int
    # Darwin: AT_FDCWD=-2, RENAME_EXCL=0x4.
    if renameatx_np(-2, os.fsencode(source), -2, os.fsencode(destination), 0x4) != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), str(destination))


def macho_uuid(path):
    architectures = run("/usr/bin/lipo", "-archs", path).split()
    if architectures != ["arm64"]:
        raise RuntimeError(f"Expected a thin arm64 Mach-O: {path}: {architectures}")
    result = run("/usr/bin/dwarfdump", "--uuid", path)
    match = re.search(r"UUID: ([0-9A-Fa-f-]+) \(arm64\)", result)
    if not match:
        raise RuntimeError(f"Expected one arm64 Mach-O UUID: {path}: {result}")
    return match.group(1).upper()


def load_paths(path):
    text = run("/usr/bin/otool", "-l", path)
    return re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", text)


def dependencies(path):
    lines = run("/usr/bin/otool", "-L", path).splitlines()[1:]
    deps = [line.strip().split(" (compatibility", 1)[0] for line in lines]
    return deps[1:] if path.suffix == ".dylib" else deps


def collect_source_inventory(build_dir, runtime_dir, game_executable=None):
    game_manifest = tomllib.loads((ROOT / "game-work/futari15-x360/futari15x360_manifest.toml").read_text())
    game_names = ["futari15x360"] + [f"libfutari15x360_{Path(module['guest_path']).stem}.dylib"
                                    for module in game_manifest["modules"]]
    rows = []
    for name in game_names + RUNTIME_NAMES:
        source = ((runtime_dir / name) if name in RUNTIME_NAMES else
                  (game_executable if name == "futari15x360" and game_executable else build_dir / name))
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"missing or symlinked package input: {source}")
        rows.append({"input": name, "source_sha256": sha(source)})
    return rows


def launcher_source_inventory():
    return [{"path": str(path.relative_to(ROOT)), "source_sha256": sha(path)}
            for path in (ROOT / "scripts/package_launcher.mm",
                         ROOT / "scripts/package_launcher_ui.inc")]


def calculate_build_identity(content, input_inventory, launcher_sources, helper_sha, header_sha, files):
    inputs = sorted(({"input": row["input"], "source_sha256": row["source_sha256"]}
                     for row in input_inventory), key=lambda row: row["input"])
    profile_files = sorted(({"path": row["path"], "bytes": row["bytes"], "sha256": row["sha256"]}
                            for row in files), key=lambda row: row["path"])
    identity = {"schema_version": 1, "title_id": "435A07D7", "package_type": 2,
                "content": content, "inputs": inputs,
                "launcher_sources": sorted(launcher_sources, key=lambda row: row["path"]),
                "replay_helper_sources": {"source_sha256": helper_sha,
                                          "header_sha256": header_sha},
                "files": profile_files}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_content_profiles(root, input_inventory=None, launcher_sources=None,
                              helper_sha=None, header_sha=None):
    expected = {
        "base": set(),
        "ver101": {"9893A78CA6062B935BD1F53EA43CFD546B7246DF43"},
        "blacklabel": {"496F816DD4B53DAEFF931838B6C69831AAD7663043"},
        "both": {"9893A78CA6062B935BD1F53EA43CFD546B7246DF43",
                 "496F816DD4B53DAEFF931838B6C69831AAD7663043"},
    }
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"ContentProfiles root must be a real directory: {root}")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise RuntimeError("symlink found in ContentProfiles")
    actual_children = {p.name for p in root.iterdir()}
    if actual_children != set(expected):
        raise RuntimeError(f"ContentProfiles must contain exactly {sorted(expected)}")
    reports = {}
    common = {}
    for name, package_ids in expected.items():
        profile = root / name
        manifest_path = profile / "content-profile-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError(f"missing profile manifest: {manifest_path}")
        manifest = json.loads(manifest_path.read_text())
        if (manifest.get("schema_version") != 1 or manifest.get("title_id") != "435A07D7"
                or manifest.get("package_type") != 2 or manifest.get("content") != name):
            raise RuntimeError(f"invalid profile identity: {name}")
        sources = manifest.get("source_packages")
        if not isinstance(sources, list) or {row.get("package_id") for row in sources} != package_ids:
            raise RuntimeError(f"source package identity mismatch: {name}")
        if len(sources) != len(package_ids) or any(
                not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")) for row in sources):
            raise RuntimeError(f"source package hashes malformed: {name}")
        installer = manifest.get("installer")
        if (not isinstance(installer, dict)
                or installer.get("driver") != "scripts/install_dlc_via_runtime.py"
                or installer.get("harness") != "scripts/test_content_install.py"):
            raise RuntimeError(f"unexpected installer provenance identity: {name}")
        required_installer_hashes = ("driver_sha256", "harness_sha256",
                                     "content_manager_source_sha256", "helper_binary_sha256")
        if not isinstance(installer, dict) or any(
                not re.fullmatch(r"[0-9a-f]{64}", installer.get(key, ""))
                for key in required_installer_hashes):
            raise RuntimeError(f"installer provenance incomplete: {name}")
        rows = manifest.get("files")
        if not isinstance(rows, list) or (not rows and name != "base"):
            raise RuntimeError(f"profile has no installed files: {name}")
        listed = set()
        for row in rows:
            relative = row.get("path")
            if not isinstance(relative, str):
                raise RuntimeError(f"malformed profile file row: {name}")
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or not path.parts or relative in listed:
                raise RuntimeError(f"unsafe/duplicate profile file path: {name}: {relative}")
            if not relative.startswith("0000000000000000/435A07D7/"):
                raise RuntimeError(f"file outside expected title tree: {relative}")
            parts = path.parts
            valid_package_path = (len(parts) >= 5
                                  and parts[:3] == ("0000000000000000", "435A07D7", "00000002")
                                  and parts[3] in package_ids)
            header_package = None
            if (len(parts) == 5
                    and parts[:4] == ("0000000000000000", "435A07D7", "Headers", "00000002")
                    and parts[4].endswith(".header")):
                header_package = parts[4][:-7]
            if not valid_package_path and header_package not in package_ids:
                raise RuntimeError(f"profile file path does not match selected package IDs: {relative}")
            member = profile / path
            if any(part.is_symlink() for part in (profile, *[profile.joinpath(*path.parts[:i])
                                                               for i in range(1, len(path.parts) + 1)])):
                raise RuntimeError(f"symlink in content profile: {relative}")
            if not member.is_file() or member.stat().st_size != row.get("bytes"):
                raise RuntimeError(f"missing/size-mismatched profile file: {relative}")
            digest = sha(member)
            if digest != row.get("sha256"):
                raise RuntimeError(f"profile file hash mismatch: {relative}")
            listed.add(relative)
            if relative in common and common[relative] != digest:
                raise RuntimeError(f"conflicting profile merge: {relative}")
            common[relative] = digest
        actual_files = {p.relative_to(profile).as_posix() for p in profile.rglob("*")
                        if p.is_file() and not p.is_symlink()
                        and p.name != "content-profile-manifest.json"}
        if actual_files != listed:
            raise RuntimeError(f"unmanifested or missing profile files: {name}")
        identity = manifest.get("build_identity_sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", identity or ""):
            raise RuntimeError(f"profile build identity missing: {name}")
        if input_inventory is not None:
            expected_identity = calculate_build_identity(name, input_inventory, launcher_sources,
                                                         helper_sha, header_sha, rows)
            if identity != expected_identity:
                raise RuntimeError(f"profile build identity mismatches selected package inputs: {name}")
        reports[name] = {"manifest_sha256": sha(manifest_path), "files": rows,
                         "source_packages": sources, "installer": installer,
                         "build_identity_sha256": identity}
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True,
                        help="explicit x360 build directory containing the executable and modules")
    parser.add_argument("--game-executable", type=Path,
                        help="optional selected executable outside --build-dir")
    parser.add_argument("--runtime-dir", type=Path, required=True,
                        help="explicit directory containing the runtime, GPU and Vulkan libraries")
    parser.add_argument("--game-data-dir", type=Path, required=True,
                        help="explicit local game-data directory (bundled personal package only)")
    parser.add_argument("--content-profiles-dir", type=Path, required=True,
                        help="verified ContentProfiles tree from stage_content_profiles.py")
    args = parser.parse_args()
    output = args.output.absolute()
    receipt_path = Path(str(output) + ".release-receipt.json")
    if output.suffix != ".app" or os.path.lexists(output) or os.path.lexists(receipt_path):
        raise SystemExit("Choose a new .app destination; existing apps are never overwritten.")
    output.parent.mkdir(parents=True, exist_ok=True)
    build_dir, runtime_dir, game_data, content_profiles = (p.resolve(strict=True) for p in
        (args.build_dir, args.runtime_dir, args.game_data_dir, args.content_profiles_dir))
    game_executable = args.game_executable.resolve(strict=True) if args.game_executable else build_dir / "futari15x360"
    manifest = tomllib.loads((ROOT / "game-work/futari15-x360/futari15x360_manifest.toml").read_text())
    game_names = ["futari15x360"] + [f"libfutari15x360_{Path(module['guest_path']).stem}.dylib"
                                    for module in manifest["modules"]]
    runtime_names = RUNTIME_NAMES
    required = [game_executable] + [build_dir / name for name in game_names[1:]] + \
        [runtime_dir / name for name in runtime_names]
    required += [game_data / "default.xex"]
    for item in required:
        if item.is_symlink() or not item.is_file():
            raise SystemExit(f"Missing required input: {item}")
    stage_root = Path(tempfile.mkdtemp(prefix=f".{output.stem}-stage-", dir=output.parent))
    app = stage_root / output.name
    try:
        macos = app / "Contents/MacOS"
        resources = app / "Contents/Resources"
        macos.mkdir(parents=True)
        resources.mkdir(parents=True)
        inventory = []
        binary_sources = {}
        binary_sources[game_names[0]] = game_executable
        for name in game_names[1:]:
            binary_sources[name] = build_dir / name
        for name in runtime_names:
            binary_sources[name] = runtime_dir / name

        def destination(name):
            return (resources if name.startswith("vulkan/") else macos) / name

        for name, source in binary_sources.items():
            target = destination(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            source_sha256 = sha(source)
            source_uuid = macho_uuid(source) if source.suffix == ".dylib" or name == "futari15x360" else None
            shutil.copy2(source, target)
            if sha(target) != source_sha256:
                raise RuntimeError(f"Copied input hash mismatch: {source} -> {target}")
            if sha(source) != source_sha256:
                raise RuntimeError(f"Input changed while being packaged: {source}")
            inventory.append({"input": name, "source_sha256": source_sha256, "source_uuid": source_uuid})
        helper_source = ROOT / "scripts/replay_bundle.mm"
        helper_header = ROOT / "scripts/libarchive_api_subset.h"
        helper_source_sha, helper_header_sha = sha(helper_source), sha(helper_header)
        launcher_sources = launcher_source_inventory()
        helper = macos / "FutariReplayBundle"
        run("/usr/bin/clang++", "-std=c++17", "-fobjc-arc", "-framework", "Foundation",
            "-I", ROOT / "scripts", helper_source, "-larchive", "-o", helper)
        if sha(helper_source) != helper_source_sha or sha(helper_header) != helper_header_sha:
            raise RuntimeError("Replay helper source changed while it was being compiled")
        helper_entry = {"source": str(helper_source.relative_to(ROOT)),
                        "source_sha256": helper_source_sha,
                        "header": str(helper_header.relative_to(ROOT)),
                        "header_sha256": helper_header_sha,
                        "packaged_path": str(helper.relative_to(app)),
                        "packaged_uuid": macho_uuid(helper)}
        helper_sources = {"replay_bundle_source_sha256": helper_source_sha,
                          "replay_bundle_header_sha256": helper_header_sha}
        profile_manifests = validate_content_profiles(content_profiles, inventory,
            launcher_sources, helper_source_sha, helper_header_sha)
        print("Copying local game data...", flush=True)
        for source in game_data.rglob("*"):
            if source.is_symlink():
                raise RuntimeError(f"Symlink in game data is not packageable: {source}")
        shutil.copytree(game_data, resources / "GameData", symlinks=False,
                        ignore=shutil.ignore_patterns(".DS_Store"))
        data_hashes = {}
        for source in sorted(game_data.rglob("*")):
            if source.is_file() and source.name != ".DS_Store":
                relative = source.relative_to(game_data)
                digest = sha(source)
                if digest != sha(resources / "GameData" / relative):
                    raise RuntimeError(f"Game data copy mismatch: {relative}")
                data_hashes[str(relative)] = digest
        content_root = resources / "ContentProfiles"
        shutil.copytree(content_profiles, content_root, symlinks=False)
        profile_resource_rows = {}
        for profile_name, metadata in profile_manifests.items():
            manifest_path = content_root / profile_name / "content-profile-manifest.json"
            if sha(manifest_path) != metadata["manifest_sha256"]:
                raise RuntimeError(f"content profile manifest changed during packaging: {profile_name}")
            profile_resource_rows[profile_name] = metadata
        copied_profiles = validate_content_profiles(content_root, inventory, launcher_sources,
                                                     helper_source_sha, helper_header_sha)
        if copied_profiles != profile_manifests:
            raise RuntimeError("bundled content profiles differ from the verified source profiles")
        machos = [destination(name) for name in binary_sources
                  if name.endswith(".dylib") or name == "futari15x360"]
        for path in machos:
            for rpath in load_paths(path):
                if rpath.startswith("/"):
                    run("/usr/bin/install_name_tool", "-delete_rpath", rpath, path)
            if "@loader_path" not in load_paths(path):
                run("/usr/bin/install_name_tool", "-add_rpath", "@loader_path", path)
            if path.suffix == ".dylib":
                run("/usr/bin/install_name_tool", "-id", f"@rpath/{path.name}", path)
        launcher = macos / "FutariLauncher"
        launcher_source = ROOT / "scripts/package_launcher.mm"
        launcher_source_sha = sha(launcher_source)
        run("/usr/bin/clang++", "-std=c++17", "-fobjc-arc", "-framework", "AppKit",
            launcher_source, "-o", launcher)
        if launcher_source_inventory() != launcher_sources or sha(launcher_source) != launcher_source_sha:
            raise RuntimeError("launcher source changed while it was being compiled")
        machos.extend([launcher, helper])
        for path in machos:
            for dependency in dependencies(path):
                if dependency.startswith(("/usr/lib/", "/System/Library/")):
                    continue
                if dependency.startswith("@rpath/"):
                    candidate = path.parent / dependency.removeprefix("@rpath/")
                    if not candidate.is_file():
                        raise RuntimeError(f"Unresolved bundled dependency: {path}: {dependency}")
                elif dependency.startswith(("@loader_path/", "@executable_path/")):
                    base = path.parent if dependency.startswith("@loader_path/") else macos
                    if not (base / dependency.split("/", 1)[1]).is_file():
                        raise RuntimeError(f"Unresolved relative dependency: {dependency}")
                else:
                    raise RuntimeError(f"External dependency: {path}: {dependency}")
            if any(p.startswith("/") for p in load_paths(path)):
                raise RuntimeError(f"Absolute library search path remains: {path}")
        info = {"CFBundleName": "Mushihimesama Futari", "CFBundleDisplayName": "Mushihimesama Futari",
                "CFBundleIdentifier": "com.smpduong.futari", "CFBundleExecutable": "FutariLauncher",
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.1.0",
                "CFBundleVersion": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                "NSHighResolutionCapable": True, "LSArchitecturePriority": ["arm64"]}
        min_versions = []
        for path in machos:
            load_commands = run("/usr/bin/otool", "-l", path)
            for block in re.split(r"(?=Load command \d+\n)", load_commands):
                if "cmd LC_BUILD_VERSION" in block and re.search(r"\bplatform 1\b", block):
                    match = re.search(r"\bminos ([\d.]+)", block)
                    if match:
                        min_versions.append(tuple(map(int, match.group(1).split("."))))
                elif "cmd LC_VERSION_MIN_MACOSX" in block:
                    match = re.search(r"^\s*version ([\d.]+)$", block, re.M)
                    if match:
                        min_versions.append(tuple(map(int, match.group(1).split("."))))
        if not min_versions:
            raise RuntimeError("Could not determine minimum macOS version from packaged Mach-O files")
        info["LSMinimumSystemVersion"] = ".".join(map(str, max(min_versions)))
        info_path = app / "Contents/Info.plist"
        info_path.write_bytes(plistlib.dumps(info))
        notices = resources / "Licenses"
        notices.mkdir()
        shutil.copy2(ROOT / "tools/rexglue-sdk/LICENSE", notices / "ReXGlue.txt")
        shutil.copy2(helper_header, notices / "libarchive-api-subset.h")
        for license_path in (ROOT / "tools/rexglue-sdk/thirdparty").rglob("*"):
            if license_path.is_file() and license_path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                if any(p in {".git", "out", "build"} for p in license_path.parts):
                    continue
                target = notices / "thirdparty" / license_path.relative_to(ROOT / "tools/rexglue-sdk/thirdparty")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(license_path, target)
        # Sign each Mach-O before recording package-side identities. This also
        # signs Vulkan dylibs under Resources, so their resource hashes must be
        # taken only after this loop.
        for path in machos:
            run("/usr/bin/codesign", "--force", "--sign", "-", path)
        resource_hashes = {}
        for path in sorted(resources.rglob("*")):
            if path.is_symlink():
                raise RuntimeError(f"Symlink in packaged resources: {path}")
            if path.is_file():
                resource_hashes[path.relative_to(resources).as_posix()] = sha(path)
        helper_entry["packaged_sha256"] = sha(helper)
        helper_entry["packaged_uuid"] = macho_uuid(helper)
        for entry in inventory:
            target = destination(entry["input"])
            entry.update({"packaged_path": str(target.relative_to(app)),
                          "packaged_sha256": sha(target),
                          "packaged_uuid": macho_uuid(target) if entry["source_uuid"] else None})
            if entry["source_uuid"] and entry["source_uuid"] != entry["packaged_uuid"]:
                raise RuntimeError(f"Packaged UUID differs from input for {entry['input']}")
        launcher_entry = {"identity_scheme": "source_inventory_external_post_sign_receipt_v1",
                          "source": str(launcher_source.relative_to(ROOT)),
                          "source_sha256": launcher_source_sha,
                          "packaged_path": str(launcher.relative_to(app)),
                          "packaged_uuid": macho_uuid(launcher)}
        report = {"schema_version": 3, "source_commit": run("git", "-C", ROOT, "rev-parse", "HEAD"),
                  "created_utc": datetime.now(timezone.utc).isoformat(),
                  "build": "caller-selected prebuilt game/runtime inputs; packager did not run a game build",
                  "build_dir": str(build_dir), "runtime_dir": str(runtime_dir),
                  "game_executable": str(game_executable),
                  "game_data_dir": str(game_data),
                  "minimum_macos_version": info["LSMinimumSystemVersion"],
                  "packager_source_sha256": sha(Path(__file__).resolve()),
                  "launcher": launcher_entry, "replay_helper": helper_entry,
                  "helper_sources": helper_sources, "launcher_sources": launcher_sources,
                  "inputs": inventory,
                  "game_data_sha256": data_hashes,
                  "content_profiles": profile_resource_rows,
                  "replay_build_identities": {name: row["build_identity_sha256"]
                                               for name, row in profile_resource_rows.items()},
                  "resource_sha256": resource_hashes,
                  "contains_local_game_data": True, "distribution": "local personal package",
                  "dependency_audit": "all non-system linked libraries resolved within bundle; no absolute rpaths"}
        (resources / "package-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        run("/usr/bin/codesign", "--force", "--sign", "-", app)
        run("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
        stage_receipt = Path(str(app) + ".release-receipt.json")
        write_release_receipt(app, stage_receipt)
        sys.path.insert(0, str(ROOT / "scripts"))
        import package_smoke  # noqa: E402
        static_identity = package_smoke.validate_candidate(app)
        publish_exclusive(app, output)
        publish_exclusive(stage_receipt, receipt_path)
        stage_root.rmdir()
        print(f"Packaged and statically validated: {output}\n"
              f"External receipt: {receipt_path}\n"
              f"Candidate identity: {static_identity['candidate_identity_sha256']}\n"
              f"Copied and hashed {len(data_hashes)} personal game-data files.")
    except BaseException:
        if stage_root.exists():
            shutil.rmtree(stage_root)
        raise


if __name__ == "__main__":
    main()
