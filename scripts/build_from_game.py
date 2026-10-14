#!/usr/bin/env python3
"""Build Futari from user-supplied extracted Xbox 360 files.

All SDK, generated guest code, build output, logs, and receipts live beneath an
explicit external build root. The supplied game directory is treated as
read-only. This tool never launches the game.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone


REPO = Path(__file__).resolve().parents[1]
SDK_URL = "https://github.com/rexglue/rexglue-sdk.git"
SDK_REVISION = "c94f5ebdcb3c9d1a460ca48e04f9758448f8d518"
SDK_PATCH_SHA256 = "327d153119c2a332bf6333ab48462ac3b34383579443797bd25fd1f20dd7cb86"
SDK_PATCH = REPO / "patches" / "rexglue-sdk-current.patch"
APP_NAME = "futari15x360"
ENTRYPOINT = "default.xex"
MODULES = (
    "reco_normal.xex", "reco_abnormal.xex",
    "palm_normal.xex", "palm_abnormal.xex",
    "kiniro_normal.xex", "kiniro_abnormal.xex",
    "hirow_normal.xex", "hirow_abnormal.xex",
)
MARKER = ".futari-public-build-root.json"
RECIPE_FILES = (
    "scripts/build_from_game.py",
    "config/futari15x360.json",
    "templates/futari15-x360/CMakeLists.txt",
    "templates/futari15-x360/main.cpp",
    "templates/futari15-x360/rexglue.cmake",
    "game-work/futari15-x360/src/futari15x360_app.h",
    "game-work/futari15-x360/src/futari_config_paths.h",
    "game-work/futari15-x360/src/futari_profile_lease.h",
    "patches/rexglue-sdk-current.patch",
    "patches/sdk-submodules.txt",
)
MAX_LOG_BYTES = 16 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 7200
MIN_FREE_BYTES = 12 * 1024 * 1024 * 1024


class BuildError(RuntimeError):
    pass


def recipe_identity(repo: Path = REPO) -> dict:
    """Hash the source/configuration that determines generated build inputs."""
    hashes = {}
    for relative in RECIPE_FILES:
        path = repo / relative
        if path.is_symlink() or not path.is_file():
            raise BuildError(f"critical build recipe file is missing or unsafe: {relative}")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = {"files_sha256": hashes}
    try:
        head = capture(["git", "rev-parse", "HEAD"], repo)
    except (OSError, BuildError, subprocess.SubprocessError):
        head = ""
    if re.fullmatch(r"[0-9a-f]{40,64}", head):
        identity["git_head"] = head
    return identity


def reject_symlink_components(path: Path) -> None:
    raw = path.expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                raise BuildError(f"path may not traverse a symlink: {current}")
        except OSError as error:
            raise BuildError(f"cannot inspect path component: {current}") from error


def canonical(path: Path) -> Path:
    reject_symlink_components(path)
    return path.expanduser().resolve(strict=False)


def validate_tree_links(path: Path, allow_internal: bool) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise BuildError(f"refusing symlinked writable tree: {path}")
    for item in path.rglob("*"):
        if not item.is_symlink():
            continue
        if not allow_internal:
            raise BuildError(f"refusing symlink in writable tree: {item}")
        try:
            item.resolve(strict=False).relative_to(path.resolve(strict=True))
        except (OSError, ValueError):
            raise BuildError(f"writable-tree symlink escapes its owned root: {item}")


def overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def check_separate_roots(game_dir: Path, build_root: Path) -> tuple[Path, Path]:
    reject_symlink_components(game_dir)
    reject_symlink_components(build_root)
    game = canonical(game_dir)
    build = canonical(build_root)
    repo = canonical(REPO)
    if not game.is_dir():
        raise BuildError(f"game directory does not exist: {game}")
    if overlaps(game, repo) or overlaps(build, repo) or overlaps(game, build):
        raise BuildError("repository, game directory, and build root must be separate non-overlapping paths")
    return game, build


def load_public_config() -> dict:
    config = json.loads((REPO / "config" / "futari15x360.json").read_text(encoding="utf-8"))
    return validate_public_config(config)


def validate_public_config(config: object) -> dict:
    if not isinstance(config, dict) or set(config) != {
        "schema", "supported_revision", "files_sha256", "entrypoint_functions", "modules"
    }:
        raise BuildError("public title metadata has unknown or missing top-level fields")
    if type(config["schema"]) is not int or config["schema"] != 1:
        raise BuildError("unsupported public title metadata schema")
    if config["supported_revision"] != "futari-ver-1.5-xex-set-2026-10-13":
        raise BuildError("unsupported public title metadata revision")
    hashes = config["files_sha256"]
    if not isinstance(hashes, dict) or set(hashes) != {ENTRYPOINT, *MODULES}:
        raise BuildError("public metadata input hash set is invalid")
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in hashes.values()):
        raise BuildError("public metadata contains an invalid input hash")

    def check_functions(values: object, label: str) -> None:
        if not isinstance(values, list) or any(
            not isinstance(value, str) or re.fullmatch(r"0x[0-9A-Fa-f]{8}", value) is None
            for value in values
        ) or len(values) != len(set(values)):
            raise BuildError(f"{label} must be a unique list of uint32 guest addresses")

    check_functions(config["entrypoint_functions"], "entrypoint functions")
    modules = config["modules"]
    if not isinstance(modules, list) or len(modules) != len(MODULES):
        raise BuildError("public metadata module list is invalid")
    seen = set()
    for module in modules:
        if not isinstance(module, dict) or set(module) != {
            "file", "out_directory_path", "functions", "switch_tables"
        }:
            raise BuildError("public module metadata has unknown or missing fields")
        filename = module["file"]
        if filename not in MODULES or filename in seen:
            raise BuildError("public module filename is not an exact supported input")
        seen.add(filename)
        if module["out_directory_path"] != f"generated/{Path(filename).stem}":
            raise BuildError("public module output path is not a reviewed relative path")
        check_functions(module["functions"], f"{filename} functions")
        tables = module["switch_tables"]
        if not isinstance(tables, list):
            raise BuildError("public switch-table metadata must be a list")
        for table in tables:
            if not isinstance(table, dict) or set(table) != {"address", "register", "labels"}:
                raise BuildError("public switch-table metadata has unknown or missing fields")
            if type(table["address"]) is not int or not 0 <= table["address"] <= 0xFFFFFFFF:
                raise BuildError("public switch-table address must be uint32")
            if type(table["register"]) is not int or not 0 <= table["register"] <= 31:
                raise BuildError("public switch-table register must be in range 0..31")
            labels = table["labels"]
            if (not isinstance(labels, list) or not labels
                    or any(type(label) is not int or not 0 <= label <= 0xFFFFFFFF for label in labels)
                    or len(labels) != len(set(labels))):
                raise BuildError("public switch-table labels must be unique uint32 values")
    if seen != set(MODULES):
        raise BuildError("public module metadata does not cover the supported input set")
    return config


def validate_game(game: Path) -> dict[str, Path]:
    config = load_public_config()
    required = {ENTRYPOINT: game / ENTRYPOINT}
    required.update({name: game / "media" / "Module" / name for name in MODULES})
    configured = [module.get("file") for module in config.get("modules", [])]
    if len(configured) != len(MODULES) or set(configured) != set(MODULES):
        raise BuildError("public module metadata does not match the supported input set")
    for module in config["modules"]:
        expected_output = f"generated/{Path(module['file']).stem}"
        if module.get("out_directory_path") != expected_output:
            raise BuildError("public module output path is not a reviewed relative path")
    if game.is_symlink() or (game / "media").is_symlink() or (game / "media/Module").is_symlink():
        raise BuildError("game root and module directories may not be symlinks")
    missing = [str(path.relative_to(game)) for path in required.values() if not path.is_file()]
    if missing:
        raise BuildError("missing required extracted game files: " + ", ".join(missing))
    empty = [str(path.relative_to(game)) for path in required.values() if path.stat().st_size == 0]
    if empty:
        raise BuildError("empty required game files: " + ", ".join(empty))
    symlinks = [str(path.relative_to(game)) for path in required.values() if path.is_symlink()]
    if symlinks:
        raise BuildError("symlinked game inputs are not allowed: " + ", ".join(symlinks))
    mismatched = []
    for name, path in required.items():
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != config["files_sha256"].get(name):
            mismatched.append(name)
    if mismatched:
        raise BuildError("unsupported game revision; file hashes do not match the supported Ver. 1.5 set: "
                         + ", ".join(mismatched))
    return required


def validate_sdk_checkout(root: Path, require_outputs: bool) -> None:
    sdk = root / "sdk"
    if sdk.is_symlink() or not (sdk / ".git").exists():
        raise BuildError("external SDK path is not an owned Git checkout")
    if capture(["git", "rev-parse", "HEAD"], sdk) != SDK_REVISION:
        raise BuildError("existing SDK HEAD differs from the pinned base")
    if capture(["git", "remote", "get-url", "origin"], sdk) != SDK_URL:
        raise BuildError("existing SDK origin differs from the reviewed upstream")
    exact_patch = subprocess.run(["git", "diff", "--cached", "HEAD", "--binary", "--full-index"],
                                 cwd=sdk, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if exact_patch.returncode or exact_patch.stdout != SDK_PATCH.read_bytes():
        raise BuildError("existing SDK staged patch bytes differ from the reviewed patch")
    if capture(["git", "diff", "--name-only"], sdk) or capture(
            ["git", "ls-files", "--others", "--exclude-standard"], sdk):
        raise BuildError("existing SDK contains unreviewed modifications or untracked files")
    expected = {}
    for line in (REPO / "patches/sdk-submodules.txt").read_text(encoding="utf-8").splitlines():
        fields = line.strip().lstrip("+").split()
        if len(fields) >= 2:
            expected[fields[1]] = fields[0]
    actual = {}
    for state, revision, path in parse_submodule_status(
            capture(["git", "submodule", "status", "--recursive"], sdk)):
        if state != " " or expected.get(path) != revision:
            raise BuildError(f"existing SDK submodule differs from its pin: {path}")
        actual[path] = revision
    if actual != expected:
        raise BuildError("existing SDK submodule set differs from the pinned list")
    for submodule in expected:
        if capture(["git", "-C", str(sdk / submodule), "status", "--porcelain", "--untracked-files=all"], sdk):
            raise BuildError(f"existing SDK submodule has unreviewed changes: {submodule}")
    if require_outputs:
        outputs = (sdk / "out/mac-arm64/Debug/rexglued",
                   sdk / "out/mac-arm64/Debug/librexruntimed.dylib",
                   sdk / "out/mac-arm64/Debug/librexcodegend.a")
        if any(not item.is_file() or item.stat().st_size == 0 for item in outputs):
            raise BuildError("verified SDK bootstrap is missing expected built outputs")


def ensure_build_root(path: Path, resume: bool, game: Path) -> Path:
    root = canonical(path)
    if path.expanduser().is_symlink() or root.is_symlink():
        raise BuildError("build root may not be a symlink")
    if root.exists():
        marker = root / MARKER
        if not resume or marker.is_symlink() or not marker.is_file():
            raise BuildError("build root exists and is not a resumable build created by this tool")
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BuildError("build-root marker is invalid; refusing to modify it") from error
        inputs = validate_game(game)
        fingerprints = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for name, path in inputs.items()}
        expected = {
            "schema": 2, "repo": str(REPO.resolve()), "sdk_revision": SDK_REVISION,
            "sdk_patch_sha256": SDK_PATCH_SHA256,
            "config_sha256": hashlib.sha256((REPO / "config" / "futari15x360.json").read_bytes()).hexdigest(),
            "build_root": str(root), "game_root": str(game.resolve()), "input_sha256": fingerprints,
            "recipe_identity": recipe_identity(),
        }
        if (isinstance(record, dict) and record.get("schema") == 1
                and record.get("repo") == str(REPO.resolve())
                and record.get("sdk_revision") == SDK_REVISION):
            # Adopt only the SDK worker's known SDK-only bootstrap shape after
            # byte-for-byte SDK, patch, submodule, and output verification.
            if {item.name for item in root.iterdir()} != {MARKER, "sdk", "cache", "logs"}:
                raise BuildError("legacy SDK bootstrap contains unexpected paths; refusing adoption")
            for name in ("sdk", "cache", "logs"):
                if (root / name).is_symlink() or not (root / name).is_dir():
                    raise BuildError(f"legacy SDK bootstrap path is unsafe: {root / name}")
            validate_tree_links(root / "sdk", allow_internal=True)
            validate_tree_links(root / "cache", allow_internal=False)
            validate_tree_links(root / "logs", allow_internal=False)
            validate_sdk_checkout(root, require_outputs=True)
            record = {**expected, "created_utc": record.get("created_at_utc", "verified SDK bootstrap")}
            marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        elif not isinstance(record, dict) or any(record.get(key) != value for key, value in expected.items()):
            raise BuildError("build-root marker belongs to another source/revision")
        allowed = {MARKER, "sdk", "cache", "logs", "project", "build", "receipts"}
        extras = {item.name for item in root.iterdir()} - allowed
        if extras:
            raise BuildError("resumable build root contains unrecognized top-level paths: "
                             + ", ".join(sorted(extras)))
        for name in ("sdk", "cache", "logs", "project", "build", "receipts"):
            child = root / name
            if child.is_symlink():
                raise BuildError(f"resumable build-root path is unsafe: {child}")
            if not child.exists():
                if name in {"sdk", "cache", "logs"}:
                    raise BuildError(f"resumable build-root path is missing: {child}")
                continue
            if not child.is_dir():
                raise BuildError(f"resumable build-root path is missing or unsafe: {child}")
    else:
        if resume:
            raise BuildError("cannot resume because the build root does not exist")
        root.mkdir(parents=True, exist_ok=False)
        marker = root / MARKER
        inputs = validate_game(game)
        marker.write_text(json.dumps({"schema": 2, "repo": str(REPO.resolve()),
                                      "sdk_revision": SDK_REVISION,
                                      "sdk_patch_sha256": SDK_PATCH_SHA256,
                                      "config_sha256": hashlib.sha256((REPO / "config" / "futari15x360.json").read_bytes()).hexdigest(),
                                      "build_root": str(root), "game_root": str(game.resolve()),
                                      "recipe_identity": recipe_identity(),
                                      "input_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                                       for name, path in inputs.items()},
                                      "created_utc": datetime.now(timezone.utc).isoformat()},
                                     indent=2) + "\n", encoding="utf-8")
    for name in ("sdk", "cache", "logs"):
        directory = root / name
        if directory.is_symlink():
            raise BuildError(f"refusing symlinked build-root directory: {directory}")
        directory.mkdir(exist_ok=True)
    validate_tree_links(root / "sdk", allow_internal=True)
    validate_tree_links(root / "cache", allow_internal=False)
    validate_tree_links(root / "logs", allow_internal=False)
    for name in ("project", "build", "receipts"):
        validate_tree_links(root / name, allow_internal=(name == "build"))
    return root


def command(argv: list[str], cwd: Path, log: Path, dry_run: bool) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return
    permitted = {"PATH", "HOME", "TMPDIR", "SDKROOT", "DEVELOPER_DIR", "HTTP_PROXY", "HTTPS_PROXY"}
    environment = {key: value for key, value in os.environ.items() if key in permitted}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if log.is_symlink() or log.exists():
        raise BuildError(f"refusing to overwrite existing command log: {log}")
    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=16)
    with log.open("xb") as output:
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        assert process.stdout is not None

        def pump_output() -> None:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    chunks.put(None)
                    return
                chunks.put(chunk)

        reader = threading.Thread(target=pump_output, daemon=True)
        reader.start()
        started = time.monotonic()
        written = 0
        overflow = False
        timed_out = False
        finished = False
        while not finished:
            try:
                chunk = chunks.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is None and time.monotonic() - started > COMMAND_TIMEOUT_SECONDS:
                    timed_out = True
                    os.killpg(process.pid, signal.SIGKILL)
                continue
            if chunk is None:
                finished = True
            elif written + len(chunk) > MAX_LOG_BYTES:
                overflow = True
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
            elif not overflow:
                output.write(chunk)
                written += len(chunk)
        return_code = process.wait()
        reader.join(timeout=5)
    if timed_out:
        raise BuildError(f"command exceeded the {COMMAND_TIMEOUT_SECONDS}-second limit; see {log}")
    if overflow:
        raise BuildError(f"command output exceeded the {MAX_LOG_BYTES}-byte log limit; see {log}")
    if return_code:
        raise BuildError(f"command failed ({return_code}); see {log}")


def capture(argv: list[str], cwd: Path) -> str:
    result = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise BuildError(f"read-only SDK validation command failed: {argv[0]}")
    # Keep leading status characters from `git submodule status` intact.
    return result.stdout.rstrip("\r\n")


def parse_submodule_status(status: str) -> list[tuple[str, str, str]]:
    parsed = []
    for line in status.splitlines():
        match = re.match(r"([ +-U])([0-9a-f]+)\s+(.+?)(?:\s+\([^)]*\))?$", line)
        if not match:
            raise BuildError("existing SDK submodule status is unreadable")
        parsed.append(match.groups())
    return parsed


def sdk_setup_commands(root: Path, jobs: int) -> list[tuple[Path, list[str]]]:
    sdk = root / "sdk"
    cache = root / "cache" / "CPM"
    commands: list[tuple[Path, list[str]]] = []
    if sdk.is_symlink():
        raise BuildError("external SDK path is a symlink; refusing to use it")
    fresh = not sdk.exists() or (sdk.is_dir() and not any(sdk.iterdir()))
    if fresh:
        commands.extend(((root, ["git", "clone", "--filter=blob:none", SDK_URL, str(sdk)]),
                         (sdk, ["git", "checkout", "--detach", SDK_REVISION]),
                         (sdk, ["git", "apply", "--check", str(SDK_PATCH)]),
                         (sdk, ["git", "apply", "--index", str(SDK_PATCH)])))
        patch_applied = False
        submodules_ready = False
    else:
        if sdk.is_symlink() or not (sdk / ".git").exists():
            raise BuildError("external SDK path is occupied by an unrecognized checkout")
        if capture(["git", "rev-parse", "HEAD"], sdk) != SDK_REVISION:
            raise BuildError("existing SDK HEAD differs from the pinned base; refusing checkout/reset")
        if capture(["git", "remote", "get-url", "origin"], sdk) != SDK_URL:
            raise BuildError("existing SDK origin differs from the reviewed upstream")
        reverse = subprocess.run(["git", "apply", "--reverse", "--check", str(SDK_PATCH)],
                                 cwd=sdk, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 check=False)
        patch_applied = reverse.returncode == 0
        if not patch_applied:
            check = subprocess.run(["git", "apply", "--check", str(SDK_PATCH)], cwd=sdk,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if check.returncode:
                raise BuildError("existing SDK does not match the reviewed patch state")
            staged = capture(["git", "diff", "--cached", "--name-only"], sdk)
            unstaged = capture(["git", "diff", "--name-only"], sdk)
            untracked = capture(["git", "ls-files", "--others", "--exclude-standard"], sdk)
            if staged or unstaged or untracked:
                raise BuildError("existing SDK has unrecognized local changes; refusing to overwrite")
        if patch_applied:
            expected_files = set(re.findall(r"^diff --git a/(.+?) b/", SDK_PATCH.read_text(encoding="utf-8"), re.M))
            staged = set(capture(["git", "diff", "--cached", "--name-only"], sdk).splitlines())
            unstaged = capture(["git", "diff", "--name-only"], sdk)
            untracked = capture(["git", "ls-files", "--others", "--exclude-standard"], sdk)
            if staged != expected_files or unstaged or untracked:
                raise BuildError("existing SDK changes differ from the reviewed patch; refusing to overwrite")
            exact_patch = subprocess.run(["git", "diff", "--cached", "HEAD", "--binary", "--full-index"],
                                         cwd=sdk, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         check=False)
            if exact_patch.returncode or exact_patch.stdout != SDK_PATCH.read_bytes():
                raise BuildError("existing SDK staged patch bytes differ from the reviewed patch")
        expected = {}
        for line in (REPO / "patches/sdk-submodules.txt").read_text(encoding="utf-8").splitlines():
            fields = line.strip().lstrip("+").split()
            if len(fields) >= 2:
                expected[fields[1]] = fields[0]
        status = capture(["git", "submodule", "status", "--recursive"], sdk)
        actual = {}
        incomplete = False
        for state, revision, path in parse_submodule_status(status):
            if state in {"+", "U"}:
                raise BuildError(f"existing SDK submodule differs from its pin: {path}")
            if state == "-":
                incomplete = True
            else:
                actual[path] = revision
        if any(actual.get(path) not in {None, revision} for path, revision in expected.items()):
            raise BuildError("existing SDK submodule revisions differ from the pinned list")
        for submodule in actual:
            if capture(["git", "-C", str(sdk / submodule), "status", "--porcelain",
                        "--untracked-files=all"], sdk):
                raise BuildError(f"existing SDK submodule has unreviewed changes: {submodule}")
        submodules_ready = not incomplete and all(actual.get(path) == revision
                                                   for path, revision in expected.items())
        if not patch_applied:
            commands.extend(((sdk, ["git", "apply", "--check", str(SDK_PATCH)]),
                             (sdk, ["git", "apply", "--index", str(SDK_PATCH)])))
    if not submodules_ready:
        commands.append((sdk, ["git", "submodule", "update", "--init", "--recursive"]))
    sdk_cli = sdk / "out/mac-arm64/Debug/rexglued"
    runtime = sdk / "out/mac-arm64/Debug/librexruntimed.dylib"
    codegen = sdk / "out/mac-arm64/Debug/librexcodegend.a"
    if not all(path.is_file() and path.stat().st_size for path in (sdk_cli, runtime, codegen)):
        commands.extend(((sdk, ["cmake", "--preset", "mac-arm64",
                                "-DCMAKE_C_COMPILER=/usr/bin/clang",
                                "-DCMAKE_CXX_COMPILER=/usr/bin/clang++",
                                "-DREXGLUE_ENABLE_FIDELITYFX=OFF", "-DREXGLUE_BUILD_TESTS=OFF",
                                f"-DCPM_SOURCE_CACHE={cache}"]),
                         (sdk, ["cmake", "--build", "--preset", "mac-arm64-debug",
                                "--target", "rexglue", "rexruntime", "rexcodegen",
                                "--config", "Debug", "--parallel", str(jobs)])))
    return commands


def render_manifest(game: Path, project: Path) -> Path:
    """Write only structured config/hints; never copy the user's manifest."""
    config = load_public_config()
    manifest = project / f"{APP_NAME}_manifest.toml"
    # Absolute paths point only to validated, read-only user game files.
    lines = [f'[project]\nname = "{APP_NAME}"\nsdk_version = "0.10.0"\n',
             f'\n[entrypoint]\nfile_path = {json.dumps(str(game / ENTRYPOINT))}\n',
             'out_directory_path = "generated/default"\nincludes = []\n',
             '\n[entrypoint.functions]\n']
    for address in config["entrypoint_functions"]:
        lines.append(f'{address} = {{}}\n')
    for module in config["modules"]:
        lines.extend(('\n[[modules]]\n',
                      f'guest_path = {json.dumps("media/module/" + module["file"].lower())}\n',
                      f'file_path = {json.dumps(str(game / "media" / "Module" / module["file"]))}\n',
                      f'out_directory_path = {json.dumps(module["out_directory_path"])}\n',
                      'includes = []\n', '\n[modules.functions]\n'))
        lines.extend(f'{address} = {{}}\n' for address in module["functions"])
        for table in module["switch_tables"]:
            lines.extend(('\n[[modules.switch_tables]]\n',
                          f'address = {table["address"]}\n',
                          f'register = {table["register"]}\n',
                          'labels = [' + ', '.join(map(str, table["labels"])) + ']\n'))
    payload = "".join(lines)
    if manifest.is_symlink():
        raise BuildError("refusing symlinked generated manifest")
    if manifest.exists():
        if not manifest.is_file() or manifest.read_text(encoding="utf-8") != payload:
            raise BuildError("refusing to overwrite a non-matching generated manifest")
    else:
        manifest.write_text(payload, encoding="utf-8")
    return manifest


def write_project(game: Path, root: Path) -> tuple[Path, Path]:
    project = root / "project"
    template = REPO / "templates" / "futari15-x360"
    sources = {
        "CMakeLists.txt": template / "CMakeLists.txt",
        "src/main.cpp": template / "main.cpp",
        "src/futari15x360_app.h": REPO / "game-work/futari15-x360/src/futari15x360_app.h",
        "src/futari_config_paths.h": REPO / "game-work/futari15-x360/src/futari_config_paths.h",
        "src/futari_profile_lease.h": REPO / "game-work/futari15-x360/src/futari_profile_lease.h",
        "generated/rexglue.cmake": template / "rexglue.cmake",
    }
    for relative, source in sources.items():
        destination = project / relative
        relative_parent = destination.parent.relative_to(project)
        current = project
        if current.is_symlink():
            raise BuildError(f"refusing symlinked project path: {current}")
        for part in relative_parent.parts:
            current = current / part
            if current.is_symlink():
                raise BuildError(f"refusing symlinked project path: {current}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise BuildError(f"refusing symlinked project file: {destination}")
        payload = source.read_bytes()
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != payload:
                raise BuildError(f"refusing to overwrite project file: {destination}")
        else:
            destination.write_bytes(payload)
    manifest = render_manifest(game, project)
    return project, manifest


def plan(root: Path, game: Path, jobs: int) -> list[tuple[Path, list[str]]]:
    sdk = root / "sdk"
    build = root / "build"
    project = root / "project"
    cache = root / "cache"
    guest_configure = ["cmake", "-S", str(project), "-B", str(build), "-G", "Ninja",
                       "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_C_COMPILER=/usr/bin/clang",
                       "-DCMAKE_CXX_COMPILER=/usr/bin/clang++", f"-DCPM_SOURCE_CACHE={cache / 'CPM'}",
                       "-DREXGLUE_ENABLE_FIDELITYFX=OFF", "-DREXGLUE_BUILD_TESTS=OFF",
                       f"-DREXSDK_DIR={sdk}",
                       f"-DREXGLUE_ROOT={sdk}", f"-DREXGLUE_MOLTENVK_ICD={sdk / 'cmake/MoltenVK_icd.json'}"]
    return [*sdk_setup_commands(root, jobs),
            (project, guest_configure),
            (project, ["cmake", "--build", str(build), "--target", f"{APP_NAME}_codegen", "--parallel", str(jobs)]),
            (project, guest_configure),
            (project, ["cmake", "--build", str(build), "--parallel", str(jobs)])]


def expected_artifacts(build: Path) -> list[Path]:
    config = load_public_config()
    modules = [f"lib{APP_NAME}_{Path(item['file']).stem}.dylib" for item in config["modules"]]
    return [build / APP_NAME, build / "librexruntimed.dylib", build / "librexgpu-xenosd.dylib",
            build / "libTracyClientd.dylib", build / "vulkan/lib/libMoltenVK.dylib",
            build / "vulkan/lib/libvulkan.1.dylib",
            build / "vulkan/share/vulkan/icd.d/MoltenVK_icd.json",
            *[build / module for module in modules]]


def verify_artifacts(build: Path) -> None:
    root = build.parent.resolve(strict=True)
    missing = []
    for path in expected_artifacts(build):
        try:
            target = path.resolve(strict=True)
            target.relative_to(root)
        except (OSError, ValueError):
            missing.append(path.name)
            continue
        if not target.is_file() or target.stat().st_size == 0:
            missing.append(path.name)
    if missing:
        raise BuildError("build command completed but required outputs are missing/empty: "
                         + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", required=True, type=Path,
                        help="read-only extracted game root containing default.xex and media/Module/*.xex")
    parser.add_argument("--build-root", required=True, type=Path,
                        help="new external directory for SDK, generated project, build, logs, and receipts")
    parser.add_argument("--jobs", type=int, default=2, choices=range(1, 9), metavar="1..8")
    parser.add_argument("--stage", choices=("doctor", "plan", "build"), default="doctor")
    parser.add_argument("--resume", action="store_true", help="resume only a build root marked by this tool")
    parser.add_argument("--dry-run", action="store_true", help="validate and print safe build plan without subprocesses")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 11):
            raise BuildError("Python 3.11 or later is required")
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise BuildError("the native build workflow requires macOS on Apple silicon")
        game, build_path = check_separate_roots(args.game_dir, args.build_root)
        files = validate_game(game)
        if not build_path.parent.is_dir():
            raise BuildError("build-root parent directory must already exist")
        if shutil.disk_usage(build_path.parent).free < MIN_FREE_BYTES:
            raise BuildError("at least 12 GiB of free space is required for the pinned SDK and generated build")
        if shutil.which("cmake") is None or shutil.which("ninja") is None or shutil.which("git") is None:
            raise BuildError("cmake, ninja, and git must be installed")
        if not Path("/usr/bin/clang").is_file() or not Path("/usr/bin/clang++").is_file():
            raise BuildError("Apple command-line tools with /usr/bin/clang and clang++ are required")
        if not SDK_PATCH.is_file():
            raise BuildError("pinned current SDK patch is missing from the source checkout")
        with SDK_PATCH.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != SDK_PATCH_SHA256:
                raise BuildError("current SDK patch hash differs from the reviewed patch")
        if args.stage == "build" and not args.dry_run:
            root = ensure_build_root(build_path, args.resume, game)
        else:
            root = build_path
        receipt = {"stage": args.stage, "repo": str(REPO), "game_dir": str(game),
                   "build_root": str(root), "sdk_revision": SDK_REVISION,
                   "files": {name: {"relative": str(path.relative_to(game)), "bytes": path.stat().st_size}
                             for name, path in files.items()},
                   "created_utc": datetime.now(timezone.utc).isoformat()}
        if args.stage == "doctor":
            print("Preflight passed. Inputs are present; no build commands were run.")
            print(f"External build root selected: {root}")
            return 0
        if args.stage == "plan":
            for cwd, argv in plan(root, game, args.jobs):
                print(" ".join(argv))
            return 0
        if args.dry_run:
            for cwd, argv in plan(root, game, args.jobs):
                print(" ".join(argv))
            return 0
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        commands = plan(root, game, args.jobs)
        for name in ("project", "build", "receipts"):
            target = root / name
            reject_symlink_components(target)
            if target.is_symlink():
                raise BuildError(f"refusing symlinked build-root directory: {target}")
            target.mkdir(exist_ok=True)
        receipt_path = root / "receipts" / f"preflight-{run_id}.json"
        if receipt_path.exists() or receipt_path.is_symlink():
            raise BuildError(f"refusing to overwrite receipt: {receipt_path}")
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        project, _manifest = write_project(game, root)
        run_logs = root / "logs" / run_id
        if run_logs.exists() or run_logs.is_symlink():
            raise BuildError(f"refusing to overwrite run logs: {run_logs}")
        run_logs.mkdir()
        for index, (cwd, argv) in enumerate(commands, 1):
            command(argv, cwd, run_logs / f"{index:02d}.log", False)
        verify_artifacts(root / "build")
        print(f"Build completed. Executable: {root / 'build' / APP_NAME}")
        print("The game was not launched. No game or user-data files were modified.")
        return 0
    except (BuildError, OSError, ValueError) as error:
        print(f"build_from_game: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
