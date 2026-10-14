#!/usr/bin/env python3
"""Pin the host-exit vs launcher-stop distinction (synthetic; no game is run).

The handoff raised a real question: a bounded launcher stop can return shell
status 0 while its receipt records clean_exit=false.  Is that a bug?

It is deliberate, and this file is the executable evidence for that answer.
`scripts/launch_ref_manual.py` ends with:

    return 0 if stopped_by_launcher or proc.returncode == 0 else 1

so the launcher's shell status means "the launcher completed the supervision it
was configured to do", NOT "the game process exited by itself".  The authority
for process-exit semantics is the receipt field `clean_exit`, and the wrapper
decides automatic export from that receipt, never from the shell status.

The two axes are deliberately independent:

  * a bounded stop at the --seconds safety bound is SUCCESSFUL supervision
    (the bound is the point), so the launcher returns 0 -- but the child was
    killed, so the receipt says clean_exit=false and export is skipped;
  * a genuinely clean host exit returns 0 AND records clean_exit=true, and
    only then may automatic export run.

Collapsing these into one code would destroy the distinction this project
depends on.  So the tests below assert the axis independence, not a change.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch_replays as W
import replay_library as R


def launcher_status(stopped_by_launcher, returncode):
    """The exact expression used by scripts/launch_ref_manual.py:615.

    Duplicated here on purpose: if that line ever changes, this file should be
    updated deliberately, which is the point of pinning it.
    """
    return 0 if stopped_by_launcher or returncode == 0 else 1


class HostExitSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.sandbox = self.root / "sandbox"
        self.session = self.sandbox / "manual/replay-library-both"
        self.calls = []
        self.receipt = None
        self.shell_status = 0
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(W.launcher, "SANDBOX", self.sandbox))
        self.stack.enter_context(patch.object(
            W.launcher, "verify_identity",
            return_value=(True, {"files": {"game": {"sha256": "a" * 64}}}, [])))
        self.stack.enter_context(patch.object(W.launcher, "install_content"))
        self.stack.enter_context(patch.object(
            W.Library, "import_pending",
            side_effect=lambda *a: self.calls.append("import") or []))
        self.stack.enter_context(patch.object(
            W.Library, "export",
            side_effect=lambda *a: self.calls.append("export") or []))

        def fake_game(session_lock_fd):
            self.calls.append("game")
            launches = self.session / "launches"
            launches.mkdir(exist_ok=True)
            log = launches / str(len(list(launches.iterdir())))
            log.mkdir()
            (log / "launch-result.json").write_text(json.dumps(self.receipt))
            (log / "child-pid").write_text("%d\n" % self.receipt["child_pid"])
            return self.shell_status

        self.stack.enter_context(patch.object(W.launcher, "main", side_effect=fake_game))

    def run_wrapper(self, action="play"):
        with patch.object(sys, "argv", ["replays", action, "--library",
                                        str(self.root / "library")]):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                return W.main()

    def test_bounded_stop_is_status_zero_but_skips_export(self):
        """The exact combination the handoff flagged, asserted as correct."""
        self.receipt = {"exit_code": -15, "stopped_by_launcher": True,
                        "clean_exit": False, "child_pid": 4242}
        self.shell_status = launcher_status(stopped_by_launcher=True, returncode=-15)
        self.assertEqual(self.shell_status, 0, "a bounded stop is successful supervision")
        self.assertEqual(self.run_wrapper(), 0)
        self.assertNotIn("export", self.calls,
                         "automatic export must be skipped for a bounded stop")

    def test_clean_host_exit_is_the_only_automatic_export(self):
        self.receipt = {"exit_code": 0, "stopped_by_launcher": False,
                        "clean_exit": True, "child_pid": 4243}
        self.shell_status = launcher_status(stopped_by_launcher=False, returncode=0)
        self.assertEqual(self.run_wrapper(), 0)
        self.assertEqual(self.calls, ["game", "export"])


    def test_shell_status_alone_never_decides_export(self):
        """Identical shell status 0, opposite export decisions, receipt-driven."""
        decisions = []
        cases = (
            ({"exit_code": 0, "stopped_by_launcher": False,
              "clean_exit": True, "child_pid": 4244}, True),
            ({"exit_code": -15, "stopped_by_launcher": True,
              "clean_exit": False, "child_pid": 4245}, False),
            ({"exit_code": 0, "stopped_by_launcher": True,
              "clean_exit": False, "child_pid": 4246}, False),
            ({"exit_code": 0, "stopped_by_launcher": False,
              "clean_exit": False, "child_pid": 4247}, False),
        )
        for receipt, expected_export in cases:
            with self.subTest(receipt=receipt):
                self.setUp()
                self.receipt = receipt
                self.shell_status = 0
                self.run_wrapper()
                decisions.append("export" in self.calls)
        self.assertEqual(decisions, [True, False, False, False])
        self.assertTrue(any(decisions) and not all(decisions),
                        "the receipt, not the shell status, must be decisive")

    def test_receipt_clean_exit_false_never_qualifies_as_clean(self):
        """result_is_clean is the single gate and it is receipt-only."""
        for value in (False, 0, "false", "True", None):
            with self.subTest(clean_exit=value):
                self.assertFalse(W.result_is_clean({
                    "exit_code": 0, "stopped_by_launcher": False,
                    "clean_exit": value}))

    def test_result_is_clean_fails_closed_on_untyped_or_missing_status(self):
        """Every untyped, missing, or non-dict receipt must be rejected.

        `result_is_clean` is the only gate on automatic export, and the whole
        point of the strict `type(...) is int` / `is False` / `is True`
        comparisons is that a malformed or JSON-coerced receipt can never be
        read as a clean host exit.  The behaviour was verified by direct
        inspection on 2026-09-26, but only the clean_exit axis was pinned by a
        test, so the non-dict guard and the exit_code / stopped_by_launcher
        type-confusion branches were unpinned regressions-in-waiting.  This
        test pins them without changing any behaviour.

        Note the Python-specific traps it locks down: `True == 1` and
        `False == 0`, so a plain `==` / truthiness check would accept an
        integer where a bool is required; and `0.0 == 0`, so a float exit
        code would pass an equality test but must not pass here.
        """
        self.assertTrue(W.result_is_clean({
            "exit_code": 0, "stopped_by_launcher": False, "clean_exit": True}),
            "the strictly typed receipt must still qualify")
        rejected = (
            # non-dict receipts: the isinstance guard, never reached otherwise
            (None, "None"),
            (["exit_code"], "list"),
            ("exit_code=0", "str"),
            (0, "int"),
            # missing keys must not be treated as "absent means false/zero"
            ({"stopped_by_launcher": False, "clean_exit": True},
             "missing exit_code"),
            ({"exit_code": 0, "clean_exit": True},
             "missing stopped_by_launcher"),
            # type confusion where == would have accepted the value
            ({"exit_code": 0, "stopped_by_launcher": 0, "clean_exit": True},
             "int 0 is not False"),
            ({"exit_code": 0, "stopped_by_launcher": "false", "clean_exit": True},
             "string 'false' is not False"),
            ({"exit_code": 0, "stopped_by_launcher": None, "clean_exit": True},
             "None is not False"),
            ({"exit_code": 0, "stopped_by_launcher": False, "clean_exit": 1},
             "int 1 is not True"),
            ({"exit_code": "0", "stopped_by_launcher": False, "clean_exit": True},
             "string exit_code"),
            ({"exit_code": 0.0, "stopped_by_launcher": False, "clean_exit": True},
             "float exit_code equals 0 but is not an int"),
            ({"exit_code": None, "stopped_by_launcher": False, "clean_exit": True},
             "None exit_code"),
        )
        for outcome, label in rejected:
            with self.subTest(receipt=label):
                self.assertFalse(W.result_is_clean(outcome))

    def test_export_stopped_rejects_receipt_claiming_clean_exit(self):
        """The mirror image: a receipt that claims a clean exit is not a stop."""
        session = self.root / "manual" / "fixture-both"
        launch = session / "launches" / "run-1"
        launch.mkdir(parents=True)
        (launch / "child-pid").write_text("123456\n")
        (launch / "launch-result.json").write_text(json.dumps({
            "exit_code": 0, "stopped_by_launcher": True,
            "clean_exit": True, "child_pid": 123456}))
        with patch.object(W.launcher, "SANDBOX", self.root), \
             patch.object(W, "_pid_is_alive", return_value=False):
            with self.assertRaisesRegex(ValueError, "clean_exit=false"):
                W._stopped_export_session("both", "fixture-both", "run-1")

    def test_session_lock_is_held_for_the_whole_export(self):
        """A fake game must not be able to re-enter the locked session."""
        seen = {}

        def fake_game(session_lock_fd):
            self.assertGreaterEqual(session_lock_fd, 0)
            try:
                with R.session_lock(self.session):
                    seen["reentrant"] = True
            except ValueError:
                seen["reentrant"] = False
            launches = self.session / "launches"
            launches.mkdir(exist_ok=True)
            log = launches / str(len(list(launches.iterdir())))
            log.mkdir()
            (log / "launch-result.json").write_text(json.dumps(self.receipt))
            return 0

        with patch.object(W.launcher, "main", side_effect=fake_game):
            self.receipt = {"exit_code": 0, "stopped_by_launcher": False,
                            "clean_exit": True, "child_pid": 4248}
            self.run_wrapper()
        self.assertEqual(seen.get("reentrant"), False,
                         "the session lock must exclude a concurrent game")


if __name__ == "__main__":
    unittest.main(verbosity=2)
