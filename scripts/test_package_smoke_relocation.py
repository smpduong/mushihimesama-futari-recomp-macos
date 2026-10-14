#!/usr/bin/env python3
"""Synthetic regressions for post-copy package identity verification."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_PATH = Path(__file__).with_name("package_smoke.py")
spec = importlib.util.spec_from_file_location("package_smoke", SCRIPT_PATH)
package_smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package_smoke
spec.loader.exec_module(package_smoke)


def synthetic_identity(app):
    payload = app / "Contents" / "payload.bin"
    receipt = Path(str(app) + ".release-receipt.json")
    return {
        "payload_sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
    }


class PackageSmokeRelocationTests(unittest.TestCase):
    def make_candidate(self, root):
        app = root / "Futari Personal.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "payload.bin").write_bytes(b"signed app payload")
        Path(str(app) + ".release-receipt.json").write_bytes(b"external signed identity")
        return app

    def test_relocation_copies_receipt_and_revalidates_identity(self):
        with tempfile.TemporaryDirectory(prefix="futari-relocation-identity-") as td:
            root = Path(td)
            source = self.make_candidate(root / "source")
            relocated = root / "trial" / source.name
            expected = synthetic_identity(source)
            with mock.patch.object(package_smoke, "validate_candidate", side_effect=synthetic_identity):
                actual = package_smoke.copy_validated_relocation(source, relocated, expected)
            self.assertEqual(actual, expected)
            self.assertEqual((relocated / "Contents" / "payload.bin").read_bytes(),
                             (source / "Contents" / "payload.bin").read_bytes())
            self.assertEqual(Path(str(relocated) + ".release-receipt.json").read_bytes(),
                             Path(str(source) + ".release-receipt.json").read_bytes())

    def test_relocation_rejects_changed_copy(self):
        with tempfile.TemporaryDirectory(prefix="futari-relocation-tamper-") as td:
            root = Path(td)
            source = self.make_candidate(root / "source")
            relocated = root / "trial" / source.name
            expected = synthetic_identity(source)
            def altered_copy(src, dst):
                Path(dst, "Contents").mkdir(parents=True)
                (Path(dst) / "Contents" / "payload.bin").write_bytes(b"altered")

            with mock.patch.object(package_smoke.shutil, "copytree", side_effect=altered_copy), \
                    mock.patch.object(package_smoke, "validate_candidate", side_effect=synthetic_identity):
                with self.assertRaisesRegex(ValueError, "differs from the validated source"):
                    package_smoke.copy_validated_relocation(source, relocated, expected)

    def test_relocation_requires_receipt_compatible_bundle_name(self):
        with tempfile.TemporaryDirectory(prefix="futari-relocation-name-") as td:
            root = Path(td)
            source = self.make_candidate(root / "source")
            relocated = root / "trial" / "Different Name.app"
            with self.assertRaisesRegex(ValueError, "retain its bundle name"):
                package_smoke.copy_validated_relocation(source, relocated, {})

    def test_symlinked_temp_ancestor_is_resolved_before_copy_and_profile_paths(self):
        with tempfile.TemporaryDirectory(prefix="futari-relocation-temp-alias-") as td:
            root = Path(td)
            real = root / "private" / "var" / "trial-root"
            real.mkdir(parents=True)
            alias = root / "var-alias"
            alias.symlink_to(real, target_is_directory=True)
            (real / "trial").mkdir()
            source_root = root / "source"
            source_root.mkdir()
            source = self.make_candidate(source_root)

            trial, relocated, user_data = package_smoke.canonical_trial_paths(
                alias / "trial", source.name)
            expected = synthetic_identity(source)
            with mock.patch.object(package_smoke, "validate_candidate", side_effect=synthetic_identity):
                actual = package_smoke.copy_validated_relocation(source, relocated, expected)

            self.assertEqual(actual, expected)
            canonical_real = real.resolve(strict=True)
            self.assertEqual(relocated, (canonical_real / "trial" / source.name))
            self.assertEqual(user_data, canonical_real / "trial" / "UserData")
            self.assertNotIn("var-alias", str(relocated))
            self.assertNotIn("var-alias", str(user_data))
            self.assertTrue(relocated.is_dir())
            self.assertEqual(Path(str(relocated) + ".release-receipt.json").read_bytes(),
                             Path(str(source) + ".release-receipt.json").read_bytes())

    def test_marker_capture_accepts_paths_with_spaces_and_retains_after_disappearance(self):
        with tempfile.TemporaryDirectory(prefix="futari-marker-with-space-") as td:
            logs = Path(td) / "Logs"
            logs.mkdir()
            native_log = logs / "session file.log"
            console = logs / "session-console.log"
            console.write_text(
                f"FUTARI_DIAGNOSTIC_CHILD_STARTED pid=431 content=base log={native_log} "
                "(process start only; not guest-init proof)\n")
            records = package_smoke.observe_child_start_markers(logs)
            self.assertEqual(records[0]["log"], str(native_log))
            retained, conflict = package_smoke.retain_child_start_marker(None, records)
            self.assertFalse(conflict)
            console.write_text("dyld output replaced/obscured the old marker\n")
            retained_again, conflict = package_smoke.retain_child_start_marker(
                retained, package_smoke.observe_child_start_markers(logs))
            self.assertFalse(conflict)
            self.assertEqual(retained_again, retained)

    def test_marker_capture_accepts_canonical_path_without_spaces(self):
        with tempfile.TemporaryDirectory(prefix="futari-marker-") as td:
            logs = Path(td) / "Logs"
            logs.mkdir()
            native_log = logs / "session.log"
            (logs / "session-console.log").write_text(
                f"FUTARI_DIAGNOSTIC_CHILD_STARTED pid=432 content=both log={native_log} "
                "(process start only; not guest-init proof)\n")
            self.assertEqual(package_smoke.observe_child_start_markers(logs)[0]["log"],
                             str(native_log))

    def test_title_preparation_survives_numbered_log_rotation(self):
        with tempfile.TemporaryDirectory(prefix="futari-log-rotation-") as td:
            user_data = Path(td) / "UserData"
            logs = user_data / "Logs"
            logs.mkdir(parents=True)
            base = logs / "session.log"
            rotated = logs / "session.1.log"
            base.write_text("current logger output without title marker\n")
            rotated.write_text("Initializing shader storage for title 435A07D7...\n")
            self.assertEqual(package_smoke.title_preparation_log(base, user_data), rotated.resolve())

    def test_title_preparation_ignores_symlinked_log_rotation(self):
        with tempfile.TemporaryDirectory(prefix="futari-log-rotation-link-") as td:
            root = Path(td)
            user_data = root / "UserData"
            logs = user_data / "Logs"
            logs.mkdir(parents=True)
            outside = root / "outside.log"
            outside.write_text("Initializing shader storage for title 435A07D7...\n")
            base = logs / "session.log"
            base.write_text("no title marker\n")
            (logs / "session.1.log").symlink_to(outside)
            self.assertIsNone(package_smoke.title_preparation_log(base, user_data))

    def test_log_reader_tolerates_user_data_created_after_launcher_start(self):
        with tempfile.TemporaryDirectory(prefix="futari-log-delayed-root-") as td:
            user_data = Path(td) / "UserData"
            offsets = {}
            self.assertEqual(package_smoke.read_new_log_text(user_data, offsets), "")
            user_data.mkdir()
            self.assertEqual(package_smoke.read_new_log_text(user_data, offsets), "")
            logs = user_data / "Logs"
            logs.mkdir()
            log = logs / "session.log"
            log.write_text("FUTARI_DIAGNOSTIC_CHILD_STARTED pid=433\n")
            self.assertEqual(package_smoke.read_new_log_text(user_data, offsets),
                             "FUTARI_DIAGNOSTIC_CHILD_STARTED pid=433\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
