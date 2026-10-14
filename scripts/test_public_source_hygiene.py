"""Synthetic tests for the fail-closed public source tree scanner."""
import tempfile
from pathlib import Path
import unittest

from scripts import check_public_tree


class PublicSourceHygieneTests(unittest.TestCase):
    def make_tree(self, root):
        for relative in check_public_tree.PUBLIC_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic source\n", encoding="utf-8")

    def test_exact_allowlist_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            self.assertEqual(check_public_tree.scan_tree(root), [])

    def test_unlisted_game_file_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            asset = root / "game-work/game-data/default.xex"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"synthetic executable")
            self.assertIn("unreviewed path: game-work/game-data/default.xex",
                          check_public_tree.scan_tree(root))

    def test_binary_content_fails_even_at_allowlisted_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            (root / "README.md").write_bytes(b"synthetic\0payload")
            self.assertIn("binary content is not allowed: README.md",
                          check_public_tree.scan_tree(root))

    def test_invalid_utf8_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            (root / "README.md").write_bytes(b"invalid utf-8: \xff")
            self.assertIn("invalid UTF-8 text: README.md",
                          check_public_tree.scan_tree(root))

    def test_private_key_material_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            (root / "README.md").write_text(
                "-----BEGIN " + "PRIVATE KEY-----\nfixture\n", encoding="utf-8")
            self.assertIn("possible credential material: README.md",
                          check_public_tree.scan_tree(root))

    def test_lfs_pointer_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            (root / "README.md").write_text(
                "version https://git-lfs.github.com/" + "spec/v1\noid sha256:fixture\n",
                encoding="utf-8")
            self.assertIn("Git LFS pointer is not allowed: README.md",
                          check_public_tree.scan_tree(root))

    def test_large_encoded_payload_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            (root / "README.md").write_text("A" * 1024, encoding="utf-8")
            self.assertIn("large encoded payload is not allowed: README.md",
                          check_public_tree.scan_tree(root))

    def test_symlink_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            linked = root / "README.md"
            linked.unlink()
            linked.symlink_to(root / "SECURITY.md")
            self.assertIn("symlink is not allowed: README.md",
                          check_public_tree.scan_tree(root))

    def test_unlisted_empty_directory_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            (root / "tools/vendor-sdk").mkdir(parents=True)
            self.assertIn("unreviewed path: tools/vendor-sdk",
                          check_public_tree.scan_tree(root))

    def test_machine_path_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_tree(root)
            (root / "README.md").write_text("/Users/" + "user/private/file\n", encoding="utf-8")
            self.assertIn("machine-specific path found: README.md",
                          check_public_tree.scan_tree(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
