#!/usr/bin/env python3
"""Synthetic, non-game regressions for personal package identity and receipts."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load_script("package_smoke")
hotspots = load_script("candidate_hotspot_check")
packager = load_script("package_macos")
installer = load_script("test_content_install")
profile_stager = load_script("stage_content_profiles")


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ReleaseToolingTests(unittest.TestCase):
    def test_profile_staging_installer_links_exact_selected_runtime(self):
        with tempfile.TemporaryDirectory(prefix="futari-selected-runtime-") as temporary:
            root = Path(temporary)
            runtime_dir = root / "fresh artifacts with spaces"
            runtime_dir.mkdir()
            runtime = runtime_dir / "librexruntimed.dylib"
            runtime.write_bytes(b"selected fresh runtime")
            with mock.patch.object(installer.subprocess, "run") as run_build:
                installer.build(root / "install_cli", root / "content_manager_rextest.cpp",
                                runtime_library=runtime)
            command = run_build.call_args.args[0]
            resolved_runtime = runtime.resolve()
            self.assertIn(str(resolved_runtime), command)
            self.assertIn(f"-Wl,-rpath,{resolved_runtime.parent}", command)
            self.assertFalse(any(arg.startswith(f"-L{installer.SDK / 'out/mac-arm64/Debug'}")
                                 for arg in command))
            self.assertNotIn("-lrexruntimed", command)

    def test_selected_runtime_must_be_present_regular_and_expected_name(self):
        with tempfile.TemporaryDirectory(prefix="futari-runtime-gates-") as temporary:
            root = Path(temporary)
            missing = root / "librexruntimed.dylib"
            with self.assertRaises(ValueError):
                installer.selected_runtime_library(missing)
            wrong_name = root / "liboldruntime.dylib"
            wrong_name.write_bytes(b"old")
            with self.assertRaises(ValueError):
                installer.selected_runtime_library(wrong_name)
            actual = root / "actual.dylib"
            actual.write_bytes(b"selected")
            symlink = root / "librexruntimed.dylib"
            symlink.symlink_to(actual)
            with self.assertRaises(ValueError):
                installer.selected_runtime_library(symlink)

    def test_staging_scrubs_loader_overrides_but_records_runtime_load_diagnostic(self):
        with mock.patch.dict(os.environ, {
                "DYLD_LIBRARY_PATH": "/old/sdk",
                "DYLD_FALLBACK_LIBRARY_PATH": "/old/fallback",
                "FUTARI_KEEP_ME": "yes"}, clear=True):
            env = profile_stager.installer_environment()
        self.assertNotIn("DYLD_LIBRARY_PATH", env)
        self.assertNotIn("DYLD_FALLBACK_LIBRARY_PATH", env)
        self.assertEqual(env["DYLD_PRINT_LIBRARIES"], "1")
        self.assertEqual(env["FUTARI_KEEP_ME"], "yes")
        self.assertTrue(profile_stager.runtime_load_observed(
            "dyld: loaded /fresh/artifacts/librexruntimed.dylib", Path("/fresh/artifacts/librexruntimed.dylib")))
        self.assertFalse(profile_stager.runtime_load_observed(
            "dyld: loaded /old/sdk/librexruntimed.dylib", Path("/fresh/artifacts/librexruntimed.dylib")))

    def make_synthetic_app(self, root):
        app = root / "Futari.app"
        macos = app / "Contents/MacOS"
        data = app / "Contents/Resources/GameData"
        macos.mkdir(parents=True)
        (app / "Contents/Resources/vulkan/share/vulkan/icd.d").mkdir(parents=True)
        (app / "Contents/Resources/vulkan/lib").mkdir(parents=True)
        data.mkdir(parents=True)
        (app / "Contents/_CodeSignature").mkdir()
        (macos / "FutariLauncher").write_bytes(b"synthetic launcher")
        (macos / "FutariReplayBundle").write_bytes(b"synthetic replay helper")
        (data / "default.xex").write_bytes(b"synthetic game data")
        (app / "Contents/_CodeSignature/CodeResources").write_bytes(b"synthetic signature")
        plist = {"CFBundleIdentifier": "com.smpduong.futari", "CFBundleExecutable": "FutariLauncher",
                 "LSMinimumSystemVersion": "27.0"}
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(plist))
        uuid = "01234567-89AB-CDEF-0123-456789ABCDEF"
        binary_names = ["futari15x360", "librexruntimed.dylib", "librexgpu-xenosd.dylib",
                        "libTracyClientd.dylib", "libfutari15x360_reco_normal.dylib",
                        "libfutari15x360_reco_abnormal.dylib", "libfutari15x360_palm_normal.dylib",
                        "libfutari15x360_palm_abnormal.dylib", "libfutari15x360_kiniro_normal.dylib",
                        "libfutari15x360_kiniro_abnormal.dylib", "libfutari15x360_hirow_normal.dylib",
                        "libfutari15x360_hirow_abnormal.dylib", "vulkan/lib/libMoltenVK.dylib",
                        "vulkan/lib/libvulkan.1.dylib",
                        "vulkan/share/vulkan/icd.d/MoltenVK_icd.json"]
        rows = []
        for name in binary_names:
            target = (app / "Contents/Resources" / name if name.startswith("vulkan/")
                      else macos / name)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = ("synthetic:" + name).encode()
            target.write_bytes(content)
            is_macho = name.endswith(".dylib") or name == "futari15x360"
            rows.append({"input": name, "source_sha256": digest(("source:" + name).encode()),
                         "source_uuid": uuid if is_macho else None,
                         "packaged_path": str(target.relative_to(app)),
                         "packaged_sha256": digest(content),
                         "packaged_uuid": uuid if is_macho else None})
        profile_root = app / "Contents/Resources/ContentProfiles"
        profile_specs = {
            "base": [],
            "ver101": ["9893A78CA6062B935BD1F53EA43CFD546B7246DF43"],
            "blacklabel": ["496F816DD4B53DAEFF931838B6C69831AAD7663043"],
            "both": ["9893A78CA6062B935BD1F53EA43CFD546B7246DF43",
                     "496F816DD4B53DAEFF931838B6C69831AAD7663043"],
        }
        source_hashes = {package_id: digest(("package:" + package_id).encode())
                         for ids in profile_specs.values() for package_id in ids}
        installer = {"driver": "scripts/install_dlc_via_runtime.py",
                     "driver_sha256": digest(b"driver"),
                     "harness": "scripts/test_content_install.py",
                     "harness_sha256": digest(b"harness"),
                     "content_manager_source_sha256": digest(b"content manager"),
                     "helper_binary_sha256": digest(b"helper")}
        launcher_sources = [
            {"path": "scripts/package_launcher.mm", "source_sha256": digest(b"launcher")},
            {"path": "scripts/package_launcher_ui.inc", "source_sha256": digest(b"launcher ui")},
        ]
        for content, ids in profile_specs.items():
            profile = profile_root / content
            profile.mkdir(parents=True, exist_ok=True)
            file_rows = []
            for package_id in ids:
                relative = f"0000000000000000/435A07D7/00000002/{package_id}/sample.bin"
                target = profile / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(("payload:" + package_id).encode())
                file_rows.append({"path": relative, "bytes": target.stat().st_size,
                                  "sha256": digest(target.read_bytes())})
            profile_manifest = {"schema_version": 1, "title_id": "435A07D7", "package_type": 2,
                                "content": content,
                                "source_packages": [{"package_id": package_id,
                                                     "sha256": source_hashes[package_id]} for package_id in ids],
                                "installer": installer,
                                "build_identity_sha256": packager.calculate_build_identity(
                                    content, rows, launcher_sources, digest(b"replay source"),
                                    digest(b"replay header"), file_rows),
                                "files": file_rows}
            (profile / "content-profile-manifest.json").write_text(json.dumps(profile_manifest))
        profile_reports = packager.validate_content_profiles(profile_root)
        resources_root = app / "Contents/Resources"
        resource_hashes = {path.relative_to(resources_root).as_posix(): digest(path.read_bytes())
                           for path in resources_root.rglob("*")
                           if path.is_file() and not path.is_symlink()}
        manifest = {
            "schema_version": 3,
            "source_commit": "synthetic-test",
            "minimum_macos_version": "27.0",
            "distribution": "local personal package",
            "contains_local_game_data": True,
            "packager_source_sha256": digest(b"packager source"),
            "launcher": {"identity_scheme": "source_inventory_external_post_sign_receipt_v1",
                         "source_sha256": digest(b"source launcher"),
                         "packaged_path": "Contents/MacOS/FutariLauncher",
                         "packaged_uuid": uuid},
            "replay_helper": {"source_sha256": digest(b"replay source"),
                              "header_sha256": digest(b"replay header"),
                              "source": "scripts/replay_bundle.mm",
                              "header": "scripts/libarchive_api_subset.h",
                              "packaged_path": "Contents/MacOS/FutariReplayBundle",
                              "packaged_sha256": digest(b"synthetic replay helper"),
                              "packaged_uuid": uuid},
            "helper_sources": {"replay_bundle_source_sha256": digest(b"replay source"),
                               "replay_bundle_header_sha256": digest(b"replay header")},
            "launcher_sources": launcher_sources,
            "inputs": rows,
            "game_data_sha256": {"default.xex": digest(b"synthetic game data")},
            "content_profiles": profile_reports,
            "resource_sha256": resource_hashes,
            "replay_build_identities": {name: row["build_identity_sha256"]
                                         for name, row in profile_reports.items()},
        }
        manifest_path = app / "Contents/Resources/package-manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        receipt = {"schema_version": 1,
                   "receipt_type": "external_post_sign_bundle_identity_v1",
                   "app_name": app.name,
                   "package_manifest_sha256": digest(manifest_path.read_bytes()),
                   "codesign_verification": {"argv": ["/usr/bin/codesign", "--verify", "--deep", "--strict"],
                                              "returncode": 0},
                   "files": [{"path": path.relative_to(app).as_posix(),
                              "bytes": path.stat().st_size,
                              "sha256": digest(path.read_bytes())}
                             for path in sorted(app.rglob("*")) if path.is_file()]}
        (app.parent / (app.name + ".release-receipt.json")).write_text(json.dumps(receipt))
        return app, manifest, uuid

    def test_candidate_identity_accepts_bound_synthetic_personal_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, _, uuid = self.make_synthetic_app(Path(temporary))
            with mock.patch.object(smoke, "verify_uuid") as verify, \
                 mock.patch.object(smoke, "verify_minimum_os"), \
                 mock.patch.object(smoke.subprocess, "run") as codesign:
                identity = smoke.validate_candidate(app)
            self.assertEqual(identity["binary_count"], 15)
            self.assertEqual(identity["game_data_count"], 1)
            self.assertEqual(len(identity["candidate_identity_sha256"]), 64)
            self.assertEqual(verify.call_count, 16)
            codesign.assert_called_once()

    def test_candidate_identity_rejects_legacy_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, manifest, _ = self.make_synthetic_app(Path(temporary))
            manifest.pop("schema_version")
            (app / "Contents/Resources/package-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "repackage first"):
                smoke.validate_candidate(app)

    def test_candidate_identity_rejects_hash_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, _, _ = self.make_synthetic_app(Path(temporary))
            (app / "Contents/MacOS/futari15x360").write_bytes(b"changed")
            with mock.patch.object(smoke, "verify_uuid"), mock.patch.object(smoke, "verify_minimum_os"), \
                 mock.patch.object(smoke.subprocess, "run"):
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    smoke.validate_candidate(app)

    def test_external_receipt_binds_signed_launcher_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, _, _ = self.make_synthetic_app(Path(temporary))
            receipt = app.parent / (app.name + ".release-receipt.json")
            with mock.patch.object(smoke, "verify_uuid"), mock.patch.object(smoke, "verify_minimum_os"), \
                 mock.patch.object(smoke.subprocess, "run"):
                smoke.validate_candidate(app)
                (app / "Contents/MacOS/FutariLauncher").write_bytes(b"signed launcher changed")
                with self.assertRaisesRegex(ValueError, "external candidate receipt"):
                    smoke.validate_candidate(app)

    def test_candidate_identity_rejects_profile_for_other_guest_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, manifest, _ = self.make_synthetic_app(Path(temporary))
            manifest["inputs"][0]["source_sha256"] = digest(b"different guest/runtime build")
            (app / "Contents/Resources/package-manifest.json").write_text(json.dumps(manifest))
            with mock.patch.object(smoke, "verify_uuid"), mock.patch.object(smoke, "verify_minimum_os"), \
                 mock.patch.object(smoke.subprocess, "run"):
                with self.assertRaisesRegex(RuntimeError, "build identity mismatches"):
                    smoke.validate_candidate(app)

    def test_content_profile_union_rejects_conflicting_same_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, _, _ = self.make_synthetic_app(Path(temporary))
            profiles = app / "Contents/Resources/ContentProfiles"
            relative = "0000000000000000/435A07D7/00000002/496F816DD4B53DAEFF931838B6C69831AAD7663043/sample.bin"
            both_file = profiles / "both" / relative
            both_file.write_bytes(b"conflicting duplicate")
            manifest_path = profiles / "both/content-profile-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            original_build_id = manifest["build_identity_sha256"]
            row = next(item for item in manifest["files"] if item["path"] == relative)
            row["bytes"] = both_file.stat().st_size
            row["sha256"] = digest(both_file.read_bytes())
            # The collision is rejected before build-identity validation; preserve
            # the valid old identity so this exercises the merge guard itself.
            manifest["build_identity_sha256"] = original_build_id
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, "conflicting profile merge"):
                packager.validate_content_profiles(profiles)

    def test_candidate_identity_rejects_manifest_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, manifest, _ = self.make_synthetic_app(Path(temporary))
            manifest["inputs"][0]["packaged_path"] = "../../outside"
            (app / "Contents/Resources/package-manifest.json").write_text(json.dumps(manifest))
            with mock.patch.object(smoke, "verify_uuid"), mock.patch.object(smoke, "verify_minimum_os"), \
                 mock.patch.object(smoke.subprocess, "run"):
                with self.assertRaisesRegex(ValueError, "unsafe package member path"):
                    smoke.validate_candidate(app)

    def test_hotspot_checker_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "candidate.log"
            log.write_text("launcher: replay_saved_game_compat=on\n")
            report = root / "receipt.json"
            report.write_text("preserve me\n")
            with mock.patch.object(sys, "argv", ["candidate_hotspot_check.py", str(log), "--json", str(report)]), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    hotspots.main()
            self.assertEqual(report.read_text(), "preserve me\n")

    def test_hotspot_checker_creates_fresh_report_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "candidate.log"
            log.write_text("launcher: replay_saved_game_compat=on\n")
            report = root / "new" / "receipt.json"
            with mock.patch.object(sys, "argv", ["candidate_hotspot_check.py", str(log), "--json", str(report)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    hotspots.main()
            self.assertEqual(result.exception.code, 0)
            data = json.loads(report.read_text())
            self.assertTrue(data["hotspots"]["replay_policy"]["observed"])
            self.assertFalse(data["hotspots"]["stage5_boss"]["observed"])

    def test_signal_group_targets_owned_process_group(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        try:
            smoke.signal_group(child.pid, 15)
            child.wait(timeout=5)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                smoke.signal_group(child.pid, 9)
                child.wait(timeout=5)

    def test_launcher_only_survival_is_not_child_readiness_or_game_boot(self):
        expected = {"base": digest(b"base build")}
        self.assertFalse(smoke.launcher_child_ready(
            True, None, False, None, expected, True, True, True, False, False, 0, 180, [], []))
        self.assertTrue(smoke.launcher_child_ready(
            True, 1234, True, "base", expected, True, True, True, True, True, 180, 180, [], []))
        self.assertFalse(smoke.launcher_child_ready(
            True, 1234, True, "base", expected, True, True, True, False, True, 180, 180, [], []))
        self.assertFalse(smoke.launcher_child_ready(
            True, 1234, True, "base", expected, True, True, True, True, False, 180, 180, [], []))
        self.assertFalse(smoke.launcher_child_ready(
            True, 1234, True, "base", expected, True, True, True, True, True, 179, 180, [], []))

    def test_trial_child_requires_exact_relocated_executable_and_all_data_paths(self):
        trial = Path("/private/tmp/trial with spaces")
        app = trial / "Futari Personal.app"
        good = (f"{app}/Contents/MacOS/futari15x360 --game_data_root={app}/Contents/Resources/GameData "
                f"--user_data_root={trial}/UserData/Profiles/base --cache_root={trial}/UserData/Profiles/base/cache "
                f"--log_file={trial}/UserData/Logs/session.log")
        self.assertTrue(smoke.command_is_trial_game(good, trial, app))
        self.assertFalse(smoke.command_is_trial_game(good.replace("Futari Personal.app", "Other.app"), trial, app))
        self.assertFalse(smoke.command_is_trial_game(good.replace("--log_file=", "--other_log="), trial, app))

    def test_trial_child_accepts_only_verified_tmp_alias_for_exact_trial(self):
        if os.path.realpath("/tmp") != os.path.realpath("/private/tmp"):
            self.skipTest("host does not expose /tmp as the standard /private/tmp alias")
        trial = Path("/private/tmp/trial-unique")
        app = trial / "Futari.app"
        good = (f"{app}/Contents/MacOS/futari15x360 --game_data_root={app}/Contents/Resources/GameData "
                f"--user_data_root={trial}/UserData/Profiles/base --cache_root={trial}/UserData/Profiles/base/cache "
                f"--log_file={trial}/UserData/Logs/session.log")
        aliased = good.replace("/private/tmp/", "/tmp/")
        self.assertTrue(smoke.command_is_trial_game(aliased, trial, app))
        self.assertFalse(smoke.command_is_trial_game(aliased.replace("trial-unique", "other-trial"), trial, app))
        self.assertFalse(smoke.command_is_trial_game(aliased.replace("/UserData/Profiles/base", "/UserData/../Profiles/base"), trial, app))
        self.assertFalse(smoke.command_is_trial_game(
            aliased.replace("/UserData/Profiles/base --cache_root=",
                            "/UserData/Profiles/base-extra --cache_root="), trial, app))
        self.assertFalse(smoke.command_is_trial_game(aliased.replace("futari15x360 ", "futari15x360.evil "), trial, app))
        for old, new in (("/Resources/GameData ", "/Resources/GameData-evil "),
                         ("/Profiles/base/cache ", "/Profiles/base/cache-evil "),
                         ("/Logs/session.log", "/Logs/session.log.evil")):
            self.assertFalse(smoke.command_is_trial_game(aliased.replace(old, new), trial, app))

    def test_trial_child_rejects_nonstandard_symlink_for_log_root(self):
        with tempfile.TemporaryDirectory(prefix="futari-log-alias-") as td:
            root = Path(td)
            trial = root / "trial"
            app = trial / "Futari.app"
            logs = trial / "UserData/Logs"
            logs.mkdir(parents=True)
            alias = root / "external-logs"
            alias.symlink_to(logs, target_is_directory=True)
            command = (f"{app}/Contents/MacOS/futari15x360 --game_data_root={app}/Contents/Resources/GameData "
                       f"--user_data_root={trial}/UserData/Profiles/base --cache_root={trial}/UserData/Profiles/base/cache "
                       f"--log_file={alias}/session.log")
            self.assertFalse(smoke.command_is_trial_game(command, trial, app))

    def test_mapped_file_match_requires_complete_path_field(self):
        expected = Path("/private/tmp/trial/Futari Personal.app/Contents/MacOS/futari15x360")
        self.assertTrue(smoke.mapped_contains(
            f"p42\nn{expected}\n", expected))
        self.assertFalse(smoke.mapped_contains(
            f"p42\nn{expected}.evil\n", expected))

    def test_dyld_external_scan_ignores_status_text_but_keeps_absolute_image_paths(self):
        self.assertEqual(smoke.dyld_image_path(
            "dyld[123]: <ABCDEF01-1234-5678-90AB-CDEF01234567> "
            "/Users/example/project/libsample.dylib"),
            "/Users/example/project/libsample.dylib")
        self.assertIsNone(smoke.dyld_image_path(
            "dyld[123]: move loaded to delayed: AppPredictionClient"))
        self.assertIsNone(smoke.dyld_image_path(
            "dyld[123]: libsample.dylib has weak-def symbol used by libfoo.dylib"))

    def test_process_scan_finds_exact_child_without_a_marker(self):
        trial = Path("/private/tmp/trial-unique")
        app = trial / "Futari.app"
        command = (f"{app}/Contents/MacOS/futari15x360 --game_data_root={app}/Contents/Resources/GameData "
                   f"--user_data_root={trial}/UserData/Profiles/base --cache_root={trial}/UserData/Profiles/base/cache "
                   f"--log_file={trial}/UserData/Logs/uuid.log")
        ps = f" 431  400  431 Sat Oct  3 22:10:56 2026 {command}\n"
        row = smoke.find_owned_trial_child(ps, 400, trial, app)
        self.assertIsNotNone(row)
        self.assertEqual(row["pid"], 431)
        self.assertIsNone(smoke.find_owned_trial_child(ps.replace(" 400 ", " 1 "), 400, trial, app))

    def test_title_evidence_requires_same_live_child_and_its_log(self):
        with tempfile.TemporaryDirectory(prefix="futari-title-binding-") as td:
            trial = Path(td)
            app = trial / "Futari.app"
            logs = trial / "UserData/Logs"
            logs.mkdir(parents=True)
            log = logs / "session.log"
            log.write_text("Initializing shader storage for title 435A07D7...\n")
            identity = {"pid": 431, "start_time": "start-431",
                        "command": f"{app}/Contents/MacOS/futari15x360 "
                                   f"--game_data_root={app}/Contents/Resources/GameData "
                                   f"--user_data_root={trial}/UserData/Profiles/base "
                                   f"--cache_root={trial}/UserData/Profiles/base/cache "
                                   f"--log_file={log}",
                        "child_log_argument": str(log)}
            good_marker = {"pid": 431, "log": str(log)}
            wrong_pid = {"pid": 432, "log": str(log)}
            wrong_log = {"pid": 431, "log": str(logs / "other.log")}
            self.assertFalse(smoke.title_evidence_matches_child(None, True, True, good_marker,
                                                                 str(log), trial / "UserData"))
            self.assertFalse(smoke.title_evidence_matches_child(identity, False, True, good_marker,
                                                                 str(log), trial / "UserData"))
            self.assertFalse(smoke.title_evidence_matches_child(identity, True, True, wrong_pid,
                                                                 str(log), trial / "UserData"))
            self.assertFalse(smoke.title_evidence_matches_child(identity, True, True, wrong_log,
                                                                 str(log), trial / "UserData"))
            self.assertTrue(smoke.title_evidence_matches_child(identity, True, True, good_marker,
                                                                str(log), trial / "UserData"))
            self.assertTrue(smoke.title_evidence_matches_child(identity, True, True, None,
                                                                str(log), trial / "UserData"))

    def test_verified_child_cleanup_uses_pid_and_verifies_after_kill(self):
        trial = Path("/private/tmp/trial-unique")
        app = trial / "Futari.app"
        command = (f"{app}/Contents/MacOS/futari15x360 --game_data_root={app}/Contents/Resources/GameData "
                   f"--user_data_root={trial}/UserData/Profiles/base --cache_root={trial}/UserData/Profiles/base/cache "
                   f"--log_file={trial}/UserData/Logs/uuid.log")
        identity = {"pid": 431, "parent_pid": 400, "process_group": 431,
                    "start_time": "Sat Oct 3 22:10:56 2026", "command": command}
        live = {**identity}
        with mock.patch.object(smoke, "snapshot_process", side_effect=[live, live, live, live, None]), \
                mock.patch.object(smoke.os, "kill") as kill:
            result = smoke.terminate_verified_trial_child(identity, 400, trial, app, wait_seconds=0)
        self.assertEqual(result["status"], "terminated")
        self.assertEqual(result["signal"], "SIGKILL")
        kill.assert_any_call(431, smoke.signal.SIGTERM)
        kill.assert_any_call(431, smoke.signal.SIGKILL)
        self.assertEqual(kill.call_count, 2)

    def test_verified_child_cleanup_refuses_pid_reuse(self):
        trial = Path("/private/tmp/trial-unique")
        app = trial / "Futari.app"
        command = (f"{app}/Contents/MacOS/futari15x360 --game_data_root={app}/Contents/Resources/GameData "
                   f"--user_data_root={trial}/UserData/Profiles/base --cache_root={trial}/UserData/Profiles/base/cache "
                   f"--log_file={trial}/UserData/Logs/uuid.log")
        identity = {"pid": 431, "parent_pid": 400, "process_group": 431,
                    "start_time": "original", "command": command}
        reused = {**identity, "start_time": "different"}
        with mock.patch.object(smoke, "snapshot_process", return_value=reused), \
                mock.patch.object(smoke.os, "kill") as kill:
            result = smoke.terminate_verified_trial_child(identity, 400, trial, app)
        self.assertEqual(result["status"], "identity_changed_refused")
        kill.assert_not_called()

    def test_process_snapshot_distinguishes_absence_from_inspection_failure(self):
        absent = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        failure = subprocess.CompletedProcess([], 1, stdout="", stderr="ps: operation not permitted")
        with mock.patch.object(smoke.subprocess, "run", return_value=absent):
            self.assertIsNone(smoke.snapshot_process(99999999))
        with mock.patch.object(smoke.subprocess, "run", return_value=failure):
            with self.assertRaises(smoke.ProcessInspectionError):
                smoke.snapshot_process(1234)

    def test_cleanup_never_signals_when_identity_inspection_fails(self):
        trial = Path("/private/tmp/trial-unique")
        app = trial / "Futari.app"
        command = (f"{app}/Contents/MacOS/futari15x360 --game_data_root={app}/Contents/Resources/GameData "
                   f"--user_data_root={trial}/UserData/Profiles/base --cache_root={trial}/UserData/Profiles/base/cache "
                   f"--log_file={trial}/UserData/Logs/uuid.log")
        identity = {"pid": 431, "parent_pid": 400, "initial_parent_pid": 400,
                    "process_group": 431, "start_time": "original", "command": command}
        with mock.patch.object(smoke, "snapshot_process", side_effect=smoke.ProcessInspectionError("denied")), \
                mock.patch.object(smoke.os, "kill") as kill:
            result = smoke.terminate_verified_trial_child(identity, 400, trial, app)
        self.assertEqual(result["status"], "could_not_verify_initial_identity")
        kill.assert_not_called()

    def test_smoke_isolation_modes_are_explicit_and_fail_honest(self):
        app = Path("/tmp/relocated/Futari.app")
        profile = Path("/tmp/relocated/isolation.sb")
        paths = ["/project", "/sdk", "/opt/homebrew"]
        self.assertEqual(smoke.smoke_command(app, profile, "sandboxed"),
                         ["/usr/bin/sandbox-exec", "-f", str(profile),
                          str(app / "Contents/MacOS/FutariLauncher")])
        self.assertEqual(smoke.smoke_command(app, profile, "mapped-libraries"),
                         [str(app / "Contents/MacOS/FutariLauncher")])
        sandboxed = smoke.isolation_report("sandboxed", paths)
        self.assertTrue(sandboxed["filesystem_isolation_enforced"])
        self.assertEqual(sandboxed["denied_read_paths"], paths)
        mapped = smoke.isolation_report("mapped-libraries", paths)
        self.assertFalse(mapped["filesystem_isolation_enforced"])
        self.assertEqual(mapped["denied_read_paths"], [])
        self.assertEqual(mapped["audited_external_paths"], paths)
        with self.assertRaisesRegex(ValueError, "unsupported smoke isolation"):
            smoke.smoke_command(app, profile, "unknown")

    @unittest.skipUnless(sys.platform == "darwin", "renameatx_np is macOS-specific")
    def test_package_publish_is_atomic_and_never_replaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "staged.app"
            stage.mkdir()
            (stage / "marker").write_text("new candidate")
            destination = root / "candidate.app"
            packager.publish_exclusive(stage, destination)
            self.assertEqual((destination / "marker").read_text(), "new candidate")
            second = root / "second.app"
            second.mkdir()
            (second / "marker").write_text("must survive")
            with self.assertRaises(FileExistsError):
                packager.publish_exclusive(second, destination)
            self.assertEqual((destination / "marker").read_text(), "new candidate")
            self.assertTrue(second.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
