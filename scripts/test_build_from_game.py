"""Synthetic path and manifest tests for the source-only build launcher."""
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock
import subprocess

from scripts import build_from_game as build


class BuildFromGameTests(unittest.TestCase):
    def test_unmocked_submodule_status_preserves_leading_clean_state(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            dependency = root / "sdk dependency with spaces"
            sdk = root / "sdk checkout with spaces"

            def git(*args):
                subprocess.run(["git", *map(str, args)], check=True, capture_output=True)

            git("init", "-q", dependency)
            (dependency / "README.md").write_text("synthetic dependency\n", encoding="utf-8")
            git("-C", dependency, "add", "README.md")
            git("-C", dependency, "-c", "user.name=Fixture", "-c",
                "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
            git("init", "-q", sdk)
            git("-C", sdk, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                dependency, "thirdparty/fixture")
            git("-C", sdk, "-c", "user.name=Fixture", "-c",
                "user.email=fixture@example.invalid", "commit", "-qm", "fixture")

            captured = build.capture(["git", "submodule", "status", "--recursive"], sdk)
            self.assertTrue(captured.startswith(" "), captured)
            parsed = build.parse_submodule_status(captured)
            self.assertEqual(parsed[0][0], " ")
            self.assertEqual(parsed[0][2], "thirdparty/fixture")

    def test_recipe_identity_is_stable_and_detects_critical_source_change(self):
        import shutil
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            copied = Path(temporary) / "repo"
            copied.mkdir()
            for relative in build.RECIPE_FILES:
                target = copied / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(build.REPO / relative, target)
            first = build.recipe_identity(copied)
            self.assertEqual(first, build.recipe_identity(copied))
            changed = copied / "config/futari15x360.json"
            changed.write_bytes(changed.read_bytes() + b" ")
            self.assertNotEqual(first["files_sha256"], build.recipe_identity(copied)["files_sha256"])

    def test_path_overlap_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "repo with spaces"
            game = parent / "game"
            game.mkdir(parents=True)
            original = build.REPO
            try:
                build.REPO = parent
                with self.assertRaises(build.BuildError):
                    build.check_separate_roots(game, Path(temporary) / "output")
            finally:
                build.REPO = original

    def test_game_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            game = root / "game"
            (game / "media/Module").mkdir(parents=True)
            (game / "default.xex").write_bytes(b"synthetic")
            (game / "media/Module/reco_normal.xex").symlink_to(game / "default.xex")
            for name in build.MODULES[1:]:
                (game / "media/Module" / name).write_bytes(b"synthetic")
            with self.assertRaisesRegex(build.BuildError, "symlinked game inputs"):
                build.validate_game(game)

    def test_missing_and_wrong_revision_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            game = Path(temporary) / "game"
            (game / "media/Module").mkdir(parents=True)
            with self.assertRaisesRegex(build.BuildError, "missing required"):
                build.validate_game(game)
            (game / "default.xex").write_bytes(b"synthetic default")
            for name in build.MODULES:
                (game / "media/Module" / name).write_bytes(b"synthetic module")
            with self.assertRaisesRegex(build.BuildError, "unsupported game revision"):
                build.validate_game(game)

    def test_build_root_requires_owned_marker_to_resume(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary) / "output"
            game = Path(temporary) / "game"
            game.mkdir()
            input_file = game / "default.xex"
            input_file.write_bytes(b"synthetic-input")
            inputs = {"default.xex": input_file}
            with self.assertRaises(build.BuildError):
                build.ensure_build_root(root, resume=True, game=game)
            with mock.patch.object(build, "validate_game", return_value=inputs):
                build.ensure_build_root(root, resume=False, game=game)
                self.assertEqual(build.ensure_build_root(root, resume=True, game=game), root.resolve())
            (root / build.MARKER).write_text("{}", encoding="utf-8")
            with mock.patch.object(build, "validate_game", return_value=inputs):
                with self.assertRaises(build.BuildError):
                    build.ensure_build_root(root, resume=True, game=game)

    def test_raw_symlinked_parent_is_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            actual_parent = root / "actual"
            actual_parent.mkdir()
            alias = root / "alias"
            alias.symlink_to(actual_parent, target_is_directory=True)
            game = actual_parent / "game"
            game.mkdir()
            with self.assertRaisesRegex(build.BuildError, "traverse a symlink"):
                build.check_separate_roots(alias / "game", root / "build")

    def test_config_rejects_unknown_fields_and_bad_numeric_types(self):
        import json
        original = json.loads((build.REPO / "config" / "futari15x360.json").read_text(encoding="utf-8"))
        config = dict(original)
        config["unexpected"] = True
        with self.assertRaisesRegex(build.BuildError, "unknown or missing"):
            build.validate_public_config(config)
        config = json.loads(json.dumps(original))
        config["modules"][0]["switch_tables"][0]["register"] = True
        with self.assertRaisesRegex(build.BuildError, "register"):
            build.validate_public_config(config)

    def test_generated_manifest_is_structured_and_has_no_decomp_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "output with spaces"
            root.mkdir()
            project = root / "project"
            project.mkdir()
            game = Path(temporary) / "game files"
            manifest_path = build.render_manifest(game, project)
            parsed = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(parsed["entrypoint"]["functions"]), 56)
            self.assertEqual(len(parsed["modules"]), 8)
            self.assertEqual(sum(len(item["functions"]) for item in parsed["modules"]), 90)
            self.assertEqual(sum(len(item["switch_tables"]) for item in parsed["modules"]), 8)
            self.assertNotIn("decomp", manifest_path.read_text(encoding="utf-8").lower())
            self.assertIn("game files", parsed["entrypoint"]["file_path"])

    def test_project_generation_is_idempotent_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build root"
            root.mkdir()
            game = Path(temporary) / "game files"
            project, manifest = build.write_project(game, root)
            first = manifest.read_bytes()
            build.write_project(game, root)
            self.assertEqual(manifest.read_bytes(), first)
            (project / "CMakeLists.txt").write_text("user data", encoding="utf-8")
            with self.assertRaisesRegex(build.BuildError, "refusing to overwrite"):
                build.write_project(game, root)

    def test_plan_is_deterministic_and_has_no_private_override_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build root"
            game = Path(temporary) / "game files"
            first = build.plan(root, game, 2)
            second = build.plan(root, game, 2)
            self.assertEqual(first, second)
            rendered = "\n".join(" ".join(argv) for _, argv in first)
            self.assertIn(build.SDK_REVISION, rendered)
            self.assertIn("rexglue-sdk-current.patch", rendered)
            self.assertNotIn("decomp_sources.cmake", rendered)
            commands = [argv for _, argv in first]
            codegen_index = next(i for i, argv in enumerate(commands) if f"{build.APP_NAME}_codegen" in argv)
            configure_indexes = [i for i, argv in enumerate(commands) if argv[:2] == ["cmake", "-S"]]
            self.assertEqual(len(configure_indexes), 2)
            self.assertLess(codegen_index, configure_indexes[1])
            self.assertIn("-DCMAKE_BUILD_TYPE=Debug", commands[configure_indexes[0]])
            self.assertTrue(any("cache/CPM" in arg for arg in commands[configure_indexes[0]]))
            self.assertTrue(any("cache/CPM" in arg for argv in commands[:codegen_index] for arg in argv))

    def test_resume_parser_accepts_only_verified_patched_sdk_state(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            sdk = root / "sdk"
            (sdk / ".git").mkdir(parents=True)
            for relative in ("out/mac-arm64/Debug/rexglued",
                             "out/mac-arm64/Debug/librexruntimed.dylib",
                             "out/mac-arm64/Debug/librexcodegend.a"):
                artifact = sdk / relative
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(b"synthetic-sdk-output")
            expected_files = set(build.re.findall(
                r"^diff --git a/(.+?) b/", build.SDK_PATCH.read_text(encoding="utf-8"), build.re.M))
            submodules = []
            for line in (build.REPO / "patches/sdk-submodules.txt").read_text(encoding="utf-8").splitlines():
                fields = line.strip().lstrip("+").split()
                if len(fields) >= 2:
                    submodules.append(f" {fields[0]} {fields[1]}")
            submodules[0] += " (heads/main)"

            def fake_capture(argv, cwd):
                if argv[1:3] == ["-C", str(sdk / "thirdparty")]:
                    return ""
                if len(argv) > 2 and argv[1] == "-C":
                    return ""
                if argv[1:3] == ["rev-parse", "HEAD"]:
                    return build.SDK_REVISION
                if argv[1:4] == ["remote", "get-url", "origin"]:
                    return build.SDK_URL
                if argv[1:4] == ["diff", "--cached", "--name-only"]:
                    return "\n".join(sorted(expected_files))
                if argv[1:3] == ["diff", "--name-only"] or argv[1:3] == ["ls-files", "--others"]:
                    return ""
                if argv[1:4] == ["submodule", "status", "--recursive"]:
                    return "\n".join(submodules)
                self.fail(f"unexpected validation command: {argv}")

            patch_bytes = build.SDK_PATCH.read_bytes()
            def fake_run(argv, **kwargs):
                if argv[1:4] == ["apply", "--reverse", "--check"]:
                    return subprocess.CompletedProcess(argv, 0)
                if argv[1:5] == ["diff", "--cached", "HEAD", "--binary"]:
                    return subprocess.CompletedProcess(argv, 0, stdout=patch_bytes, stderr=b"")
                self.fail(f"unexpected subprocess command: {argv}")

            with mock.patch.object(build, "capture", side_effect=fake_capture), \
                    mock.patch.object(build.subprocess, "run", side_effect=fake_run):
                commands = build.sdk_setup_commands(root, 2)
            rendered = "\n".join(" ".join(argv) for _, argv in commands)
            self.assertNotIn("clone", rendered)
            self.assertNotIn("apply", rendered)
            self.assertNotIn("submodule update", rendered)
            self.assertNotIn("cmake", rendered)

    def test_expected_output_manifest_is_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            build_root = Path(temporary)
            for path in build.expected_artifacts(build_root):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic output")
            build.verify_artifacts(build_root)
            build.expected_artifacts(build_root)[-1].unlink()
            with self.assertRaisesRegex(build.BuildError, "required outputs"):
                build.verify_artifacts(build_root)

    def test_internal_artifact_symlink_is_allowed_but_escape_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_dir = root / "build"
            for path in build.expected_artifacts(build_dir):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic output")
            artifact = build.expected_artifacts(build_dir)[0]
            backing = root / "sdk" / "futari15x360"
            backing.parent.mkdir()
            backing.write_bytes(b"synthetic output")
            artifact.unlink()
            artifact.symlink_to(backing)
            build.verify_artifacts(build_dir)
            outside = Path(temporary).parent / "outside-synthetic-artifact"
            try:
                outside.write_bytes(b"synthetic output")
                artifact.unlink()
                artifact.symlink_to(outside)
                with self.assertRaisesRegex(build.BuildError, "required outputs"):
                    build.verify_artifacts(build_dir)
            finally:
                outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
