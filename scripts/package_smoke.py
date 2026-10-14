#!/usr/bin/env python3
"""Relocate a local app and audit diagnostic child/library startup in isolation."""
import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import package_macos as packager  # noqa: E402
from smoke_observation import (  # noqa: E402
    ObservationError, child_log_candidates, command_is_trial_game,
    command_path_match, dyld_image_path, mapped_contains, observe_child_start_markers,
    parse_process_command, parse_process_row, path_within, read_new_log_text,
    read_log_files, read_log_snapshot,
    resolve_child_log_path, retain_child_start_marker, title_observation_for_identity,
    title_preparation_log,
    trial_path_observations, verified_path_spellings,
)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_member(app, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError(f"unsafe package member path: {relative}")
    path = app / rel
    current = app
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink in package identity path: {current}")
    if not path.is_file():
        raise ValueError(f"missing package member: {relative}")
    return path


def verify_uuid(path, wanted):
    archs = subprocess.check_output(["/usr/bin/lipo", "-archs", str(path)], text=True).split()
    if archs != ["arm64"]:
        raise ValueError(f"expected thin arm64 Mach-O at {path}, found {archs}")
    output = subprocess.check_output(["/usr/bin/dwarfdump", "--uuid", str(path)], text=True)
    found = re.findall(r"UUID: ([0-9A-Fa-f-]+) \(arm64\)", output)
    if found != [wanted]:
        raise ValueError(f"arm64 UUID mismatch for {path}: expected {wanted}; found {found}")


def verify_minimum_os(paths, expected):
    versions = []
    for path in paths:
        output = subprocess.check_output(["/usr/bin/otool", "-l", str(path)], text=True)
        for block in re.split(r"(?=Load command \d+\n)", output):
            if "cmd LC_BUILD_VERSION" in block and re.search(r"\bplatform 1\b", block):
                match = re.search(r"\bminos ([\d.]+)", block)
                if match:
                    versions.append(tuple(map(int, match.group(1).split("."))))
            elif "cmd LC_VERSION_MIN_MACOSX" in block:
                match = re.search(r"^\s*version ([\d.]+)$", block, re.M)
                if match:
                    versions.append(tuple(map(int, match.group(1).split("."))))
    if not versions:
        raise ValueError("could not read macOS minimum from packaged Mach-O files")
    actual = ".".join(map(str, max(versions)))
    if actual != expected:
        raise ValueError(f"minimum macOS version mismatch: manifest={expected}, binaries={actual}")


def validate_candidate(app):
    manifest_path = safe_member(app, "Contents/Resources/package-manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") != 3:
        raise ValueError("package manifest lacks current source/output identity schema; repackage first")
    if manifest.get("distribution") != "local personal package" or not manifest.get("contains_local_game_data"):
        raise ValueError("candidate is not declared as a personal package with local game data")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.get("packager_source_sha256", "")):
        raise ValueError("packaging tool source identity missing from manifest")
    launcher = manifest.get("launcher")
    if (not isinstance(launcher, dict)
            or launcher.get("identity_scheme") != "source_inventory_external_post_sign_receipt_v1"
            or not re.fullmatch(r"[0-9a-f]{64}", launcher.get("source_sha256", ""))
            or "packaged_sha256" in launcher):
        raise ValueError("launcher source identity missing from package manifest")
    launcher_path = safe_member(app, launcher["packaged_path"])
    verify_uuid(launcher_path, launcher.get("packaged_uuid"))
    helper = manifest.get("replay_helper")
    if (not isinstance(helper, dict)
            or not re.fullmatch(r"[0-9a-f]{64}", helper.get("source_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", helper.get("header_sha256", ""))):
        raise ValueError("replay helper source/header identity missing")
    helper_path = safe_member(app, helper.get("packaged_path", ""))
    if sha(helper_path) != helper.get("packaged_sha256"):
        raise ValueError("packaged replay helper hash differs from manifest")
    verify_uuid(helper_path, helper.get("packaged_uuid"))
    helper_sources = manifest.get("helper_sources")
    if helper_sources != {"replay_bundle_source_sha256": helper["source_sha256"],
                          "replay_bundle_header_sha256": helper["header_sha256"]}:
        raise ValueError("replay helper source inventory is inconsistent")

    binary_rows = manifest.get("inputs")
    if not isinstance(binary_rows, list) or not binary_rows:
        raise ValueError("package binary inventory missing")
    identity_rows = []
    seen = set()
    for row in binary_rows:
        if not isinstance(row, dict) or not isinstance(row.get("input"), str) or row["input"] in seen:
            raise ValueError("malformed or duplicate package input inventory row")
        if not re.fullmatch(r"[0-9a-f]{64}", row.get("source_sha256", "")):
            raise ValueError(f"source hash missing from package input: {row['input']}")
        seen.add(row["input"])
        member = safe_member(app, row.get("packaged_path", ""))
        digest = sha(member)
        if digest != row.get("packaged_sha256"):
            raise ValueError(f"packaged binary/resource hash mismatch: {row['input']}")
        if row.get("packaged_uuid"):
            if row.get("source_uuid") != row["packaged_uuid"]:
                raise ValueError(f"source/package Mach-O UUID mismatch: {row['input']}")
            verify_uuid(member, row["packaged_uuid"])
        identity_rows.append({"path": row["packaged_path"], "sha256": digest,
                              "uuid": row.get("packaged_uuid")})
    required_inputs = {
        "futari15x360", "librexruntimed.dylib", "librexgpu-xenosd.dylib", "libTracyClientd.dylib",
        "vulkan/lib/libMoltenVK.dylib", "vulkan/lib/libvulkan.1.dylib",
        "vulkan/share/vulkan/icd.d/MoltenVK_icd.json",
        *(f"libfutari15x360_{name}.dylib" for name in
          ("reco_normal", "reco_abnormal", "palm_normal", "palm_abnormal",
           "kiniro_normal", "kiniro_abnormal", "hirow_normal", "hirow_abnormal")),
    }
    if seen != required_inputs:
        raise ValueError(f"package input inventory incomplete or unexpected: missing={sorted(required_inputs-seen)} extra={sorted(seen-required_inputs)}")

    data_hashes = manifest.get("game_data_sha256")
    if not isinstance(data_hashes, dict) or not data_hashes:
        raise ValueError("game-data input inventory missing")
    data_root = safe_member(app, "Contents/Resources/GameData/default.xex").parent
    if any(path.is_symlink() for path in data_root.rglob("*")):
        raise ValueError("symlink found under packaged game data")
    actual_files = {str(path.relative_to(data_root)) for path in data_root.rglob("*")
                    if path.is_file() and not path.is_symlink() and path.name != ".DS_Store"}
    if actual_files != set(data_hashes):
        raise ValueError("packaged game-data file set differs from manifest")
    for relative, wanted in data_hashes.items():
        member = safe_member(data_root, relative)
        if sha(member) != wanted:
            raise ValueError(f"packaged game-data hash mismatch: {relative}")

    profile_root = safe_member(app, "Contents/Resources/ContentProfiles/ver101/content-profile-manifest.json").parent.parent
    launcher_sources = manifest.get("launcher_sources")
    if (not isinstance(launcher_sources, list) or len(launcher_sources) != 2
            or any(not isinstance(row, dict)
                   or not re.fullmatch(r"[0-9a-f]{64}", row.get("source_sha256", ""))
                   for row in launcher_sources)):
        raise ValueError("launcher source inventory missing or malformed")
    if {row.get("path") for row in launcher_sources} != {
            "scripts/package_launcher.mm", "scripts/package_launcher_ui.inc"}:
        raise ValueError("launcher source inventory has unexpected paths")
    if {helper.get("source"), helper.get("header")} != {
            "scripts/replay_bundle.mm", "scripts/libarchive_api_subset.h"}:
        raise ValueError("replay helper source inventory has unexpected paths")
    profile_reports = packager.validate_content_profiles(
        profile_root, binary_rows, launcher_sources, helper["source_sha256"], helper["header_sha256"])
    expected_profiles = manifest.get("content_profiles")
    if not isinstance(expected_profiles, dict) or set(expected_profiles) != set(profile_reports):
        raise ValueError("package profile manifest inventory incomplete")
    for name, report in profile_reports.items():
        recorded = expected_profiles[name]
        if (recorded.get("manifest_sha256") != report["manifest_sha256"]
                or recorded.get("files") != report["files"]
                or recorded.get("source_packages") != report["source_packages"]
                or recorded.get("installer") != report["installer"]
                or recorded.get("build_identity_sha256") != report["build_identity_sha256"]):
            raise ValueError(f"packaged content-profile identity mismatch: {name}")
    replay_build_ids = manifest.get("replay_build_identities")
    if replay_build_ids != {name: row["build_identity_sha256"]
                            for name, row in sorted(profile_reports.items())}:
        raise ValueError("package replay build-identity map is inconsistent")

    resource_hashes = manifest.get("resource_sha256")
    resource_root = safe_member(app, "Contents/Resources/GameData/default.xex").parent.parent
    if not isinstance(resource_hashes, dict) or not resource_hashes:
        raise ValueError("complete package resource inventory missing")
    if any(path.is_symlink() for path in resource_root.rglob("*")):
        raise ValueError("symlink found under packaged resources")
    actual_resource_files = {path.relative_to(resource_root).as_posix()
                             for path in resource_root.rglob("*")
                             if path.is_file() and not path.is_symlink()
                             and path.name != "package-manifest.json"}
    if actual_resource_files != set(resource_hashes):
        raise ValueError("packaged resource file set differs from manifest")
    for relative, wanted in resource_hashes.items():
        member = safe_member(resource_root, relative)
        if sha(member) != wanted:
            raise ValueError(f"packaged resource hash mismatch: {relative}")

    info_path = safe_member(app, "Contents/Info.plist")
    info = plistlib.loads(info_path.read_bytes())
    if info.get("CFBundleIdentifier") != "com.smpduong.futari" or info.get("CFBundleExecutable") != "FutariLauncher":
        raise ValueError("bundle identity does not match the package manifest contract")
    minimum_os = manifest.get("minimum_macos_version")
    if info.get("LSMinimumSystemVersion") != minimum_os:
        raise ValueError("Info.plist minimum macOS version differs from package manifest")
    macho_paths = [safe_member(app, row["packaged_path"]) for row in binary_rows
                   if row.get("packaged_uuid")] + [launcher_path, helper_path]
    verify_minimum_os(macho_paths, minimum_os)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    receipt_path = Path(str(app) + ".release-receipt.json")
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("external post-sign candidate receipt is missing")
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    expected_signature_check = {"argv": ["/usr/bin/codesign", "--verify", "--deep", "--strict"],
                                "returncode": 0}
    if (receipt.get("schema_version") != 1
            or receipt.get("receipt_type") != "external_post_sign_bundle_identity_v1"
            or receipt.get("app_name") != app.name
            or receipt.get("package_manifest_sha256") != manifest_hash
            or receipt.get("codesign_verification") != expected_signature_check):
        raise ValueError("external post-sign candidate receipt identity is invalid")
    receipt_rows = receipt.get("files")
    if not isinstance(receipt_rows, list):
        raise ValueError("external post-sign candidate receipt has no file inventory")
    actual_rows = []
    for path in sorted(app.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in post-sign app bundle")
        if path.is_file():
            actual_rows.append({"path": path.relative_to(app).as_posix(),
                                "bytes": path.stat().st_size, "sha256": sha(path)})
    if actual_rows != receipt_rows:
        raise ValueError("post-sign app file inventory differs from external candidate receipt")
    code_resources = safe_member(app, "Contents/_CodeSignature/CodeResources")
    identity = {"manifest_sha256": manifest_hash, "info_plist_sha256": sha(info_path),
                "code_resources_sha256": sha(code_resources),
                "launcher": {"path": launcher["packaged_path"], "sha256": sha(launcher_path),
                             "uuid": launcher["packaged_uuid"]},
                "replay_helper": {"path": helper["packaged_path"],
                                  "sha256": sha(helper_path), "uuid": helper["packaged_uuid"],
                                  "source_sha256": helper["source_sha256"],
                                  "header_sha256": helper["header_sha256"]},
                "binary_inventory": sorted(identity_rows, key=lambda item: item["path"]),
                "content_profiles": {name: {"manifest_sha256": row["manifest_sha256"],
                                             "files": row["files"],
                                             "build_identity_sha256": row["build_identity_sha256"]}
                                     for name, row in sorted(profile_reports.items())},
                "replay_build_identities": replay_build_ids,
                "resource_sha256": resource_hashes,
                "external_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "game_data_count": len(data_hashes), "source_commit": manifest.get("source_commit")}
    candidate_sha = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"package_manifest_sha256": manifest_hash, "candidate_identity_sha256": candidate_sha,
            "source_commit": manifest.get("source_commit"), "binary_count": len(binary_rows),
            "game_data_count": len(data_hashes), "distribution": manifest["distribution"],
            "replay_build_identities": replay_build_ids}


def signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def launcher_child_ready(survived, pid, owned, content, expected_content, runtime,
                         graphics, guest_executable, title_preparation, child_alive,
                         child_live_seconds, required_live_seconds, fatal, external):
    """Bounded launcher/child readiness only; intentionally not guest boot."""
    return bool(survived and pid is not None and owned and content in expected_content
                and runtime and graphics and guest_executable and title_preparation and child_alive
                and child_live_seconds >= required_live_seconds
                and not fatal and not external)


def smoke_attempt_passed(readiness_passed, cleanup_verified_gone,
                         process_inspection_error, process_control_error,
                         log_observation_error,
                         mapping_checks, mapping_queries_succeeded):
    """Require complete evidence and verified owned-child cleanup for PASS."""
    return bool(readiness_passed and cleanup_verified_gone
                and process_inspection_error is None and process_control_error is None
                and log_observation_error is None
                and mapping_checks > 0 and mapping_queries_succeeded == mapping_checks)


def smoke_command(app, profile_path, isolation):
    """Keep filesystem-sandbox policy opt-in; mapped mode launches the bundle executable directly."""
    launcher = str(app / "Contents/MacOS/FutariLauncher")
    if isolation == "sandboxed":
        return ["/usr/bin/sandbox-exec", "-f", str(profile_path), launcher]
    if isolation == "mapped-libraries":
        return [launcher]
    raise ValueError(f"unsupported smoke isolation mode: {isolation}")


def isolation_report(isolation, audited_paths):
    isolated = isolation == "sandboxed"
    return {
        "isolation_mode": isolation,
        "filesystem_isolation_enforced": isolated,
        "denied_read_paths": list(audited_paths) if isolated else [],
        "audited_external_paths": list(audited_paths),
    }


def canonical_trial_paths(trial, bundle_name):
    """Resolve macOS temp aliases (notably /var -> /private/var) before deriving paths."""
    trial = Path(trial).resolve(strict=True)
    if not trial.is_dir():
        raise ValueError("smoke trial path is not a directory")
    return trial, trial / bundle_name, trial / "UserData"


def title_evidence_matches_child(child_identity, child_alive, owned, marker,
                                 command_log, user_data):
    """Only bind title evidence to the exact live child and its own log target."""
    if not child_identity or not child_alive or not owned or not command_log:
        return False
    if marker is None:
        return True
    if marker.get("pid") != child_identity.get("pid"):
        return False
    marker_paths = child_log_candidates(marker.get("log", ""), user_data)
    command_paths = child_log_candidates(command_log, user_data)
    return bool(marker_paths and command_paths and
                os.path.realpath(marker_paths[0]) == os.path.realpath(command_paths[0]))


def find_owned_trial_child(ps_output, launcher_pid, trial, app):
    """Match only an exact trial game executable whose current PPID is our launcher."""
    matches = []
    for line in ps_output.splitlines():
        row = parse_process_row(line)
        if (row and row["parent_pid"] == launcher_pid
                and command_is_trial_game(row["command"], trial, app)):
            matches.append(row)
    return matches[0] if len(matches) == 1 else None


class ProcessInspectionError(RuntimeError):
    pass


def process_table_snapshot():
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,pgid=,lstart=,command="],
                                text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessInspectionError(f"process table inspection outcome unknown: {exc}") from exc
    if result.returncode != 0 or result.stderr:
        raise ProcessInspectionError(
            f"process table inspection failed (status={result.returncode}): {result.stderr.strip()}")
    return result.stdout


def snapshot_process(pid):
    try:
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o",
                                 "pid=,ppid=,pgid=,lstart=,command="],
                                text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessInspectionError(f"process {pid} inspection outcome unknown: {exc}") from exc
    if result.returncode == 0:
        row = parse_process_row(result.stdout)
        if row is None:
            raise ProcessInspectionError(f"process {pid} returned an unparsable identity row")
        return row
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    raise ProcessInspectionError(
        f"process {pid} inspection failed (status={result.returncode}): {result.stderr.strip()}")


def terminate_verified_trial_child(identity, launcher_pid, trial, app, wait_seconds=4):
    """TERM/KILL only a child PID revalidated by start time and unique trial argv."""
    if not identity:
        return {"attempted": False, "status": "no_verified_child_identity"}
    if identity.get("initial_parent_pid", identity.get("parent_pid")) != launcher_pid:
        return {"attempted": False, "status": "initial_parent_not_launcher_refused",
                "pid": identity.get("pid")}
    pid = identity["pid"]

    def still_same_owned_process():
        try:
            current = snapshot_process(pid)
        except ProcessInspectionError as exc:
            return {"inspection_error": str(exc)}
        if not current:
            return None
        if (current["pid"] != identity["pid"]
                or current["start_time"] != identity["start_time"]
                or current["command"] != identity["command"]
                or current["process_group"] != identity["process_group"]
                or not command_is_trial_game(current["command"], trial, app)
                or current["parent_pid"] not in (launcher_pid, 1)):
            return False
        return current

    current = still_same_owned_process()
    if isinstance(current, dict) and "inspection_error" in current:
        return {"attempted": False, "status": "could_not_verify_initial_identity",
                "pid": pid, "error": current["inspection_error"]}
    if current is None:
        return {"attempted": False, "status": "already_exited", "pid": pid}
    if current is False:
        return {"attempted": False, "status": "identity_changed_refused", "pid": pid}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"attempted": True, "status": "already_exited_at_SIGTERM", "signal": "SIGTERM", "pid": pid}
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        current = still_same_owned_process()
        if isinstance(current, dict) and "inspection_error" in current:
            return {"attempted": True, "status": "could_not_verify_gone_after_SIGTERM",
                    "signal": "SIGTERM", "pid": pid, "error": current["inspection_error"]}
        if current is None:
            return {"attempted": True, "status": "terminated", "signal": "SIGTERM", "pid": pid}
        if current is False:
            return {"attempted": True, "status": "identity_changed_no_escalation", "signal": "SIGTERM", "pid": pid}
        time.sleep(0.1)
    current = still_same_owned_process()
    if isinstance(current, dict) and "inspection_error" in current:
        return {"attempted": True, "status": "could_not_verify_gone_after_SIGTERM",
                "signal": "SIGTERM", "pid": pid, "error": current["inspection_error"]}
    if current is None:
        return {"attempted": True, "status": "terminated", "signal": "SIGTERM", "pid": pid}
    if current is False:
        return {"attempted": True, "status": "identity_changed_no_escalation", "signal": "SIGTERM", "pid": pid}
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        current = still_same_owned_process()
        if isinstance(current, dict) and "inspection_error" in current:
            return {"attempted": True, "status": "could_not_verify_gone_after_SIGKILL",
                    "signal": "SIGKILL", "pid": pid, "error": current["inspection_error"]}
        return {"attempted": True,
                "status": "terminated" if current is None else "identity_changed_at_SIGKILL",
                "signal": "SIGKILL", "pid": pid}
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = still_same_owned_process()
        if isinstance(current, dict) and "inspection_error" in current:
            return {"attempted": True, "status": "could_not_verify_gone_after_SIGKILL",
                    "signal": "SIGKILL", "pid": pid, "error": current["inspection_error"]}
        if current is None:
            return {"attempted": True, "status": "terminated", "signal": "SIGKILL", "pid": pid}
        if current is False:
            return {"attempted": True, "status": "identity_changed_after_SIGKILL", "signal": "SIGKILL", "pid": pid}
        time.sleep(0.1)
    current = still_same_owned_process()
    if isinstance(current, dict) and "inspection_error" in current:
        return {"attempted": True, "status": "could_not_verify_gone_after_SIGKILL",
                "signal": "SIGKILL", "pid": pid, "error": current["inspection_error"]}
    if current is None:
        return {"attempted": True, "status": "terminated", "signal": "SIGKILL", "pid": pid}
    return {"attempted": True, "status": "still_running", "signal": "SIGKILL", "pid": pid}


def discover_trial_child(launcher_pid, trial, app):
    """Capture exact child identity while its launcher is still alive."""
    row = find_owned_trial_child(process_table_snapshot(), launcher_pid, trial, app)
    if row is None:
        return None
    row["initial_parent_pid"] = row["parent_pid"]
    try:
        mapped = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(row["pid"]), "-d", "txt", "-Fn"],
                                text=True, capture_output=True, timeout=10)
        row["mapped_files"] = mapped.stdout if mapped.returncode == 0 else ""
        row["mapping_query_succeeded"] = mapped.returncode == 0
        row["mapping_query_error"] = (None if mapped.returncode == 0 else
                                        f"lsof exited with {mapped.returncode}: {mapped.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        row["mapped_files"] = ""
        row["mapping_query_succeeded"] = False
        row["mapping_query_error"] = f"lsof outcome unknown: {exc}"
    log_match = re.search(r"--log_file=(.+?\.log)(?:\s|$)", row["command"])
    row["child_log_argument"] = log_match.group(1) if log_match else None
    try:
        row["resolved_log"] = resolve_child_log_path(row["child_log_argument"], trial / "UserData") \
            if row["child_log_argument"] else None
    except ObservationError as exc:
        row["resolved_log"] = None
        row["log_observation_error"] = str(exc)
    return row


def refresh_trial_child(identity, launcher_pid, trial, app):
    """Recheck a captured PID/start-time/executable and refresh mapped files."""
    if identity is None:
        return discover_trial_child(launcher_pid, trial, app)
    current = snapshot_process(identity["pid"])
    if (current is None or current["start_time"] != identity["start_time"]
            or current["command"] != identity["command"]
            or current["process_group"] != identity["process_group"]
            or current["parent_pid"] not in (launcher_pid, 1)
            or not command_is_trial_game(current["command"], trial, app)):
        return None
    refreshed = dict(identity)
    refreshed.update(current)
    refreshed["initial_parent_pid"] = identity.get("initial_parent_pid", identity["parent_pid"])
    try:
        mapped = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(current["pid"]), "-d", "txt", "-Fn"],
                                text=True, capture_output=True, timeout=10)
        refreshed["mapped_files"] = mapped.stdout if mapped.returncode == 0 else ""
        refreshed["mapping_query_succeeded"] = mapped.returncode == 0
        refreshed["mapping_query_error"] = (None if mapped.returncode == 0 else
                                             f"lsof exited with {mapped.returncode}: {mapped.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        refreshed["mapped_files"] = ""
        refreshed["mapping_query_succeeded"] = False
        refreshed["mapping_query_error"] = f"lsof outcome unknown: {exc}"
    log_match = re.search(r"--log_file=(.+?\.log)(?:\s|$)", current["command"])
    refreshed["child_log_argument"] = log_match.group(1) if log_match else None
    try:
        refreshed["resolved_log"] = resolve_child_log_path(refreshed["child_log_argument"],
                                                             trial / "UserData") \
            if refreshed["child_log_argument"] else None
        refreshed.pop("log_observation_error", None)
    except ObservationError as exc:
        refreshed["resolved_log"] = None
        refreshed["log_observation_error"] = str(exc)
    return refreshed


def copy_validated_relocation(source, relocated, expected_identity):
    """Copy a signed candidate and prove the moved bundle/receipt is identical."""
    if source.name != relocated.name:
        raise ValueError("relocated app must retain its bundle name for receipt validation")
    shutil.copytree(source, relocated)
    source_receipt = Path(str(source) + ".release-receipt.json")
    relocated_receipt = Path(str(relocated) + ".release-receipt.json")
    if source_receipt.is_symlink() or not source_receipt.is_file():
        raise ValueError("source candidate receipt is missing or symlinked")
    shutil.copy2(source_receipt, relocated_receipt)
    relocated_identity = validate_candidate(relocated)
    if relocated_identity != expected_identity:
        raise ValueError("relocated app identity differs from the validated source candidate")
    return relocated_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--seconds", type=int, default=45)
    parser.add_argument("--startup-deadline-seconds", type=int, default=15,
                        help="stop early if the launcher has not emitted one child-start marker")
    parser.add_argument("--isolation", choices=("sandboxed", "mapped-libraries"),
                        default="sandboxed",
                        help="sandboxed blocks source paths; mapped-libraries only audits loaded paths")
    args = parser.parse_args()
    if not 10 <= args.seconds <= 300:
        parser.error("seconds must be between 10 and 300")
    if not 1 <= args.startup_deadline_seconds <= 60:
        parser.error("startup deadline must be between 1 and 60 seconds")
    if args.app.is_symlink():
        parser.error("candidate app path must not be a symlink")
    source = args.app.resolve(strict=True)
    if not (source / "Contents/MacOS/FutariLauncher").is_file():
        parser.error("app does not contain FutariLauncher")
    try:
        candidate_identity = validate_candidate(source)
    except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError,
            subprocess.CalledProcessError) as exc:
        parser.error(f"candidate identity validation failed: {exc}")
    receipt_root = ROOT / "out/release-candidates"
    receipt_root.mkdir(parents=True, exist_ok=True)
    checkpoint = Path(tempfile.mkdtemp(
        prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-smoke-"), dir=receipt_root)).resolve(strict=True)
    trial, app, user_data = canonical_trial_paths(
        tempfile.mkdtemp(prefix="futari-package-relocation-", dir="/private/tmp"), source.name)
    copy_validated_relocation(source, app, candidate_identity)
    # The strict mode blocks the source app/project/SDK/tool locations.
    # Mapped-libraries mode deliberately does not claim that source reads were
    # prevented; it only reports whether loaded paths point outside the bundle.
    audited_paths = [str(ROOT.parent), str(ROOT / "tools/rexglue-sdk"),
                     "/opt/homebrew", "/Applications/CMake.app"]
    profile_path = trial / "isolation.sb"
    if args.isolation == "sandboxed":
        profile = "(version 1)\n(allow default)\n" + "\n".join(
            f"(deny file-read* (subpath {json.dumps(path)}))" for path in audited_paths)
        profile_path.write_text(profile)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("DYLD_", "REX", "VK_", "VULKAN", "CPM_"))}
    env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin",
               FUTARI_PACKAGE_USER_DATA=str(user_data),
               FUTARI_DIAGNOSTIC_AUTOSTART="1",
               DYLD_PRINT_LIBRARIES="1")
    command = smoke_command(app, profile_path, args.isolation)
    survived = False
    child_pid = None
    child_owned = None
    child_content = None
    child_log_from_marker = None
    child_log_path = None
    child_process_group = None
    child_command = None
    child_in_owned_group = None
    mapped_files = ""
    child_identity = None
    child_alive = False
    retained_marker = None
    marker_conflict = False
    title_prepare_seen = False
    title_prepare_log = None
    title_prepare_line = None
    title_prepare_log_sha256 = None
    title_prepare_elapsed = None
    title_prepare_process_identity = None
    title_prepare_snapshot = None
    live_log_offsets = {}
    live_fatal_lines = []
    mapping_checks = 0
    mapping_queries_succeeded = 0
    process_scan_attempted = False
    child_first_seen_elapsed = None
    child_live_elapsed = 0.0
    last_child_live_monotonic = None
    process_inspection_error = None
    process_control_error = None
    log_observation_error = None
    cleanup_report = {"attempted": False, "status": "not_needed"}
    early_stop_reason = None
    launch_started = time.monotonic()
    effective_startup_deadline = min(args.startup_deadline_seconds, args.seconds)
    with (trial / "bootstrap.log").open("w") as output:
        process = subprocess.Popen(command, cwd=trial, env=env,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        print(f"Relocated smoke running: PID {process.pid}; evidence: {trial}", flush=True)
        try:
            while True:
                elapsed = time.monotonic() - launch_started
                logs_dir = user_data / "Logs"
                try:
                    retained_marker, marker_conflict = retain_child_start_marker(
                        retained_marker, observe_child_start_markers(logs_dir), marker_conflict)
                except ObservationError as exc:
                    log_observation_error = str(exc)
                    early_stop_reason = "log_observation_failed"
                    break
                if retained_marker and child_pid is None:
                    child_pid = retained_marker["pid"]
                    child_content = retained_marker["content"]
                    child_log_from_marker = retained_marker["log"]
                    try:
                        child_log_path = resolve_child_log_path(child_log_from_marker, user_data)
                    except ObservationError as exc:
                        log_observation_error = str(exc)
                        early_stop_reason = "log_observation_failed"
                        break
                process_scan_attempted = True
                try:
                    discovered = (refresh_trial_child(child_identity, process.pid, trial, app)
                                  if child_identity is not None else
                                  discover_trial_child(process.pid, trial, app))
                except ProcessInspectionError as exc:
                    process_inspection_error = str(exc)
                    early_stop_reason = "process_identity_inspection_failed"
                    break
                except ObservationError as exc:
                    log_observation_error = str(exc)
                    early_stop_reason = "log_observation_failed"
                    break
                if discovered is not None:
                    if child_identity is None:
                        child_first_seen_elapsed = elapsed
                        child_owned = discovered["parent_pid"] == process.pid
                    child_identity = discovered
                    child_alive = True
                    now = time.monotonic()
                    if last_child_live_monotonic is not None:
                        child_live_elapsed += max(0.0, now - last_child_live_monotonic)
                    last_child_live_monotonic = now
                    # A retained marker is advisory; process identity and the
                    # exact trial paths are the ownership evidence.
                    child_pid = child_identity["pid"]
                    child_process_group = child_identity["process_group"]
                    child_in_owned_group = child_process_group == process.pid
                    child_command = child_identity["command"]
                    mapped_files = child_identity["mapped_files"]
                    if child_identity.get("log_observation_error"):
                        log_observation_error = child_identity["log_observation_error"]
                        early_stop_reason = "log_observation_failed"
                        break
                    mapping_checks += 1
                    mapping_queries_succeeded += int(child_identity.get("mapping_query_succeeded", False))
                    if child_identity["resolved_log"] is not None:
                        child_log_path = child_identity["resolved_log"]
                    process_content = next((name for name in candidate_identity["replay_build_identities"]
                                            if command_path_match(child_command,
                                                trial / "UserData/Profiles" / name,
                                                "--user_data_root=") is not None), None)
                    if (retained_marker is not None and retained_marker["pid"] == child_pid
                            and retained_marker["content"] != process_content):
                        marker_conflict = True
                    child_content = process_content
                elif child_identity is not None:
                    child_alive = False
                    last_child_live_monotonic = None
                if child_pid is not None and child_identity is not None and child_owned and child_log_path:
                    try:
                        log_text = read_log_snapshot(child_log_path) or ""
                    except ObservationError as exc:
                        log_observation_error = str(exc)
                        early_stop_reason = "log_observation_failed"
                        break
                    if re.search(rf"FUTARI_DIAGNOSTIC_CHILD_EXIT pid={child_pid} ", log_text):
                        early_stop_reason = "game_child_exit_marker_before_smoke_deadline"
                        break
                candidate_log_path = (child_identity.get("child_log_argument")
                                      if child_identity is not None else None)
                try:
                    marker_matches_child = title_evidence_matches_child(
                        child_identity, child_alive, child_owned, retained_marker,
                        candidate_log_path, user_data)
                except ObservationError as exc:
                    log_observation_error = str(exc)
                    early_stop_reason = "log_observation_failed"
                    break
                if (retained_marker is not None and child_identity is not None
                        and child_alive and child_owned and not marker_matches_child):
                    marker_conflict = True
                evidence_identity = ((child_identity["pid"], child_identity["start_time"],
                                      os.path.realpath(app / "Contents/MacOS/futari15x360"))
                                     if child_identity else None)
                try:
                    title_observation = (title_observation_for_identity(
                        evidence_identity, title_prepare_process_identity, title_prepare_snapshot,
                        candidate_log_path, user_data)
                        if candidate_log_path and marker_matches_child else None)
                except ObservationError as exc:
                    log_observation_error = str(exc)
                    early_stop_reason = "log_observation_failed"
                    break
                found_title_log = title_observation[0] if title_observation else None
                if found_title_log is not None and evidence_identity is not None:
                    if title_prepare_process_identity in (None, evidence_identity):
                        if not title_prepare_seen and title_observation is not None:
                            _path, contents, observed_sha256 = title_observation
                            title_prepare_line = next((line for line in contents.splitlines()
                                if "Initializing shader storage for title 435A07D7..." in line), None)
                            title_prepare_log_sha256 = observed_sha256
                            title_prepare_elapsed = round(elapsed, 2)
                        title_prepare_seen = True
                        title_prepare_process_identity = evidence_identity
                        title_prepare_snapshot = title_observation
                        title_prepare_log = str(found_title_log)
                        child_log_path = found_title_log
                try:
                    new_log_text = read_new_log_text(user_data, live_log_offsets)
                except ObservationError as exc:
                    log_observation_error = str(exc)
                    early_stop_reason = "log_observation_failed"
                    break
                current_fatals = re.findall(r".*(?:\[fatal\]|invalid or unregistered function|assertion failed|"
                    r"Library not loaded|Failed to load GPU|Unhandled exception).*", new_log_text, re.I)
                if current_fatals:
                    live_fatal_lines.extend(current_fatals)
                    early_stop_reason = "fatal_runtime_line_observed"
                    break
                if (child_alive and child_identity is not None and child_owned and child_content in
                        candidate_identity["replay_build_identities"] and title_prepare_seen):
                    if child_live_elapsed >= args.seconds:
                        survived = True
                        break
                if elapsed >= effective_startup_deadline and not (
                        child_alive and child_identity is not None and child_owned and title_prepare_seen):
                    early_stop_reason = ("no_verified_trial_child_before_startup_deadline"
                                         if child_identity is None else
                                         "trial_child_missing_profile_or_title_preparation")
                    break
                if process.poll() is not None:
                    early_stop_reason = "launcher_exited_before_smoke_deadline"
                    break
                if not child_alive and child_identity is not None:
                    early_stop_reason = "verified_child_disappeared_before_smoke_deadline"
                    break
                time.sleep(min(0.25, max(0.01, args.seconds - elapsed)))
        finally:
            # Capture a direct child before the launcher exits; NSTask can put
            # that child in a separate process group from the launcher.
            if child_identity is None:
                try:
                    child_identity = discover_trial_child(process.pid, trial, app)
                except ProcessInspectionError as exc:
                    process_inspection_error = str(exc)
                except ObservationError as exc:
                    log_observation_error = str(exc)
            if child_identity is not None and child_alive:
                try:
                    final_child = snapshot_process(child_identity["pid"])
                except ProcessInspectionError as exc:
                    process_inspection_error = str(exc)
                    final_child = None
                still_same = bool(final_child and
                    final_child["start_time"] == child_identity["start_time"] and
                    final_child["command"] == child_identity["command"] and
                    final_child["process_group"] == child_identity["process_group"] and
                    final_child["parent_pid"] in (process.pid, 1))
                if still_same and last_child_live_monotonic is not None:
                    child_live_elapsed += max(0.0, time.monotonic() - last_child_live_monotonic)
                else:
                    child_alive = False
            if child_identity is not None:
                try:
                    cleanup_report = terminate_verified_trial_child(
                        child_identity, process.pid, trial, app)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_report = {"attempted": True, "status": "cleanup_error",
                                      "error": str(exc), "pid": child_identity.get("pid")}
            if process.poll() is None:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    if process.poll() is None:
                        try:
                            signal_group(process.pid, sig)
                        except OSError as exc:
                            process_control_error = process_control_error or str(exc)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if sig == signal.SIGKILL:
                            process_control_error = process_control_error or \
                                "owned launcher remained after SIGKILL wait"
                    except OSError as exc:
                        process_control_error = process_control_error or str(exc)
                    if process.poll() is not None:
                        break
            if child_identity is not None and cleanup_report.get("status") not in ("terminated", "already_exited"):
                try:
                    cleanup_report["post_launcher_cleanup"] = terminate_verified_trial_child(
                        child_identity, process.pid, trial, app)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_report["post_launcher_cleanup"] = {
                        "attempted": True, "status": "cleanup_error", "error": str(exc),
                        "pid": child_identity.get("pid")}
    try:
        log_records = read_log_files(user_data / "Logs")
    except ObservationError as exc:
        log_observation_error = log_observation_error or str(exc)
        early_stop_reason = early_stop_reason or "log_observation_failed"
        log_records = []
    logs = [path for path, _contents in log_records]
    observed_seconds = round(time.monotonic() - launch_started, 2)
    text = "\n".join(payload.decode("utf-8", errors="replace")
                       for _path, payload in log_records)
    fatal = re.findall(r".*(?:\[fatal\]|invalid or unregistered function|assertion failed|"
                       r"Library not loaded|Failed to load GPU|Unhandled exception).*", text, re.I)
    fatal.extend(line for line in live_fatal_lines if line not in fatal)
    # Map the exact child process, not the long-lived launcher, to avoid
    # treating a responsive control window as a running guest.
    graphics = all(mapped_contains(mapped_files, app / "Contents/Resources/vulkan/lib" / name)
                   for name in ("libMoltenVK.dylib", "libvulkan.1.dylib"))
    runtime = mapped_contains(mapped_files, app / "Contents/MacOS/librexruntimed.dylib")
    guest_executable = mapped_contains(mapped_files, app / "Contents/MacOS/futari15x360")
    loaded_images = [path for line in text.splitlines()
                     if (path := dyld_image_path(line)) is not None]
    mapped_images = [line[1:] for line in mapped_files.splitlines() if line.startswith("n")]
    external = ([{"source": "dyld", "path": path} for path in loaded_images
                 if any(path_within(path, root) for root in audited_paths)]
                + [{"source": "lsof", "path": path} for path in mapped_images
                   if any(path_within(path, root) for root in audited_paths)])
    final_title_identity = ((child_identity["pid"], child_identity["start_time"],
                             os.path.realpath(app / "Contents/MacOS/futari15x360"))
                            if child_identity else None)
    title_prepare = (title_prepare_seen and
                     title_prepare_process_identity == final_title_identity)
    exit_markers = re.findall(r"FUTARI_DIAGNOSTIC_CHILD_EXIT pid=(\d+) reason=(exit|signal) status=(\d+)", text)
    child_exit_marker = next(({"pid": int(pid), "reason": reason, "status": int(status)}
                              for pid, reason, status in exit_markers if int(pid) == child_pid), None)
    readiness_passed = launcher_child_ready(
        survived, child_pid, child_owned, child_content, candidate_identity["replay_build_identities"],
        runtime, graphics, guest_executable, title_prepare and not marker_conflict, child_alive,
        child_live_elapsed, args.seconds, fatal, external)
    cleanup_verified_gone = cleanup_report.get("status") in (
        "terminated", "already_exited", "already_exited_at_SIGTERM")
    launcher_child_passed = smoke_attempt_passed(
        readiness_passed, cleanup_verified_gone, process_inspection_error,
        process_control_error, log_observation_error, mapping_checks,
        mapping_queries_succeeded)
    report = {"launcher_child_smoke_passed": launcher_child_passed,
              "game_boot_verified": False,
              "acceptance_level": "launcher child start + child library map + title preparation only",
              "survived_seconds": args.seconds if survived else observed_seconds,
              "launcher_elapsed_seconds": observed_seconds,
              "child_verified_elapsed_seconds": round(child_live_elapsed, 2),
              "child_first_seen_elapsed_seconds": (round(child_first_seen_elapsed, 2)
                                                     if child_first_seen_elapsed is not None else None),
              "smoke_deadline_seconds": args.seconds,
              "startup_deadline_seconds": effective_startup_deadline,
              "early_stop_reason": early_stop_reason,
              "process_inspection_error": process_inspection_error,
              "process_control_error": process_control_error,
              "log_observation_error": log_observation_error,
              "child_cleanup_verified_gone": cleanup_verified_gone,
              "returncode_after_test_stop": process.returncode,
              **isolation_report(args.isolation, audited_paths),
              "relocated_app": str(app), "source_app": str(source),
              "child_pid": child_pid, "child_content": child_content,
              "child_log_from_start_marker": child_log_from_marker,
              "child_owned_by_launcher": child_owned,
              "child_in_owned_process_group": child_in_owned_group,
              "child_process_group": child_process_group,
              "child_command": child_command,
              "child_path_observations": (trial_path_observations(child_command, trial, app)
                                           if child_command else None),
              "child_log": str(child_log_path) if child_log_path else None,
              "title_preparation_log": title_prepare_log,
              "title_preparation_line": title_prepare_line,
              "title_preparation_log_sha256_at_observation": title_prepare_log_sha256,
              "title_preparation_first_seen_elapsed_seconds": title_prepare_elapsed,
              "title_preparation_process_identity": (dict(pid=title_prepare_process_identity[0],
                    start_time=title_prepare_process_identity[1], executable=title_prepare_process_identity[2])
                    if title_prepare_process_identity else None),
              "liveness_mapping_checks": mapping_checks,
              "liveness_mapping_query_successes": mapping_queries_succeeded,
              "child_exit_marker": child_exit_marker,
              "child_process_identity": ({key: child_identity.get(key) for key in
                    ("pid", "parent_pid", "process_group", "start_time", "command",
                     "mapping_query_succeeded", "mapping_query_error")} if child_identity else None),
              "process_scan_attempted": process_scan_attempted,
            "child_start_marker_conflict": marker_conflict,
              "child_alive_at_stop": child_alive,
              "owned_child_cleanup": cleanup_report,
              "loaded_bundled_runtime_in_child": runtime,
              "loaded_bundled_vulkan_and_moltenvk_in_child": graphics,
              "guest_executable_mapped_in_child": guest_executable,
              "title_preparation_marker": title_prepare,
              "title_preparation_identity_consistent": not marker_conflict,
              "guest_init_proven": False,
              "fatal_lines": fatal, "external_library_lines": external,
              "logs": [str(path) for path in logs],
              "candidate_identity": candidate_identity,
              "limitations": ("A child-start marker and mapped executable/runtime do not prove first guest instruction, initialized game state, rendered title screen, or gameplay. The title-preparation log precedes guest-thread resume. No synthetic input or gameplay certification." +
                              (" Filesystem access was not restricted; external-path scanning is not proof that source reads were blocked." if args.isolation == "mapped-libraries" else ""))}
    evidence_write_errors = []
    for log, payload in log_records:
        try:
            with (checkpoint / log.name).open("xb") as stream:
                stream.write(payload)
        except OSError as exc:
            evidence_write_errors.append({"path": str(log), "error": str(exc)})
    if evidence_write_errors:
        report["evidence_log_copy_errors"] = evidence_write_errors
        report["launcher_child_smoke_passed"] = False
        report["early_stop_reason"] = report["early_stop_reason"] or "evidence_log_copy_failed"
        launcher_child_passed = False
    for source, destination in ((trial / "bootstrap.log", checkpoint / "bootstrap.log"),):
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            evidence_write_errors.append({"path": str(source), "error": str(exc)})
    try:
        (trial / "mapped-files.txt").write_text(mapped_files)
        shutil.copy2(trial / "mapped-files.txt", checkpoint / "mapped-files.txt")
    except OSError as exc:
        evidence_write_errors.append({"path": "mapped-files.txt", "error": str(exc)})
    if evidence_write_errors:
        report["evidence_write_errors"] = evidence_write_errors
        report["launcher_child_smoke_passed"] = False
        report["early_stop_reason"] = report["early_stop_reason"] or "evidence_write_failed"
        launcher_child_passed = False
    report["launcher_child_smoke_passed"] = launcher_child_passed
    receipt_bytes = json.dumps(report, indent=2) + "\n"
    try:
        (trial / "result.json").write_text(receipt_bytes)
    except OSError as exc:
        evidence_write_errors.append({"path": "trial/result.json", "error": str(exc)})
    try:
        shutil.copy2(trial / "result.json", checkpoint / "result.json")
    except OSError as exc:
        evidence_write_errors.append({"path": "checkpoint/result.json", "error": str(exc)})
    if evidence_write_errors:
        report["evidence_write_errors"] = evidence_write_errors
        report["launcher_child_smoke_passed"] = False
        launcher_child_passed = False
        report["early_stop_reason"] = report["early_stop_reason"] or "evidence_write_failed"
        receipt_bytes = json.dumps(report, indent=2) + "\n"
        for path in (trial / "result.json", checkpoint / "result.json"):
            try:
                path.write_text(receipt_bytes)
            except OSError:
                pass
    print(json.dumps(report, indent=2), flush=True)
    raise SystemExit(0 if launcher_child_passed else 1)


if __name__ == "__main__":
    main()
