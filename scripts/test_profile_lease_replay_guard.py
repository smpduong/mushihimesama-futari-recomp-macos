#!/usr/bin/env python3
"""Native profile-lease tests for interruption-safe replay import refusal."""
import fcntl
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "game-work/futari15-x360/src/futari_profile_lease.h"


class ProfileLeaseReplayGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="futari-profile-lease-contract-",
                                                   dir="/private/tmp")
        cls.binary = Path(cls.temp_dir.name) / "profile-lease-contract"
        source = r'''#include "futari_profile_lease.h"
#include <cstdio>
int main(int argc, char** argv) {
  if (argc != 2 && argc != 3) return 2;
  const std::filesystem::path root(argv[1]);
  const std::string mode = argc == 3 ? argv[2] : "normal";
  if (mode == "retry-after-pending") {
    bool refused = false;
    try { futari::ProfileLease first(root); }
    catch (const std::exception&) { refused = true; }
    if (!refused || !std::filesystem::remove(root / futari::kPendingReplayImportName)) return 4;
    futari::ProfileLease retry(root);
    return retry.held() ? 0 : 5;
  }
  if (mode == "retry-after-busy") {
    int held_fd = open((root / futari::kProfileLockName).c_str(), O_RDWR | O_CREAT, 0600);
    if (held_fd < 0 || flock(held_fd, LOCK_EX | LOCK_NB) != 0) return 6;
    bool refused = false;
    try { futari::ProfileLease first(root); }
    catch (const std::exception&) { refused = true; }
    if (!refused) return 7;
    flock(held_fd, LOCK_UN);
    close(held_fd);
    std::filesystem::remove(root / futari::kPendingReplayImportName);
    futari::ProfileLease retry(root);
    return retry.held() ? 0 : 8;
  }
  try {
    futari::ProfileLease lease(root);
    if (!lease.held()) return 3;
    std::puts("profile lease acquired");
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "%s\n", e.what());
    return 1;
  }
}
'''
        built = subprocess.run(
            ["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(HEADER.parent),
             "-o", str(cls.binary), "-x", "c++", "-"],
            input=source, text=True, capture_output=True, timeout=90)
        if built.returncode:
            cls.temp_dir.cleanup()
            raise RuntimeError(built.stdout + built.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def run_lease(self, root, expected, message=None, mode=None):
        args = [str(self.binary), str(root)]
        if mode:
            args.append(mode)
        result = subprocess.run(args, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        if message:
            self.assertIn(message, result.stderr)
        return result

    def test_clean_profile_is_allowed(self):
        with tempfile.TemporaryDirectory(prefix="futari-clean-profile-", dir="/private/tmp") as td:
            result = self.run_lease(Path(td), 0)
            self.assertIn("profile lease acquired", result.stdout)

    def test_pending_regular_journal_is_refused_under_runtime_lease(self):
        with tempfile.TemporaryDirectory(prefix="futari-pending-profile-", dir="/private/tmp") as td:
            (Path(td) / ".futari-replay-import.pending").write_bytes(
                b'{"schema":"futari-replay-import-transaction-1"}')
            self.run_lease(Path(td), 1, "Replay import recovery is pending or unsafe")

    def test_pending_refusal_releases_runtime_lock_descriptor(self):
        with tempfile.TemporaryDirectory(prefix="futari-pending-release-", dir="/private/tmp") as td:
            root = Path(td)
            (root / ".futari-replay-import.pending").write_bytes(b"pending")
            self.run_lease(root, 0, mode="retry-after-pending")

    def test_pending_symlink_is_refused_without_following_it(self):
        with tempfile.TemporaryDirectory(prefix="futari-pending-link-", dir="/private/tmp") as td:
            root = Path(td)
            target = root / "outside-journal"
            target.write_text("corrupt journal")
            (root / ".futari-replay-import.pending").symlink_to(target)
            self.run_lease(root, 1, "Replay import recovery is pending or unsafe")

    def test_corrupt_journal_and_unknown_stage_entries_still_refuse(self):
        with tempfile.TemporaryDirectory(prefix="futari-ambiguous-profile-", dir="/private/tmp") as td:
            root = Path(td)
            (root / ".futari-replay-import.pending").write_bytes(b"not valid json")
            stage = root / ".futari-replay-import-staging" / (
                ".replay-import-" + "a" * 32)
            stage.mkdir(parents=True)
            (stage / "unexpected-entry").write_bytes(b"ambiguous")
            self.run_lease(root, 1, "Replay import recovery is pending or unsafe")

    def test_busy_profile_lock_is_refused_before_profile_open(self):
        with tempfile.TemporaryDirectory(prefix="futari-busy-profile-", dir="/private/tmp") as td:
            root = Path(td)
            lock_path = root / ".futari-profile.lock"
            (root / ".futari-replay-import.pending").write_bytes(b"pending")
            with lock_path.open("w") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.run_lease(root, 1, "Profile is already in use")
            self.run_lease(root, 0, mode="retry-after-busy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
