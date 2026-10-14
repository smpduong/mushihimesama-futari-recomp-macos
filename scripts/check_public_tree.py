#!/usr/bin/env python3
"""Fail closed when the public tree differs from its reviewed text-source list."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    ".gitattributes",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/workflows/source-hygiene.yml",
    ".gitignore",
    "CONTRIBUTING.md",
    "config/futari15x360.json",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/ARCHITECTURE.md",
    "docs/BUILDING.md",
    "game-work/futari15-x360/src/futari15x360_app.h",
    "game-work/futari15-x360/src/futari_config_paths.h",
    "game-work/futari15-x360/src/futari_profile_lease.h",
    "patches/README.md",
    "patches/rexglue-sdk-LICENSE.txt",
    "patches/rexglue-sdk-current.patch",
    "patches/rexglue-sdk-local.patch",
    "patches/sdk-submodules.txt",
    "scripts/check_public_tree.py",
    "scripts/build_from_game.py",
    "scripts/create_source_setup_archive.py",
    "scripts/futari.py",
    "scripts/install_dlc_via_runtime.py",
    "scripts/libarchive_api_subset.h",
    "scripts/package_launcher.mm",
    "scripts/package_launcher_ui.inc",
    "scripts/package_macos.py",
    "scripts/package_smoke.py",
    "scripts/public_output_guard.py",
    "scripts/replay_bundle.mm",
    "scripts/smoke_observation.py",
    "scripts/stage_content_profiles.py",
    "scripts/test_content_install.cpp",
    "scripts/test_content_install.py",
    "scripts/test_content_install_decls.h",
    "scripts/test_package_launcher_bundle.py",
    "scripts/test_package_launcher_native_contract.py",
    "scripts/test_package_smoke_relocation.py",
    "scripts/test_profile_lease_replay_guard.py",
    "scripts/test_public_source_hygiene.py",
    "scripts/test_build_from_game.py",
    "scripts/test_public_output_guard.py",
    "scripts/test_smoke_observation.py",
    "scripts/test_source_setup_archive.py",
    "tools/rexglue-sdk/include/rex/rex_app.h",
    "tools/rexglue-sdk/src/ui/host_exit_receipt.h",
    "tools/rexglue-sdk/src/ui/rex_app.cpp",
    "templates/futari15-x360/CMakeLists.txt",
    "templates/futari15-x360/main.cpp",
    "templates/futari15-x360/rexglue.cmake",
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
)
ENCODED_PAYLOAD = re.compile(rb"[A-Za-z0-9+/]{1024,}={0,2}")
PRIVATE_PATH_MARKERS = (b"/Users/" + b"user/",)
MAX_TEXT_BYTES = 5 * 1024 * 1024


def scan_tree(root=ROOT):
    root = Path(root)
    errors = []
    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        actual.add(relative)
        if path.is_symlink():
            errors.append(f"symlink is not allowed: {relative}")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            errors.append(f"non-regular file is not allowed: {relative}")
            continue
        if path.stat().st_size > MAX_TEXT_BYTES:
            errors.append(f"file exceeds text-size limit: {relative}")
            continue
        try:
            data = path.read_bytes()
        except OSError as error:
            errors.append(f"cannot read {relative}: {error}")
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"invalid UTF-8 text: {relative}")
        if b"\0" in data:
            errors.append(f"binary content is not allowed: {relative}")
        if b"version https://git-lfs.github.com/" + b"spec/v1" in data:
            errors.append(f"Git LFS pointer is not allowed: {relative}")
        if ENCODED_PAYLOAD.search(data):
            errors.append(f"large encoded payload is not allowed: {relative}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                errors.append(f"possible credential material: {relative}")
                break
        if any(marker in data for marker in PRIVATE_PATH_MARKERS):
            errors.append(f"machine-specific path found: {relative}")

    expected = set(PUBLIC_FILES)
    for relative in PUBLIC_FILES:
        parent = Path(relative).parent
        while parent.as_posix() != ".":
            expected.add(parent.as_posix())
            parent = parent.parent
    errors.extend(f"unreviewed path: {path}" for path in sorted(actual - expected))
    errors.extend(f"missing allowlisted path: {path}" for path in sorted(expected - actual))
    return errors


def main():
    errors = scan_tree()
    if errors:
        print("Public source hygiene failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Public source hygiene passed: {len(PUBLIC_FILES)} allowlisted text files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
