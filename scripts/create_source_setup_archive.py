#!/usr/bin/env python3
"""Create a source-only archive with normalized metadata and a reviewed allowlist."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "futari-public-source"
try:
    from .check_public_tree import PUBLIC_FILES
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from check_public_tree import PUBLIC_FILES

ALLOWLIST = PUBLIC_FILES


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root=ROOT):
    rows = []
    for relative in ALLOWLIST:
        path = Path(root) / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"allowlisted source missing or symlinked: {relative}")
        rows.append({"path": relative, "bytes": path.stat().st_size,
                     "sha256": digest(path)})
    return rows


def tar_info(name, payload):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def create_archive(output, root=ROOT):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    rows = inventory(root)
    manifest = {
        "schema_version": 2,
        "artifact_status": "public source snapshot; root BSD-3-Clause applies to original project-authored source and documentation only",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "included_files": rows,
        "explicit_exclusions": [
            "game and DLC packages or assets",
            "generated guest code and decompilation output",
            "compiled game executable or modules",
            "saves, replays, logs, captures, and local receipts",
            "build caches, credentials, and machine-specific files",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    for row in rows:
                        data = (Path(root) / row["path"]).read_bytes()
                        if hashlib.sha256(data).hexdigest() != row["sha256"]:
                            raise RuntimeError(f"source changed while archiving: {row['path']}")
                        archive.addfile(tar_info(f"{PREFIX}/{row['path']}", data), io.BytesIO(data))
                    encoded = (json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
                    archive.addfile(tar_info(f"{PREFIX}/SOURCE-MANIFEST.json", encoded), io.BytesIO(encoded))
        with tarfile.open(output, "r:gz") as archive:
            members = archive.getmembers()
            expected = {f"{PREFIX}/{name}" for name in ALLOWLIST}
            expected.add(f"{PREFIX}/SOURCE-MANIFEST.json")
            if {member.name for member in members} != expected or len(members) != len(expected):
                raise RuntimeError("archive member set differs from strict allowlist")
            for row in rows:
                member = archive.extractfile(f"{PREFIX}/{row['path']}")
                data = member.read() if member else b""
                if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise RuntimeError(f"archive verification failed: {row['path']}")
    except Exception:
        if output.exists() and not output.is_symlink():
            output.unlink()
        raise
    return {"path": str(output), "bytes": output.stat().st_size,
            "sha256": digest(output), "file_count": len(rows), "member_count": len(rows) + 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="new archive path; an existing path is never overwritten")
    args = parser.parse_args()
    try:
        print(json.dumps(create_archive(args.output.absolute()), indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
