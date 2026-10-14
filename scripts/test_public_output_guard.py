"""Synthetic path-only checks for external package/build outputs."""
from pathlib import Path
import tempfile
import unittest

from scripts import build_from_game as B
from scripts import public_output_guard as G

# Resolve the platform-selected temp path before applying symlink safety checks.
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


class PublicOutputGuardTests(unittest.TestCase):
    def test_temp_root_is_platform_selected_and_resolved(self):
        self.assertTrue(TEMP_ROOT.is_dir())
        self.assertEqual(TEMP_ROOT, Path(tempfile.gettempdir()).resolve())

    def test_external_sibling_output_is_allowed(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            build_root = root / "build-root"
            sdk = build_root / "sdk"
            build = build_root / "build"
            packages = root / "external-packaging/packages"
            sdk.mkdir(parents=True)
            build.mkdir()
            self.assertEqual(G.validate_output_path(packages, [build_root]), packages)

    def test_outputs_inside_marker_owned_build_root_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            build_root = Path(temporary) / "build-root"
            (build_root / "sdk").mkdir(parents=True)
            (build_root / "build").mkdir()
            for output in (build_root / "profiles", build_root / "app/Futari.app",
                           build_root / "dlc-install"):
                with self.subTest(output=output), self.assertRaisesRegex(B.BuildError, "overlaps"):
                    G.validate_output_path(output, [build_root])

    def test_output_under_git_worktree_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            repo = Path(temporary) / "repo with spaces"
            (repo / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(B.BuildError, "overlaps"):
                G.validate_output_path(repo / "release", [])

    def test_linked_worktree_git_file_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            repo = Path(temporary) / "linked worktree"
            repo.mkdir()
            (repo / ".git").write_text("gitdir: /synthetic/gitdir\n", encoding="utf-8")
            with self.assertRaisesRegex(B.BuildError, "overlaps"):
                G.validate_output_path(repo / "release", [])

    def test_symlinked_output_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(B.BuildError, "symlink"):
                G.validate_output_path(alias / "release", [])

    def test_output_may_not_overlap_build_or_package_input_directory(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            root = Path(temporary)
            build = root / "build"
            build.mkdir()
            package = root / "packages/package.stfs"
            package.parent.mkdir()
            package.write_bytes(b"synthetic package marker")
            protected = [build, *G.package_input_paths([package])]
            for output in (build / "app", package.parent / "profile-output"):
                with self.subTest(output=output), self.assertRaises(B.BuildError):
                    G.validate_output_path(output, protected)

    def test_existing_output_requires_explicit_directory_mode(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary:
            output = Path(temporary) / "already-there"
            output.mkdir()
            with self.assertRaisesRegex(B.BuildError, "already exists"):
                G.validate_output_path(output, [])
            self.assertEqual(G.validate_output_path(output, [], allow_existing_directory=True), output)


if __name__ == "__main__":
    unittest.main()
