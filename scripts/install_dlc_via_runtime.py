#!/usr/bin/env python3
"""Install a DLC package into a real root via the runtime's InstallContent.

Astra 2026-09-22: the manual file copy used by the menu probe skips the
runtime's `.header` write (which carries the license mask computed from the
STFS header).  This driver compiles the SAME production-path harness the
install test uses and runs it in `--install <package> <root>` mode so the
game's own content manager sees a properly installed package.

Usage:
  python3 scripts/install_dlc_via_runtime.py <package> <root>
"""
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import test_content_install as T  # noqa: E402


def install_packages(packages, root: pathlib.Path):
    """Compile once, install each supplied package, propagate any failure."""
    with tempfile.TemporaryDirectory(prefix="dlc-install-") as td:
        tmp = pathlib.Path(td)
        cm_copy, counts, _ = T.prepare_sources(tmp)
        if counts["fwrite("] < 3 or counts["fclose("] < 4 \
                or counts["kernel_state_->title_id()"] < 5:
            raise SystemExit("content_manager.cpp substitution counts drifted")
        exe = tmp / "install_cli"
        T.build(exe, cm_copy)
        for package in packages:
            r = subprocess.run([str(exe), "--install", str(package), str(root)],
                               capture_output=True, text=True, cwd=str(ROOT),
                               timeout=180)
            sys.stdout.write(r.stdout)
            sys.stderr.write(r.stderr)
            if r.returncode:
                return r.returncode
        return 0


def install(package: pathlib.Path, root: pathlib.Path):
    return install_packages([package], root)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    sys.exit(install(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])))
