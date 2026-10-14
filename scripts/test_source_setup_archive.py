"""Synthetic tests for the explicit public source archive allowlist."""
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "create_source_setup_archive", ROOT / "scripts/create_source_setup_archive.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SourceSetupArchiveTests(unittest.TestCase):
    def make_fixture(self, root):
        for relative in module.ALLOWLIST:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic source: " + relative, encoding="utf-8")

    def test_archive_contains_exact_allowlist_and_no_unlisted_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            self.make_fixture(root)
            asset = root / "game-work/game-data/default.xex"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"synthetic executable")
            archive_path = Path(temporary) / "source.tar.gz"
            module.create_archive(archive_path, root)
            with tarfile.open(archive_path, "r:gz") as archive:
                members = {member.name for member in archive.getmembers()}
                manifest = json.load(archive.extractfile(f"{module.PREFIX}/SOURCE-MANIFEST.json"))
            expected = {f"{module.PREFIX}/{name}" for name in module.ALLOWLIST}
            expected.add(f"{module.PREFIX}/SOURCE-MANIFEST.json")
            self.assertEqual(members, expected)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertIn("BSD-3-Clause", manifest["artifact_status"])
            self.assertNotIn("game-work/game-data/default.xex", str(manifest))

    def test_existing_archive_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            self.make_fixture(root)
            archive = Path(temporary) / "source.tar.gz"
            archive.write_bytes(b"preserve")
            with self.assertRaises(FileExistsError):
                module.create_archive(archive, root)
            self.assertEqual(archive.read_bytes(), b"preserve")

    def test_missing_allowlisted_file_fails_before_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "source.tar.gz"
            with self.assertRaises(ValueError):
                module.create_archive(archive, Path(temporary) / "empty")
            self.assertFalse(archive.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
