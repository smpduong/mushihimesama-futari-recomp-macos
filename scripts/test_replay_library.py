#!/usr/bin/env python3
"""Synthetic filesystem/ABI fixtures: NOT evidence of native replay playback."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import replay_library as R


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.lib = R.Library(self.root / "library")
        self.src = self.root / "source/user-data"
        self.dst = self.root / "destination/user-data"
        self.package = "replay0001"
        self.build = "a" * 64
        title = self.src / R.PROFILE / R.TITLE
        payload = title / "00000001" / self.package / "replaydata.bin"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"synthetic replay bytes, not gameplay")
        header = bytearray(0x148)
        struct.pack_into(">II", header, 0, 1, 1)
        header[8:20] = "Replay".encode("utf-16-be")
        header[0x108:0x108+len(self.package)] = self.package.encode()
        struct.pack_into(">QI", header, 0x138, int(R.PROFILE, 16), int(R.TITLE, 16))
        self.header = title / "Headers/00000001" / (self.package + ".header")
        self.header.parent.mkdir(parents=True)
        self.header.write_bytes(header)

    def bundle(self):
        result = self.lib.export(self.src, "both", self.build)
        self.assertNotIn("not exported", result[0][1])
        return next((self.lib.root / "Exports").glob("*" + R.EXT))

    def modified(self, change):
        archive = self.bundle()
        with zipfile.ZipFile(archive) as z:
            files = {n: z.read(n) for n in z.namelist()}
        change(files)
        target = self.root / "modified.futari-replay"
        with zipfile.ZipFile(target, "w") as z:
            for n, data in files.items():
                z.writestr(n, data)
        return target

    def rejected(self, path, content="both", build=None):
        with self.assertRaises((ValueError, KeyError, zipfile.BadZipFile)):
            self.lib.import_one(path, self.dst, content, build or self.build)
        self.assertFalse(self.dst.exists())

    def test_roundtrip_preserves_every_byte_and_metadata(self):
        self.assertEqual(self.lib.import_one(self.bundle(), self.dst, "both", self.build), "imported")
        self.assertEqual(R.collect_package(self.src, self.package), R.collect_package(self.dst, self.package))

    def test_export_idempotent(self):
        self.bundle()
        self.assertEqual(self.lib.export(self.src, "both", self.build)[0][1], "already exported")
        self.assertEqual(len(list((self.lib.root / "Exports").iterdir())), 1)

    def test_import_idempotent(self):
        archive = self.bundle()
        self.lib.import_one(archive, self.dst, "both", self.build)
        self.assertEqual(self.lib.import_one(archive, self.dst, "both", self.build), "already present")

    def test_conflict_never_overwrites(self):
        archive = self.bundle()
        self.lib.import_one(archive, self.dst, "both", self.build)
        p = self.dst / R.PROFILE / R.TITLE / "00000001" / self.package / "replaydata.bin"
        p.write_bytes(b"keep newer recording")
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.lib.import_one(archive, self.dst, "both", self.build)
        self.assertEqual(p.read_bytes(), b"keep newer recording")

    def test_receipt_prevents_resurrection_after_in_game_delete(self):
        shutil.copyfile(self.bundle(), self.lib.root / "Imports/test.futari-replay")
        self.assertEqual(self.lib.import_pending(self.dst, "both", self.build)[0][1], "imported")
        shutil.rmtree(self.dst)
        self.assertEqual(self.lib.import_pending(self.dst, "both", self.build), [])
        self.assertFalse(self.dst.exists())

    def test_wrong_build(self):
        self.rejected(self.bundle(), build="b" * 64)

    def test_wrong_dlc_profile(self):
        self.rejected(self.bundle(), content="base")

    def test_corrupt_payload(self):
        self.rejected(self.modified(lambda f: f.update({"payload/replaydata.bin": b"damaged"})))

    def test_wrong_title(self):
        def change(files):
            m = json.loads(files["manifest.json"])
            m["title"] = "12345678"
            files["manifest.json"] = json.dumps(m).encode()
        self.rejected(self.modified(change))

    def test_wrong_profile(self):
        def change(files):
            m = json.loads(files["manifest.json"])
            m["profile"] = "0000000000000000"
            files["manifest.json"] = json.dumps(m).encode()
        self.rejected(self.modified(change))

    def test_unlisted_file(self):
        self.rejected(self.modified(lambda f: f.update({"payload/extra": b"x"})))

    def test_traversal(self):
        for n in ("../escape", "/absolute", "payload/../../escape", "payload\\escape", "payload/./escape"):
            with self.subTest(n=n):
                self.rejected(self.modified(lambda f: f.update({n: b"x"})))

    def test_case_collision(self):
        self.rejected(self.modified(lambda f: f.update({"HEADER.BIN": f["header.bin"]})))

    def test_duplicate_member(self):
        archive = self.bundle()
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(archive, "a") as z:
                z.writestr("header.bin", b"duplicate")
        self.rejected(archive)

    def test_zip_symlink(self):
        archive = self.bundle()
        with zipfile.ZipFile(archive, "a") as z:
            i = zipfile.ZipInfo("payload/link")
            i.create_system = 3
            i.external_attr = 0o120777 << 16
            z.writestr(i, "/tmp/target")
        self.rejected(archive)

    def test_size_limit(self):
        archive = self.bundle()
        with patch.object(R, "LIMIT", 100):
            self.rejected(archive)

    def test_source_symlink(self):
        p = self.src / R.PROFILE / R.TITLE / "00000001" / self.package / "link"
        p.symlink_to(self.header)
        self.assertIn("Symlink", self.lib.export(self.src, "both", self.build)[0][1])

    def test_source_directory_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        p = self.src / R.PROFILE / R.TITLE / "00000001" / self.package / "nested"
        p.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            R.collect_package(self.src, self.package)

    def test_destination_symlink(self):
        self.dst.mkdir(parents=True)
        external = self.root / "external"
        external.mkdir()
        (self.dst / R.PROFILE).symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.lib.import_one(self.bundle(), self.dst, "both", self.build)
        self.assertEqual(list(external.iterdir()), [])

    def test_no_non_replay_export(self):
        p = self.src / R.PROFILE / R.TITLE / "00000001" / self.package
        (p / "replaydata.bin").rename(p / "systemdata.bin")
        self.assertIn("not exported", self.lib.export(self.src, "both", self.build)[0][1])
        self.assertEqual(list((self.lib.root / "Exports").iterdir()), [])

    def test_mixed_systemdata_is_preserved_but_not_exported(self):
        system = self.src / R.PROFILE / R.TITLE / "00000001" / "systemdata.bin"
        system.mkdir()
        (system / "savegame.bin").write_bytes(b"synthetic systemdata, not replay")
        results = dict(self.lib.export(self.src, "both", self.build))
        self.assertNotIn("not exported", results[self.package])
        self.assertIn("not exported", results["systemdata.bin"])
        self.assertEqual(len(list((self.lib.root / "Exports").glob("*" + R.EXT))), 1)

    def test_copy_only_systemdata_persistence_is_non_mutating(self):
        system = self.src / R.PROFILE / R.TITLE / "00000001" / "systemdata.bin"
        system.mkdir()
        system_file = system / "savegame.bin"
        system_file.write_bytes(b"copy-only systemdata fixture")
        copy = self.root / "copied/user-data"
        shutil.copytree(self.src, copy)
        before = (copy / R.PROFILE / R.TITLE / "00000001" /
                  "systemdata.bin/savegame.bin").read_bytes()
        copied_library = R.Library(self.root / "copied-library")
        copied_library.export(copy, "both", self.build)
        after = (copy / R.PROFILE / R.TITLE / "00000001" /
                 "systemdata.bin/savegame.bin").read_bytes()
        self.assertEqual(after, before)
        self.assertEqual(before, b"copy-only systemdata fixture")

    def test_header_validation(self):
        original = self.header.read_bytes()
        for offset, value in ((4, 2), (0x140, 1234)):
            b = bytearray(original)
            struct.pack_into(">I", b, offset, value)
            with self.assertRaises(ValueError):
                R.check_header(bytes(b), self.package)
        with self.assertRaises(ValueError):
            R.check_header(original[:100], self.package)
        with self.assertRaises(ValueError):
            R.check_header(original, "different")

    def test_failure_before_publish_leaves_no_save(self):
        archive = self.bundle()
        with patch.object(R.os, "link", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.lib.import_one(archive, self.dst, "both", self.build)
        title = self.dst / R.PROFILE / R.TITLE
        self.assertFalse((title / "00000001" / self.package).exists())
        self.assertFalse((title / "Headers/00000001" / (self.package + ".header")).exists())

    def test_lock_rejects_second_writer(self):
        with R.session_lock(self.root / "session"):
            with self.assertRaisesRegex(ValueError, "busy"):
                with R.session_lock(self.root / "session"):
                    self.fail("second writer admitted")
        with R.session_lock(self.root / "session"):
            pass

    def test_lock_survives_in_child(self):
        session = self.root / "session"
        with R.session_lock(session) as fd:
            proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                    stdin=subprocess.PIPE, pass_fds=(fd,))
        try:
            with self.assertRaises(ValueError):
                with R.session_lock(session):
                    self.fail("child lost lock")
        finally:
            proc.communicate(timeout=5)
        with R.session_lock(session):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
