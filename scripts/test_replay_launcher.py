#!/usr/bin/env python3
"""Exercise the real replay wrapper around a fake game. No gameplay claims."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import launch_replays as W
import replay_library as R


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.sandbox = self.root / "sandbox"
        self.session = self.sandbox / "manual/replay-library-both"
        self.calls = []
        self.clean = True
        self.explicit_clean = None
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(W.launcher, "SANDBOX", self.sandbox))
        self.stack.enter_context(patch.object(W.launcher, "verify_identity", return_value=(True, {"files": {"game": {"sha256": "a"*64}}}, [])))
        self.install = self.stack.enter_context(patch.object(W.launcher, "install_content"))
        self.stack.enter_context(patch.object(W.Library, "import_pending", side_effect=lambda *a: self.calls.append("import") or []))
        self.stack.enter_context(patch.object(W.Library, "export", side_effect=lambda *a: self.calls.append("export") or []))
        self.stack.enter_context(patch.object(W.launcher, "main", side_effect=self.fake_game))

    def fake_game(self, session_lock_fd):
        self.calls.append("game")
        self.assertGreaterEqual(session_lock_fd, 0)
        with self.assertRaises(ValueError):
            with R.session_lock(self.session):
                self.fail("game not locked")
        launches = self.session / "launches"
        launches.mkdir(exist_ok=True)
        log = launches / str(len(list(launches.iterdir())))
        log.mkdir()
        result = {"exit_code": 0 if self.clean else -15,
                  "stopped_by_launcher": not self.clean,
                  "clean_exit": self.clean}
        if self.explicit_clean is not None:
            result["clean_exit"] = self.explicit_clean
        (log / "launch-result.json").write_text(json.dumps(result))
        return 0

    def run_wrapper(self, action="play"):
        with patch.object(sys, "argv", ["replays", action, "--library", str(self.root / "library")]):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return W.main()

    def test_order_and_persistent_session(self):
        self.assertEqual(self.run_wrapper(), 0)
        self.assertEqual(self.calls, ["game", "export"])
        sentinel = self.session / "keep-save"
        sentinel.write_bytes(b"keep")
        self.assertEqual(self.run_wrapper(), 0)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertEqual([c.kwargs["overwrite"] for c in self.install.call_args_list], [True, False])
        self.assertEqual(self.calls, ["game", "export", "game", "export"])

    def test_abnormal_exit_skips_export(self):
        self.clean = False
        self.assertEqual(self.run_wrapper(), 0)
        self.assertEqual(self.calls, ["game"])

    def test_explicit_nonclean_field_skips_export(self):
        self.explicit_clean = False
        self.assertEqual(self.run_wrapper(), 0)
        self.assertEqual(self.calls, ["game"])

    def test_result_is_clean_requires_explicit_clean_field(self):
        self.assertTrue(W.result_is_clean({
            "exit_code": 0, "stopped_by_launcher": False, "clean_exit": True}))
        for outcome in (
                {"exit_code": 0, "stopped_by_launcher": False},
                {"exit_code": 0, "stopped_by_launcher": False, "clean_exit": False},
                {"exit_code": 0, "stopped_by_launcher": True, "clean_exit": False},
                {"exit_code": -15, "stopped_by_launcher": True, "clean_exit": False},
                {"exit_code": True, "stopped_by_launcher": False, "clean_exit": True}):
            with self.subTest(outcome=outcome):
                self.assertFalse(W.result_is_clean(outcome))

    def stopped_fixture(self, pid=123456):
        session = self.root / "manual" / "fixture-both"
        launch = session / "launches" / "run-1"
        launch.mkdir(parents=True)
        (launch / "child-pid").write_text(f"{pid}\n")
        (launch / "launch-result.json").write_text(json.dumps({
            "exit_code": -15, "stopped_by_launcher": True,
            "clean_exit": False, "child_pid": pid}))
        return session, launch

    def test_stopped_export_receipt_accepts_dead_recorded_child(self):
        session, _ = self.stopped_fixture()
        with patch.object(W.launcher, "SANDBOX", self.root), \
             patch.object(W, "_pid_is_alive", return_value=False):
            got_session, got_data = W._stopped_export_session(
                "both", "fixture-both", "run-1")
            latest_session, latest_data = W._stopped_export_session(
                "both", "fixture-both", "latest")
        self.assertEqual(got_session, session)
        self.assertEqual(got_data, session / "user-data")
        self.assertEqual(latest_session, session)
        self.assertEqual(latest_data, session / "user-data")

    def test_stopped_export_receipt_rejects_mismatch_and_live_child(self):
        self.stopped_fixture()
        with patch.object(W.launcher, "SANDBOX", self.root):
            launch = self.root / "manual/fixture-both/launches/run-1"
            result = json.loads((launch / "launch-result.json").read_text())
            result["stopped_by_launcher"] = False
            (launch / "launch-result.json").write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "stopped_by_launcher"):
                W._stopped_export_session("both", "fixture-both", "run-1")
            result["stopped_by_launcher"] = True
            result["child_pid"] = 999
            (launch / "launch-result.json").write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "does not match"):
                W._stopped_export_session("both", "fixture-both", "run-1")
            result["child_pid"] = 123456
            (launch / "launch-result.json").write_text(json.dumps(result))
            with patch.object(W, "_pid_is_alive", return_value=True):
                with self.assertRaisesRegex(ValueError, "still running"):
                    W._stopped_export_session("both", "fixture-both", "run-1")

    def test_stopped_export_receipt_requires_explicit_typed_status_fields(self):
        self.stopped_fixture()
        launch = self.root / "manual/fixture-both/launches/run-1"
        receipt_path = launch / "launch-result.json"
        valid = json.loads(receipt_path.read_text())
        invalid_receipts = (
            ({key: value for key, value in valid.items() if key != "clean_exit"},
             "clean_exit=false"),
            ({**valid, "clean_exit": True}, "clean_exit=false"),
            ({**valid, "clean_exit": 0}, "clean_exit=false"),
            ({**valid, "clean_exit": "false"}, "clean_exit=false"),
            ({key: value for key, value in valid.items() if key != "exit_code"},
             "integer exit_code"),
            ({**valid, "exit_code": True}, "integer exit_code"),
            ({**valid, "exit_code": "-15"}, "integer exit_code"),
            ({**valid, "exit_code": None}, "integer exit_code"),
        )
        with patch.object(W.launcher, "SANDBOX", self.root), \
             patch.object(W, "_pid_is_alive", return_value=False):
            for result, error in invalid_receipts:
                with self.subTest(result=result):
                    receipt_path.write_text(json.dumps(result))
                    with self.assertRaisesRegex(ValueError, error):
                        W._stopped_export_session("both", "fixture-both", "run-1")

    def test_export_stopped_cli_uses_receipt_without_game(self):
        self.stopped_fixture()
        argv = ["replays", "export-stopped", "--content", "both",
                "--session", "fixture-both", "--launch", "latest",
                "--library", str(self.root / "library")]
        with patch.object(W.launcher, "SANDBOX", self.root), \
             patch.object(W, "_pid_is_alive", return_value=False), \
             patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(W.main(), 0)
        self.assertEqual(self.calls, ["export"])
        self.assertNotIn("game", self.calls)
        self.install.assert_not_called()

    def test_missing_stopped_session_is_not_created(self):
        argv = ["replays", "export-stopped", "--session", "missing-both",
                "--launch", "latest", "--library", str(self.root / "library")]
        with patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(W.main(), 1)
        self.assertFalse((self.sandbox / "manual" / "missing-both").exists())

    def test_explicit_export_does_not_launch_or_install(self):
        self.assertEqual(self.run_wrapper("export"), 0)
        self.assertEqual(self.calls, ["export"])
        self.install.assert_not_called()

    def test_import_does_not_launch(self):
        self.assertEqual(self.run_wrapper("import"), 0)
        self.assertEqual(self.calls, ["import"])

    def test_play_never_imports_pending_archives(self):
        self.assertEqual(self.run_wrapper(), 0)
        self.assertNotIn("import", self.calls)

    def test_explicit_import_consumes_pending_archives(self):
        self.assertEqual(self.run_wrapper("import"), 0)
        self.assertEqual(self.calls, ["import"])
        self.assertNotIn("game", self.calls)

    def test_live_profile_rejected(self):
        with R.session_lock(self.session):
            self.assertEqual(self.run_wrapper(), 1)
        self.assertEqual(self.calls, [])

    def test_bad_identity_stops_before_import_and_launch(self):
        with patch.object(W.launcher, "verify_identity", return_value=(False, {}, ["bad runtime"])):
            self.assertEqual(self.run_wrapper(), 1)
        self.assertEqual(self.calls, [])

    def test_build_change_preserves_profile(self):
        self.assertEqual(self.run_wrapper(), 0)
        self.calls.clear()
        with patch.object(W.launcher, "verify_identity", return_value=(True, {"files": {"game": {"sha256": "b"*64}}}, [])):
            self.assertEqual(self.run_wrapper(), 1)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
