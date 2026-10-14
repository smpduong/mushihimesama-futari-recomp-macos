#!/usr/bin/env python3
"""Synthetic contract/security checks for the native replay bundle helper.

These fixtures prove archive/file-transfer behavior only, never native replay
recording, in-game listing, playback, or gameplay.

Interruption contract: one immutable `<USER_DATA>/.futari-replay-import.pending`
journal (`futari-replay-import-transaction-1`) binds content/build/package,
archive hash, full size/hash/device/inode roster, and a nonce-derived private
stage under `<USER_DATA>/.futari-replay-import-staging/`. The helper fsyncs the
complete stage and a private journal file, atomically publishes that file with
no replacement, and fsyncs both journal parents before public changes. It then
publishes the package by exclusive rename, then the header by exclusive
hardlink, verifies and re-syncs both public parents, cleans only the exact
owned private stage, and removes the journal last.
Recovery infers progress from identities and hashes and only moves forward;
malformed/torn journals, mismatches, and conflicting paths are refused intact.
An unjournaled pre-intent stage is inert and is never guessed or deleted.
Tests exercise process death at each boundary, not power loss or storage-device
cache behavior. flock is cooperative and does not stop an arbitrary same-UID
process racing path operations; inode/hash checks cover observed substitutions,
but inode reuse cannot be reproduced deterministically here.
"""
import hashlib
import json
import os
import fcntl
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

import replay_library as R

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/replay_bundle.mm"
HEADER = ROOT / "scripts/libarchive_api_subset.h"


@unittest.skipUnless(sys.platform == "darwin", "native libarchive helper requires macOS")
class NativeReplayBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_tmp = tempfile.TemporaryDirectory(prefix="futari-native-replay-build-")
        cls.binary = Path(cls.build_tmp.name) / "replay_bundle"
        cls.production_binary = Path(cls.build_tmp.name) / "replay_bundle-production"
        command = ["/usr/bin/clang++", "-std=c++17", "-fobjc-arc", "-DFUTARI_REPLAY_TEST_FAULTS=1",
                   "-framework", "Foundation",
                   "-I", str(SOURCE.parent), str(SOURCE), "-larchive", "-o", str(cls.binary)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("native replay helper build failed:\n" + result.stdout + result.stderr)
        production_command = ["/usr/bin/clang++", "-std=c++17", "-fobjc-arc", "-framework", "Foundation",
                              "-I", str(SOURCE.parent), str(SOURCE), "-larchive", "-o",
                              str(cls.production_binary)]
        production_result = subprocess.run(production_command, capture_output=True, text=True, timeout=120)
        if production_result.returncode:
            raise RuntimeError("production-path replay helper build failed:\n" +
                               production_result.stdout + production_result.stderr)
        cls.addClassCleanup(cls.build_tmp.cleanup)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="futari-native-replay-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / "source-user-data"
        self.package = "replay0001"
        self.build_hash = "a" * 64
        self.content = "both"
        self.payload = b"synthetic native replay bytes; not gameplay"
        title = self.data / R.PROFILE / R.TITLE
        replay_dir = title / "00000001" / self.package
        replay_dir.mkdir(parents=True)
        (replay_dir / "replaydata.bin").write_bytes(self.payload)
        header = bytearray(0x148)
        header[4:8] = (1).to_bytes(4, "big")
        header[0x108:0x108 + len(self.package)] = self.package.encode("ascii")
        header[0x138:0x140] = int(R.PROFILE, 16).to_bytes(8, "big")
        header[0x140:0x144] = int(R.TITLE, 16).to_bytes(4, "big")
        header_path = title / "Headers" / "00000001" / (self.package + ".header")
        header_path.parent.mkdir(parents=True)
        header_path.write_bytes(header)
        self.header = bytes(header)
        self.exchange = self.root / "exchange"

    def run_helper(self, *args, expected=0, fault=None, timeout=30, binary=None):
        env = os.environ.copy()
        env.pop("FUTARI_REPLAY_TEST_FAULT", None)
        if fault is not None:
            env["FUTARI_REPLAY_TEST_FAULT"] = fault
        result = subprocess.run([str(binary or self.binary), *map(str, args)], capture_output=True, env=env,
                                text=True, timeout=timeout)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def pending_journal(self, root):
        return Path(root) / ".futari-replay-import.pending"

    def replace_journal_scalar(self, raw, key, replacement):
        marker = ('"' + key + '"').encode("ascii")
        start = raw.index(marker) + len(marker)
        start = raw.index(b":", start) + 1
        while raw[start:start + 1] in (b" ", b"\t"):
            start += 1
        end = start
        while raw[end:end + 1] not in (b",", b"}", b"\n"):
            end += 1
        return raw[:start] + replacement + raw[end:]

    def public_paths(self, root):
        root = Path(root)
        return (root / R.PROFILE / R.TITLE / "00000001" / self.package,
                root / R.PROFILE / R.TITLE / "Headers" / "00000001" / (self.package + ".header"))

    def add_unrelated_save(self, root):
        root = Path(root)
        package = root / R.PROFILE / R.TITLE / "00000001" / "unrelated-slot"
        package.mkdir(parents=True)
        payload = package / "user.bin"
        payload.write_bytes(b"unrelated user's save; preserve bytes and inode")
        header = root / R.PROFILE / R.TITLE / "Headers" / "00000001" / "unrelated-slot.header"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_bytes(b"unrelated header")
        return [(path, path.read_bytes(), path.stat().st_ino) for path in (payload, header)]

    def assert_preserved(self, snapshots):
        for path, content, inode in snapshots:
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_ino, inode)

    def export(self):
        destination = self.exchange / "Exports"
        self.run_helper("--export", self.data, destination, self.content, self.build_hash)
        archives = sorted(destination.glob("*.futari-replay"))
        self.assertEqual(len(archives), 1)
        return archives[0]

    def assert_no_import_payload(self, root):
        root = Path(root)
        self.assertFalse((root / R.PROFILE / R.TITLE / "00000001" / self.package).exists())
        self.assertFalse((root / R.PROFILE / R.TITLE / "Headers" / "00000001" /
                          (self.package + ".header")).exists())
        if (root / R.PROFILE / R.TITLE / "00000001").exists():
            self.assertEqual(list((root / R.PROFILE / R.TITLE / "00000001").glob(".replay-import-*")), [])
        staging = root / ".futari-replay-import-staging"
        if staging.exists():
            self.assertEqual(list(staging.glob(".replay-import-*")), [])

    def python_bundle(self):
        library = R.Library(self.root / "python-library")
        self.assertNotIn("not exported", library.export(self.data, self.content, self.build_hash)[0][1])
        return next((library.root / "Exports").glob("*.futari-replay"))

    def test_native_export_import_interoperates_with_existing_python_contract(self):
        archive = self.export()
        library = R.Library(self.root / "validation-library")
        manifest, files = library.read(archive, self.content, self.build_hash)
        self.assertEqual(manifest["package"], self.package)
        self.assertEqual(files["payload/replaydata.bin"], self.payload)
        target = self.root / "imported-user-data"
        self.run_helper("--import", archive, target, self.content, self.build_hash)
        self.assertEqual(R.collect_package(self.data, self.package), R.collect_package(target, self.package))

    def test_native_import_reads_python_archive_and_rejects_build_or_content_mismatch(self):
        archive = self.python_bundle()
        target = self.root / "imported-user-data"
        self.run_helper("--import", archive, target, self.content, self.build_hash)
        self.assertEqual(R.collect_package(self.data, self.package), R.collect_package(target, self.package))
        wrong_build = self.root / "wrong-build"
        self.run_helper("--import", archive, wrong_build, self.content, "b" * 64, expected=1)
        self.assert_no_import_payload(wrong_build)
        wrong_content = self.root / "wrong-content"
        self.run_helper("--import", archive, wrong_content, "base", self.build_hash, expected=1)
        self.assert_no_import_payload(wrong_content)

    def test_repeated_identical_import_is_idempotent(self):
        archive = self.python_bundle()
        target = self.root / "idempotent-user-data"
        self.run_helper("--import", archive, target, self.content, self.build_hash)
        result = self.run_helper("--import", archive, target, self.content, self.build_hash)
        self.assertIn("already present", result.stdout)

    def test_interrupted_import_recovers_forward_at_every_durable_boundary(self):
        archive = self.python_bundle()
        for boundary in ("prepared", "journal_published", "package_renamed", "package_published",
                         "header_linked", "header_published", "stage_cleanup", "completed_cleanup"):
            with self.subTest(boundary=boundary):
                target = self.root / ("recovery-" + boundary)
                snapshots = self.add_unrelated_save(target)
                result = self.run_helper("--import", archive, target, self.content, self.build_hash,
                                         expected=86, fault=boundary)
                self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
                package_path, header_path = self.public_paths(target)
                if boundary in ("prepared", "journal_published"):
                    self.assertFalse(package_path.exists())
                    self.assertFalse(header_path.exists())
                elif boundary in ("package_renamed", "package_published"):
                    self.assertTrue(package_path.is_dir())
                    self.assertFalse(header_path.exists())
                else:
                    self.assertTrue(package_path.is_dir())
                    self.assertTrue(header_path.is_file())
                self.assert_preserved(snapshots)
                self.run_helper("--recover", target, self.content, self.build_hash)
                self.assertEqual(R.collect_package(self.data, self.package),
                                 R.collect_package(target, self.package))
                self.assertFalse(self.pending_journal(target).exists())
                self.assert_preserved(snapshots)
                self.run_helper("--recover", target, self.content, self.build_hash)
                self.assertIn("already present", self.run_helper(
                    "--import", archive, target, self.content, self.build_hash).stdout)
                self.assert_preserved(snapshots)

    def test_environment_cannot_enable_faults_without_compile_time_flag(self):
        archive = self.python_bundle()
        target = self.root / "production-fault-disabled"
        self.run_helper("--import", archive, target, self.content, self.build_hash,
                        fault="prepared", binary=self.production_binary)
        self.assertFalse(self.pending_journal(target).exists())
        self.assertTrue(self.public_paths(target)[0].is_dir())
        self.assertTrue(self.public_paths(target)[1].is_file())

    def test_death_before_journal_leaves_only_inert_hidden_staging(self):
        archive = self.python_bundle()
        for boundary in ("staged_before_journal", "journal_staged"):
            with self.subTest(boundary=boundary):
                target = self.root / ("orphan-private-stage-" + boundary)
                snapshots = self.add_unrelated_save(target)
                result = self.run_helper("--import", archive, target, self.content, self.build_hash,
                                         expected=86, fault=boundary)
                self.assertEqual(result.returncode, 86)
                self.assertFalse(self.pending_journal(target).exists())
                self.assertFalse(self.public_paths(target)[0].exists())
                self.assertFalse(self.public_paths(target)[1].exists())
                staging_root = target / ".futari-replay-import-staging"
                orphan = next(staging_root.glob(".replay-import-*"))
                self.assertTrue((orphan / "package" / "replaydata.bin").is_file())
                self.assertEqual((orphan / "journal").exists(), boundary == "journal_staged")
                self.run_helper("--recover", target, self.content, self.build_hash)
                self.assertTrue(orphan.exists(), "no-journal recovery must leave unowned staging inert")
                self.run_helper("--import", archive, target, self.content, self.build_hash)
                self.assertTrue(orphan.exists(), "ordinary import must not guess ownership of orphan staging")
                self.assert_preserved(snapshots)

    def test_matching_content_hardlinks_are_not_accepted_as_owned_staged_files(self):
        archive = self.python_bundle()
        for member in ("payload", "header"):
            with self.subTest(member=member):
                target = self.root / ("hardlink-" + member)
                self.run_helper("--import", archive, target, self.content, self.build_hash,
                                expected=86, fault="prepared")
                stage = next((target / ".futari-replay-import-staging").glob(".replay-import-*"))
                staged_file = stage / "package" / "replaydata.bin" if member == "payload" else stage / "header"
                external = self.root / ("external-hardlink-" + member)
                if member == "payload":
                    external.write_bytes(self.payload)
                else:
                    external.write_bytes((stage / "header").read_bytes())
                original_inode = external.stat().st_ino
                staged_file.unlink()
                os.link(external, staged_file)
                self.assertEqual(staged_file.stat().st_ino, original_inode)
                self.run_helper("--recover", target, self.content, self.build_hash, expected=1)
                self.assertEqual(external.stat().st_ino, original_inode)
                self.assertTrue(staged_file.exists())
                self.assertFalse(self.public_paths(target)[0].exists())
                self.assertFalse(self.public_paths(target)[1].exists())

    def test_recover_cli_mismatch_leaves_prepared_transaction_untouched(self):
        archive = self.python_bundle()
        target = self.root / "recover-mismatch"
        snapshots = self.add_unrelated_save(target)
        self.run_helper("--import", archive, target, self.content, self.build_hash,
                        expected=86, fault="prepared")
        journal = self.pending_journal(target)
        before = journal.read_bytes()
        self.run_helper("--recover", target, "base", self.build_hash, expected=1)
        self.run_helper("--recover", target, self.content, "b" * 64, expected=1)
        self.assertEqual(journal.read_bytes(), before)
        self.assertFalse(self.public_paths(target)[0].exists())
        self.assertFalse(self.public_paths(target)[1].exists())
        self.assert_preserved(snapshots)
        self.run_helper("--recover", target, self.content, self.build_hash)
        self.assert_preserved(snapshots)

    def test_tampered_journal_and_stage_are_refused_without_cleanup(self):
        archive = self.python_bundle()
        target = self.root / "tampered-journal"
        self.run_helper("--import", archive, target, self.content, self.build_hash,
                        expected=86, fault="prepared")
        journal = self.pending_journal(target)
        staged_root = target / ".futari-replay-import-staging"
        stage = next(staged_root.glob(".replay-import-*"))
        stage_payload = stage / "package" / "replaydata.bin"
        original_journal = journal.read_bytes()
        stage_payload.write_bytes(stage_payload.read_bytes() + b"tampered")
        self.run_helper("--recover", target, self.content, self.build_hash, expected=1)
        self.assertEqual(journal.read_bytes(), original_journal)
        self.assertTrue(stage.exists())
        self.assertFalse(self.public_paths(target)[0].exists())
        self.assertFalse(self.public_paths(target)[1].exists())

        journal.write_bytes(original_journal + b" ")
        self.run_helper("--recover", target, self.content, self.build_hash, expected=1)
        self.assertTrue(stage.exists())
        self.assertFalse(self.public_paths(target)[0].exists())

    def test_nonce_path_and_symlink_destinations_are_refused(self):
        archive = self.python_bundle()
        target = self.root / "tampered-nonce"
        self.run_helper("--import", archive, target, self.content, self.build_hash,
                        expected=86, fault="prepared")
        journal = self.pending_journal(target)
        original = journal.read_bytes()
        data = json.loads(original)
        nonce = data["nonce"]
        replacement = "f" * 32 if nonce != "f" * 32 else "e" * 32
        changed = original.replace(nonce.encode("ascii"), replacement.encode("ascii"), 1)
        self.assertNotEqual(changed, original)
        journal.write_bytes(changed)
        self.run_helper("--recover", target, self.content, self.build_hash, expected=1)
        self.assertEqual(journal.read_bytes(), changed)
        self.assertFalse(self.public_paths(target)[0].exists())
        self.assertFalse(self.public_paths(target)[1].exists())

        symlink_target = self.root / "symlink-public-path"
        snapshots = self.add_unrelated_save(symlink_target)
        self.run_helper("--import", archive, symlink_target, self.content, self.build_hash,
                        expected=86, fault="prepared")
        package_path, header_path = self.public_paths(symlink_target)
        external = self.root / "symlink-external-target"
        external.write_bytes(b"must be preserved")
        header_path.parent.mkdir(parents=True, exist_ok=True)
        header_path.symlink_to(external)
        self.run_helper("--recover", symlink_target, self.content, self.build_hash, expected=1)
        self.assertTrue(header_path.is_symlink())
        self.assertEqual(external.read_bytes(), b"must be preserved")
        self.assertFalse(package_path.exists())
        self.assert_preserved(snapshots)

    def test_journal_boolean_float_and_extra_fields_are_refused(self):
        archive = self.python_bundle()
        for mutation in ("boolean", "float", "extra-field"):
            with self.subTest(mutation=mutation):
                target = self.root / ("journal-type-" + mutation)
                snapshots = self.add_unrelated_save(target)
                self.run_helper("--import", archive, target, self.content, self.build_hash,
                                expected=86, fault="prepared")
                journal = self.pending_journal(target)
                original = journal.read_bytes()
                if mutation == "boolean":
                    changed = self.replace_journal_scalar(original, "stage_inode", b"true")
                elif mutation == "float":
                    value = json.loads(original)["stage_inode"]
                    changed = self.replace_journal_scalar(original, "stage_inode", str(value).encode() + b".5")
                else:
                    changed = original.replace(b"{\n", b'{\n  "extra": 0,\n', 1)
                self.assertNotEqual(changed, original)
                journal.write_bytes(changed)
                self.run_helper("--recover", target, self.content, self.build_hash, expected=1)
                self.assertEqual(journal.read_bytes(), changed)
                self.assertFalse(self.public_paths(target)[0].exists())
                self.assertFalse(self.public_paths(target)[1].exists())
                self.assert_preserved(snapshots)

    def test_published_payload_header_conflicts_and_unknown_stage_entries_are_refused(self):
        archive = self.python_bundle()
        for boundary, mutation in (("package_published", "payload"),
                                   ("header_published", "header"),
                                   ("header_published", "unknown-stage")):
            with self.subTest(boundary=boundary, mutation=mutation):
                target = self.root / f"tampered-{boundary}-{mutation}"
                snapshots = self.add_unrelated_save(target)
                self.run_helper("--import", archive, target, self.content, self.build_hash,
                                expected=86, fault=boundary)
                package_path, header_path = self.public_paths(target)
                stage = next((target / ".futari-replay-import-staging").glob(".replay-import-*"))
                if mutation == "payload":
                    payload = package_path / "replaydata.bin"
                    payload.write_bytes(payload.read_bytes() + b"changed")
                elif mutation == "header":
                    header_path.write_bytes(header_path.read_bytes() + b"changed")
                else:
                    marker = stage / "unowned-marker"
                    marker.write_bytes(b"must not be recursively removed")
                marker_path = stage / "unowned-marker"
                self.run_helper("--recover", target, self.content, self.build_hash, expected=1)
                self.assertTrue(package_path.exists())
                if boundary == "header_published":
                    self.assertTrue(header_path.exists())
                if mutation == "unknown-stage":
                    self.assertEqual(marker_path.read_bytes(), b"must not be recursively removed")
                self.assert_preserved(snapshots)

    def test_conflicting_destination_and_symlink_paths_are_preserved(self):
        archive = self.python_bundle()
        target = self.root / "destination-conflict"
        self.run_helper("--import", archive, target, self.content, self.build_hash,
                        expected=86, fault="prepared")
        package_path, header_path = self.public_paths(target)
        package_path.mkdir()
        sentinel = package_path / "existing.bin"
        sentinel.write_bytes(b"pre-existing conflicting save")
        before = (package_path.stat().st_ino, sentinel.read_bytes(), sentinel.stat().st_ino)
        self.run_helper("--recover", target, self.content, self.build_hash, expected=1)
        self.assertEqual((package_path.stat().st_ino, sentinel.read_bytes(), sentinel.stat().st_ino), before)
        self.assertFalse(header_path.exists())

        symlink_root = self.root / "symlink-journal"
        symlink_root.mkdir()
        elsewhere = self.root / "journal-target"
        elsewhere.write_bytes(b"leave target alone")
        self.pending_journal(symlink_root).symlink_to(elsewhere)
        self.run_helper("--recover", symlink_root, self.content, self.build_hash, expected=1)
        self.assertTrue(self.pending_journal(symlink_root).is_symlink())
        self.assertEqual(elsewhere.read_bytes(), b"leave target alone")

    def test_recover_refuses_busy_profile_lock(self):
        target = self.root / "busy-recovery"
        target.mkdir()
        with (target / ".futari-profile.lock").open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_helper("--recover", target, self.content, self.build_hash, expected=1)

    def test_fifo_journal_and_archive_are_rejected_without_blocking(self):
        journal_root = self.root / "fifo-journal"
        journal_root.mkdir()
        os.mkfifo(self.pending_journal(journal_root))
        self.run_helper("--recover", journal_root, self.content, self.build_hash,
                        expected=1, timeout=3)

        archive_fifo = self.root / "selected-archive.futari-replay"
        os.mkfifo(archive_fifo)
        self.run_helper("--import", archive_fifo, self.root / "fifo-archive-target",
                        self.content, self.build_hash, expected=1, timeout=3)

    def test_export_is_idempotent_without_overwriting_existing_archive(self):
        self.export()
        result = self.run_helper("--export", self.data, self.exchange / "Exports", self.content, self.build_hash)
        self.assertIn("already exported", result.stdout)
        self.assertEqual(len(list((self.exchange / "Exports").glob("*.futari-replay"))), 1)

    def test_export_writes_directly_to_custom_destination_not_nested_exports(self):
        destination = self.root / "chosen-export-folder"
        self.run_helper("--export", self.data, destination, self.content, self.build_hash)
        self.assertEqual(len(list(destination.glob("*.futari-replay"))), 1)
        self.assertFalse((destination / "Exports").exists())

    def test_conflicting_import_never_replaces_existing_save(self):
        archive = self.python_bundle()
        target = self.root / "imported-user-data"
        self.run_helper("--import", archive, target, self.content, self.build_hash)
        payload = target / R.PROFILE / R.TITLE / "00000001" / self.package / "replaydata.bin"
        payload.write_bytes(b"newer save that must stay intact")
        self.run_helper("--import", archive, target, self.content, self.build_hash, expected=1)
        self.assertEqual(payload.read_bytes(), b"newer save that must stay intact")

    def test_traversal_symlink_duplicate_and_bad_checksum_archives_are_rejected(self):
        source = self.python_bundle()
        with zipfile.ZipFile(source) as z:
            good = {name: z.read(name) for name in z.namelist()}

        traversal = self.root / "traversal.futari-replay"
        with zipfile.ZipFile(traversal, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in good.items():
                z.writestr("payload/../escape" if name == "payload/replaydata.bin" else name, data)
        dest = self.root / "traversal-dest"
        self.run_helper("--import", traversal, dest, self.content, self.build_hash, expected=1)
        self.assert_no_import_payload(dest)

        symlink = self.root / "symlink.futari-replay"
        with zipfile.ZipFile(symlink, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in good.items():
                z.writestr(name, data)
            info = zipfile.ZipInfo("payload/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "outside")
        self.run_helper("--import", symlink, self.root / "symlink-dest", self.content,
                        self.build_hash, expected=1)

        duplicate = self.root / "duplicate.futari-replay"
        with zipfile.ZipFile(duplicate, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in good.items():
                z.writestr(name, data)
            z.writestr("HEADER.BIN", good["header.bin"])
        self.run_helper("--import", duplicate, self.root / "duplicate-dest", self.content,
                        self.build_hash, expected=1)

        bad_hash = self.root / "bad-hash.futari-replay"
        changed = dict(good)
        changed["payload/replaydata.bin"] += b"modified"
        with zipfile.ZipFile(bad_hash, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in changed.items():
                z.writestr(name, data)
        self.run_helper("--import", bad_hash, self.root / "bad-hash-dest", self.content,
                        self.build_hash, expected=1)

    def test_archive_symlink_is_rejected_before_read(self):
        archive = self.python_bundle()
        link = self.root / "linked.futari-replay"
        link.symlink_to(archive)
        self.run_helper("--import", link, self.root / "link-dest", self.content,
                        self.build_hash, expected=1)
        self.assert_no_import_payload(self.root / "link-dest")

    def test_existing_empty_target_directory_is_never_replaced(self):
        archive = self.python_bundle()
        target = self.root / "empty-target-user-data"
        package = target / R.PROFILE / R.TITLE / "00000001" / self.package
        package.mkdir(parents=True)
        self.run_helper("--import", archive, target, self.content, self.build_hash, expected=1)
        self.assertTrue(package.is_dir())
        self.assertEqual(list(package.iterdir()), [])

    def test_export_skips_ordinary_systemdata_package_and_continues(self):
        title = self.data / R.PROFILE / R.TITLE
        ordinary = title / "00000001" / "SYSTEMDATA"
        ordinary.mkdir()
        (ordinary / "systemdata.bin").write_bytes(b"ordinary save")
        header = title / "Headers" / "00000001" / "SYSTEMDATA.header"
        header.write_bytes(bytes(0x148))
        archive = self.export()
        library = R.Library(self.root / "systemdata-validation")
        manifest, files = library.read(archive, self.content, self.build_hash)
        self.assertEqual(manifest["package"], self.package)
        self.assertEqual(files["payload/replaydata.bin"], self.payload)

    def test_export_reports_partial_failure_after_exporting_valid_replay(self):
        title = self.data / R.PROFILE / R.TITLE
        package_root = title / "00000001"
        preserved = {}
        for index in range(24):
            name = f"replaybad{index:04d}"
            path = package_root / name
            path.mkdir()
            body = f"preserve invalid package {index}".encode()
            (path / "replaydata.bin").write_bytes(body)
            preserved[path / "replaydata.bin"] = body

        destination = self.exchange / "partial-Exports"
        result = self.run_helper("--export", self.data, destination, self.content,
                                 self.build_hash, expected=1)
        archives = list(destination.glob("*.futari-replay"))
        self.assertEqual(len(archives), 1, result.stdout + result.stderr)
        self.assertIn("Replay export summary:", result.stdout)
        self.assertIn("failed=24", result.stdout)
        self.assertIn("One or more replay packages were not exported", result.stderr)
        for path, body in preserved.items():
            self.assertEqual(path.read_bytes(), body)

    def test_empty_profile_reports_no_replay_payloads_without_success_claim(self):
        empty_data = self.root / "empty-source-user-data"
        destination = self.root / "empty-exports"
        result = self.run_helper("--export", empty_data, destination, self.content,
                                 self.build_hash)
        self.assertIn("No replay payloads found", result.stdout)
        self.assertNotIn("exported", result.stdout.lower())
        self.assertEqual(list(destination.iterdir()), [])

    def test_case_and_unicode_normalization_collisions_are_rejected(self):
        source = self.python_bundle()
        with zipfile.ZipFile(source) as z:
            good = {name: z.read(name) for name in z.namelist()}
        collision = self.root / "normalization-collision.futari-replay"
        with zipfile.ZipFile(collision, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in good.items():
                z.writestr(name, data)
            z.writestr("payload/REPLAYDATA.BIN", good["payload/replaydata.bin"])
            z.writestr("payload/caf\u00e9.bin", b"one")
            z.writestr("payload/cafe\u0301.bin", b"two")
        target = self.root / "collision-user-data"
        self.run_helper("--import", collision, target, self.content, self.build_hash, expected=1)
        self.assert_no_import_payload(target)

    def test_manifest_boolean_size_is_rejected(self):
        source = self.python_bundle()
        with zipfile.ZipFile(source) as z:
            good = {name: z.read(name) for name in z.namelist()}
        manifest = json.loads(good["manifest.json"])
        manifest["files"]["header.bin"]["bytes"] = True
        good["manifest.json"] = json.dumps(manifest, sort_keys=True).encode("utf-8")
        malformed = self.root / "boolean-size.futari-replay"
        with zipfile.ZipFile(malformed, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in good.items():
                z.writestr(name, data)
        target = self.root / "boolean-size-user-data"
        self.run_helper("--import", malformed, target, self.content, self.build_hash, expected=1)
        self.assert_no_import_payload(target)

    def test_nonregular_profile_lock_is_rejected_before_import(self):
        archive = self.python_bundle()
        target = self.root / "nonregular-lock-user-data"
        target.mkdir()
        (target / ".futari-profile.lock").mkdir()
        self.run_helper("--import", archive, target, self.content, self.build_hash, expected=1)
        self.assert_no_import_payload(target)

    def test_replay_archive_over_128_mib_is_still_rejected(self):
        oversized = self.root / "oversized.futari-replay"
        with oversized.open("wb") as f:
            f.truncate(128 * 1024 * 1024 + 1)
        target = self.root / "oversized-archive-user-data"
        self.run_helper("--import", oversized, target, self.content, self.build_hash, expected=1)
        self.assert_no_import_payload(target)

    def test_busy_profile_lock_is_refused_before_import(self):
        archive = self.python_bundle()
        target = self.root / "busy-lock-user-data"
        target.mkdir()
        with (target / ".futari-profile.lock").open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_helper("--import", archive, target, self.content, self.build_hash, expected=1)
            self.assert_no_import_payload(target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
