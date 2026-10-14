#!/usr/bin/env python3
"""Focused synthetic tests for pure package-smoke observations."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


observation = load("smoke_observation", SCRIPTS / "smoke_observation.py")
smoke = load("package_smoke_under_test", SCRIPTS / "package_smoke.py")


class SmokeObservationTests(unittest.TestCase):
    def make_logs(self, root):
        user_data = root / "UserData"
        logs = user_data / "Logs"
        logs.mkdir(parents=True)
        return user_data, logs

    def test_smoke_reexports_observation_api(self):
        for name in ("parse_process_row", "parse_process_command", "command_path_match",
                     "command_is_trial_game", "dyld_image_path", "read_new_log_text",
                     "title_preparation_log"):
            with self.subTest(name=name):
                self.assertIs(getattr(smoke, name), getattr(observation, name))

    def test_disappearing_console_log_after_listing_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            _user_data, logs = self.make_logs(Path(td))
            console = logs / "one-console.log"
            console.write_text("FUTARI_DIAGNOSTIC_CHILD_STARTED pid=9 content=base log=/tmp/a.log\n")
            original_open = observation.os.open

            def vanish(path, *args, **kwargs):
                if Path(path).name == console.name:
                    console.unlink()
                    raise FileNotFoundError(path)
                return original_open(path, *args, **kwargs)

            with mock.patch.object(observation.os, "open", vanish):
                self.assertEqual(observation.observe_child_start_markers(logs), [])

    def test_non_directory_logs_root_is_not_treated_as_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            _user_data, logs = self.make_logs(Path(td))
            with mock.patch.object(observation.os, "scandir",
                                   side_effect=NotADirectoryError("Logs root replaced")):
                with self.assertRaises(observation.ObservationError):
                    observation.observe_child_start_markers(logs)

    def test_marker_conflict_remains_latched_after_logs_disappear(self):
        first = {"pid": 10, "content": "base", "log": "/tmp/a", "line": "first"}
        altered = {"pid": 11, "content": "base", "log": "/tmp/b", "line": "second"}
        retained, conflict = observation.retain_child_start_marker(None, [first])
        self.assertFalse(conflict)
        retained, conflict = observation.retain_child_start_marker(retained, [altered], conflict)
        self.assertTrue(conflict)
        retained, conflict = observation.retain_child_start_marker(retained, [], conflict)
        self.assertEqual(retained, first)
        self.assertTrue(conflict)

    def test_disappearing_rotation_during_stat_or_resolve_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            user_data, logs = self.make_logs(Path(td))
            base = logs / "session.log"
            rotated = logs / "session.1.log"
            base.write_text("no title\n")
            rotated.write_text("Initializing shader storage for title 435A07D7...\n")
            original_lstat = Path.lstat
            original_resolve = Path.resolve

            def remove_during_lstat(path, *args, **kwargs):
                if path.name == rotated.name:
                    path.unlink(missing_ok=True)
                    raise FileNotFoundError(path)
                return original_lstat(path, *args, **kwargs)

            with mock.patch.object(Path, "lstat", remove_during_lstat):
                self.assertIsNone(observation.title_preparation_log(base, user_data))

            rotated.write_text("Initializing shader storage for title 435A07D7...\n")

            def remove_during_resolve(path, *args, **kwargs):
                if path.name == rotated.name:
                    path.unlink(missing_ok=True)
                    raise FileNotFoundError(path)
                return original_resolve(path, *args, **kwargs)

            with mock.patch.object(Path, "resolve", remove_during_resolve):
                self.assertIsNone(observation.title_preparation_log(base, user_data))

    def test_disappearing_rotation_during_read_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            user_data, logs = self.make_logs(Path(td))
            base = logs / "session.log"
            rotated = logs / "session.1.log"
            base.write_text("current log without marker\n")
            rotated.write_text("Initializing shader storage for title 435A07D7...\n")
            original_read = observation._read_text

            def remove_before_read(path):
                if Path(path).name == rotated.name:
                    Path(path).unlink(missing_ok=True)
                    raise FileNotFoundError(path)
                return original_read(path)

            with mock.patch.object(observation, "_read_text", remove_before_read):
                self.assertIsNone(observation.title_preparation_log(base, user_data))

    def test_disappearing_log_during_incremental_read_preserves_offset(self):
        with tempfile.TemporaryDirectory() as td:
            user_data, logs = self.make_logs(Path(td))
            log = logs / "session.log"
            log.write_text("one line\n")
            offsets = {}
            original_open = observation.os.open

            def remove_during_open(path, *args, **kwargs):
                if Path(path).name == log.name:
                    path.unlink(missing_ok=True)
                    raise FileNotFoundError(path)
                return original_open(path, *args, **kwargs)

            with mock.patch.object(observation.os, "open", remove_during_open):
                self.assertEqual(observation.read_new_log_text(user_data, offsets), "")
            self.assertNotIn(str(log), offsets)

    def test_disappearing_log_after_final_snapshot_listing_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            _user_data, logs = self.make_logs(Path(td))
            log = logs / "session.log"
            log.write_text("last line\n")
            original_lstat = Path.lstat

            def remove_during_lstat(path, *args, **kwargs):
                if path == log:
                    path.unlink(missing_ok=True)
                    raise FileNotFoundError(path)
                return original_lstat(path, *args, **kwargs)

            with mock.patch.object(Path, "lstat", remove_during_lstat):
                self.assertEqual(observation.read_log_files(logs), [])

    def test_final_log_snapshot_preserves_exact_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            _user_data, logs = self.make_logs(Path(td))
            log = logs / "session.log"
            payload = b"runtime line\xff\n"
            log.write_bytes(payload)
            self.assertEqual(observation.read_log_files(logs), [(log.resolve(), payload)])

    def test_hard_log_read_error_is_not_treated_as_no_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            _user_data, logs = self.make_logs(Path(td))
            console = logs / "one-console.log"
            console.write_text("no marker\n")
            original_open = observation.os.open

            def deny(path, *args, **kwargs):
                if Path(path).name == console.name:
                    raise PermissionError("fixture denial")
                return original_open(path, *args, **kwargs)

            with mock.patch.object(observation.os, "open", deny):
                with self.assertRaisesRegex(observation.ObservationError, "cannot read launcher console"):
                    observation.observe_child_start_markers(logs)

    def test_directory_permission_error_is_not_treated_as_missing_logs(self):
        with tempfile.TemporaryDirectory() as td:
            _user_data, logs = self.make_logs(Path(td))
            original_scandir = observation.os.scandir

            def deny(path):
                if Path(path) == logs:
                    raise PermissionError("fixture directory denial")
                return original_scandir(path)

            with mock.patch.object(observation.os, "scandir", deny):
                with self.assertRaisesRegex(observation.ObservationError, "cannot list log directory"):
                    observation.observe_child_start_markers(logs)

    def test_same_path_inode_replacement_restarts_incremental_observation(self):
        with tempfile.TemporaryDirectory() as td:
            user_data, logs = self.make_logs(Path(td))
            log = logs / "session.log"
            log.write_text("old")
            offsets = {}
            self.assertEqual(observation.read_new_log_text(user_data, offsets), "old")
            replacement = logs / "replacement.log"
            replacement.write_text("new content")
            os.replace(replacement, log)
            self.assertEqual(observation.read_new_log_text(user_data, offsets), "new content")

    def test_same_inode_truncation_restarts_incremental_observation(self):
        with tempfile.TemporaryDirectory() as td:
            user_data, logs = self.make_logs(Path(td))
            log = logs / "session.log"
            log.write_text("long previous contents")
            offsets = {}
            observation.read_new_log_text(user_data, offsets)
            log.write_text("short")
            self.assertEqual(observation.read_new_log_text(user_data, offsets), "short")

    def test_tail_overlap_detects_fatal_signature_split_between_reads(self):
        with tempfile.TemporaryDirectory() as td:
            user_data, logs = self.make_logs(Path(td))
            log = logs / "session.log"
            log.write_text("[fat")
            offsets = {}
            self.assertEqual(observation.read_new_log_text(user_data, offsets), "[fat")
            with log.open("a") as stream:
                stream.write("al] Unhandled exception")
            observed = observation.read_new_log_text(user_data, offsets)
            self.assertIn("[fatal] Unhandled exception", observed)

    def test_child_title_snapshot_is_cached_only_for_exact_process_identity(self):
        with tempfile.TemporaryDirectory() as td:
            user_data, logs = self.make_logs(Path(td))
            log = logs / "session.log"
            log.write_text("Initializing shader storage for title 435A07D7...\n")
            identity = (42, "Sat Oct  3 12:34:56 2026", "/trial/game")
            with mock.patch.object(observation, "title_preparation_observation",
                                   wraps=observation.title_preparation_observation) as scan:
                first = observation.title_observation_for_identity(identity, None, None, log, user_data)
                second = observation.title_observation_for_identity(identity, identity, first, log, user_data)
                changed = observation.title_observation_for_identity(
                    (43, identity[1], identity[2]), identity, first, log, user_data)
            self.assertEqual(first, second)
            self.assertEqual(scan.call_count, 2)
            self.assertEqual(changed, first)

    def test_process_parser_rejects_wrong_path_and_preserves_spaces(self):
        command = ("/private/tmp/a trial/Futari.app/Contents/MacOS/futari15x360 "
                   "--user_data_root=/private/tmp/a trial/UserData/Profiles/base "
                   "--cache_root=/private/tmp/a trial/UserData/Profiles/base/cache")
        parsed = observation.parse_process_command(command)
        self.assertEqual(parsed[0], "/private/tmp/a trial/Futari.app/Contents/MacOS/futari15x360")
        self.assertTrue(observation.command_path_match(
            command, "/private/tmp/a trial/UserData/Profiles/base", "--user_data_root="))
        self.assertFalse(observation.command_path_match(
            command, "/private/tmp/other/UserData/Profiles/base", "--user_data_root="))

    def test_child_identity_survives_unknown_mapping_and_log_observation(self):
        trial = Path("/private/tmp/smoke test")
        app = trial / "Futari.app"
        command = (f"{app}/Contents/MacOS/futari15x360 "
                   f"--game_data_root={app}/Contents/Resources/GameData "
                   f"--user_data_root={trial}/UserData/Profiles/base "
                   f"--cache_root={trial}/UserData/Profiles/base/cache "
                   f"--log_file={trial}/UserData/Logs/session.log")
        row = f"778 777 778 Sat Oct  3 12:34:56 2026 {command}"
        with mock.patch.object(smoke, "process_table_snapshot", return_value=row), \
             mock.patch.object(smoke.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("lsof", 10)), \
             mock.patch.object(smoke, "resolve_child_log_path",
                               side_effect=observation.ObservationError("fixture log denial")):
            found = smoke.discover_trial_child(777, trial, app)
        self.assertEqual(found["pid"], 778)
        self.assertEqual(found["parent_pid"], 777)
        self.assertFalse(found["mapping_query_succeeded"])
        self.assertIn("outcome unknown", found["mapping_query_error"])
        self.assertIn("fixture log denial", found["log_observation_error"])
        self.assertFalse(smoke.launcher_child_ready(
            True, found["pid"], True, "base", {"base": "id"},
            False, False, False, True, True, 30.0, 30, [], []))

    def test_smoke_pass_requires_known_observations_and_verified_cleanup(self):
        ready = True
        self.assertTrue(smoke.smoke_attempt_passed(ready, True, None, None, None, 4, 4))
        self.assertFalse(smoke.smoke_attempt_passed(ready, False, None, None, None, 4, 4))
        self.assertFalse(smoke.smoke_attempt_passed(ready, True, "process unknown", None, None, 4, 4))
        self.assertFalse(smoke.smoke_attempt_passed(ready, True, None, "control unknown", None, 4, 4))
        self.assertFalse(smoke.smoke_attempt_passed(ready, True, None, None, "log unknown", 4, 4))
        self.assertFalse(smoke.smoke_attempt_passed(ready, True, None, None, None, 4, 3))
        self.assertFalse(smoke.smoke_attempt_passed(ready, True, None, None, None, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
