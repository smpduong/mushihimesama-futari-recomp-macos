#!/usr/bin/env python3
"""Local macOS ARM64 Futari workflow; Python standard library only."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "tools/rexglue-sdk"
SDK_BUILD = SDK / "out/build/mac-arm64"
PROJECTS = {"arcade": ("futari15", "futari15"), "x360": ("futari15-x360", "futari15x360")}
MAX_JOBS = 8
FATAL = re.compile(r"\[fatal\]|invalid or unregistered function|assertion failed|unhandled exception", re.I)


def cache_values(path):
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line and not line.startswith(("//", "#")) and "=" in line and ":" in line.split("=", 1)[0]:
                key, value = line.split("=", 1)
                values[key.split(":", 1)[0]] = value
    return values


def capture(command, cwd=ROOT):
    result = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=30, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    return result.returncode, result.stdout.strip()


def git_state(path):
    if not (path / ".git").exists():
        return {"path": str(path), "repository": False}
    if not shutil.which("git"):
        return {"path": str(path), "error": "git unavailable"}
    code, revision = capture(["git", "rev-parse", "HEAD"], path)
    status_code, status = capture(["git", "status", "--short", "--untracked-files=normal"], path)
    return {"path": str(path), "revision": revision if not code else None,
            "dirty": bool(status) if not status_code else None, "status": status}


def file_info(path, digest=False):
    item = {"path": str(path), "exists": path.is_file()}
    if item["exists"]:
        stat = path.stat()
        item.update(bytes=stat.st_size, modified_ns=stat.st_mtime_ns)
        if digest:
            with path.open("rb") as stream:
                item["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    return item


def layout(project):
    directory, binary = PROJECTS[project]
    source = ROOT / "game-work" / directory
    build = source / "out/build/mac-arm64-debug"
    return source, build, binary


def dependencies():
    """Use the prepared local cache; never bootstrap or fetch packages."""
    cache = cache_values(SDK_BUILD / "CMakeCache.txt")
    cpm = Path(cache.get("CPM_SOURCE_CACHE") or str(Path.home() / ".cache/CPM"))
    recipe = SDK / "thirdparty/moltenvk/cmake/recipes/CPM.cmake"
    version = re.search(r"set\(CPM_DOWNLOAD_VERSION ([^)]+)\)", recipe.read_text()) if recipe.is_file() else None
    required = {"CPM.cmake": cpm / "cpm" / f"CPM_{version[1] if version else 'missing'}.cmake"}
    flags = [f"-DCPM_SOURCE_CACHE={cpm}", "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
             "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON", "-DREXGLUE_ENABLE_FIDELITYFX=OFF"]
    for package in ("SPIRV-Cross", "cereal"):
        source = cache.get(f"{package}_SOURCE_DIR", "")
        required[package] = Path(source) / "CMakeLists.txt" if source else SDK / f"missing-local-{package}"
        flags.append(f"-DCPM_{package}_SOURCE={source}")
    return required, flags


def manifest(project):
    source, _, binary = layout(project)
    path = source / f"{binary}_manifest.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise RuntimeError(f"Invalid manifest {path}: {error}") from error


def codegen_inputs(project):
    source, _, _ = layout(project)
    data = manifest(project)
    entries = [data.get("entrypoint", {}), *data.get("modules", [])]
    return [(source / item["file_path"]).resolve() for item in entries if "file_path" in item]


def artifacts(project):
    source, build, binary = layout(project)
    modules = manifest(project).get("modules", [])
    return [build / binary, build / "librexruntimed.dylib", build / "librexgpu-xenosd.dylib",
            build / "libTracyClientd.dylib", build / "vulkan/lib/libMoltenVK.dylib",
            build / "vulkan/lib/libvulkan.1.dylib",
            build / "vulkan/share/vulkan/icd.d/MoltenVK_icd.json",
            *[build / f"lib{binary}_{Path(item['guest_path']).stem}.dylib" for item in modules],
            *[ROOT / "game-work/game-data" / item["guest_path"] for item in modules],
            ROOT / "game-work/game-data/default.xex", source / f"{binary}_manifest.toml"]


def report(project, digest=False):
    source, build, binary = layout(project)
    packages, _ = dependencies()
    tools = {}
    for name in ("cmake", "ninja", "git", "clang", "clang++"):
        executable = f"/usr/bin/{name}" if name.startswith("clang") else shutil.which(name)
        executable = executable if executable and Path(executable).is_file() else None
        tools[name] = {"path": executable}
        if executable:
            code, version = capture([executable, "--version"])
            tools[name].update(returncode=code, version=version.splitlines()[0] if version else "")
    submodules = capture(["git", "submodule", "status", "--recursive"], SDK)[1] if tools["git"]["path"] and (SDK / ".git").exists() else "unavailable"
    source_files = [source / "CMakeLists.txt", source / "CMakePresets.json"]
    if project == "x360":
        source_files.append(source / "cmake/decomp_sources.cmake")
    source_files.extend((source / "generated/rexglue.cmake", SDK / "CMakePresets.json", Path(__file__)))
    return {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "root": str(ROOT),
            "project": project, "python": sys.version.split()[0], "platform": platform.platform(),
            "machine": platform.machine(), "tools": tools,
            "repositories": [git_state(path) for path in (ROOT, source, SDK, SDK / "thirdparty/moltenvk")],
            "sdk_submodules": submodules, "local_dependencies": {name: file_info(path) for name, path in packages.items()},
            "artifacts": [file_info(path, digest) for path in artifacts(project)],
            "codegen_inputs": [file_info(path, digest) for path in codegen_inputs(project)],
            "source_files": [file_info(path, digest) for path in source_files],
            "configuration": {"sdk_preset": "mac-arm64", "sdk_build_preset": "mac-arm64-debug",
                              "game_preset": "mac-arm64-debug", "game_build": str(build)}}


def preflight(project, building=False):
    source, build, binary = layout(project)
    missing = []
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        missing.append("this workflow requires macOS ARM64")
    required = [ROOT / "game-work/game-data/default.xex", source / f"{binary}_manifest.toml"]
    if building:
        required += [SDK / "CMakePresets.json", source / "CMakePresets.json", source / "generated/rexglue.cmake",
                     SDK_BUILD / "CMakeCache.txt", *dependencies()[0].values(), *codegen_inputs(project)]
        for name in ("cmake", "ninja", "git"):
            if not shutil.which(name):
                missing.append(f"tool: {name}")
        required += [Path("/usr/bin/clang"), Path("/usr/bin/clang++")]
        if (SDK / ".git").exists() and shutil.which("git"):
            code, modules = capture(["git", "submodule", "status", "--recursive"], SDK)
            if code or any(line.startswith(("-", "U")) for line in modules.splitlines()):
                missing.append("SDK submodules are missing or conflicted; inspect doctor output")
    else:
        required += artifacts(project)
        if not list(build.glob(f"lib{binary}_*.dylib")):
            missing.append("no staged guest module libraries; build the game first")
    missing += [str(path) for path in required if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError("Required local files/tools unavailable:\n  " + "\n  ".join(dict.fromkeys(missing)))


def build_plan(project, jobs):
    source, _, binary = layout(project)
    common = ["-DCMAKE_C_COMPILER=/usr/bin/clang", "-DCMAKE_CXX_COMPILER=/usr/bin/clang++", *dependencies()[1]]
    return [(SDK, ["cmake", "--preset", "mac-arm64", *common]),
            (SDK, ["cmake", "--build", "--preset", "mac-arm64-debug", "--parallel", str(jobs)]),
            (source, ["cmake", "--preset", "mac-arm64-debug", *common, f"-DREXSDK_DIR={SDK}",
                      f"-DREXGLUE_ROOT={SDK}", f"-DREXGLUE_MOLTENVK_ICD={SDK / 'cmake/MoltenVK_icd.json'}"]),
            # DLL sources aren't declared outputs of the template's codegen rule.
            # Finish writing them before Ninja decides which objects need rebuilding.
            (source, ["cmake", "--build", "--preset", "mac-arm64-debug", "--target",
                      f"{binary}_codegen", "--parallel", str(jobs)]),
            (source, ["cmake", "--build", "--preset", "mac-arm64-debug", "--parallel", str(jobs)])]


def launch_plan(args, checkpoint):
    _, build, binary = layout(args.project)
    user_data = ROOT / "out/user-data" / args.project
    return build, [str(build / binary), f"--game_data_root={ROOT / 'game-work/game-data'}",
                   "--gpu_plugin=xenos", f"--protect_zero={args.protect_zero}",
                   f"--fullscreen={str(args.fullscreen).lower()}", "--window_width=960", "--window_height=540",
                   f"--mnk_mode={str(args.keyboard).lower()}",
                   f"--log_level={args.log_level}", f"--log_file={checkpoint / 'runtime.log'}",
                   f"--user_data_root={user_data}", f"--cache_root={user_data / 'cache'}"]


def show_plan(plan, environment=None):
    if environment:
        print("Environment:", shlex.join([f"{key}={value}" for key, value in environment.items()]))
    for cwd, command in plan:
        print(f"Working directory: {cwd}\n  {shlex.join(command)}")


def run(command, cwd, log, environment, seconds=None):
    started = time.monotonic()
    reason = "exited"
    with log.open("wb") as stream:
        process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                   env={**os.environ, **environment}, start_new_session=True)
        print(f"PID {process.pid}; output: {log}", flush=True)
        try:
            process.wait(timeout=seconds)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            reason = "time_limit" if isinstance(error, subprocess.TimeoutExpired) else "interrupted"
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    return {"command": command, "cwd": str(cwd), "pid": process.pid, "returncode": process.returncode,
            "reason": reason, "elapsed_seconds": round(time.monotonic() - started, 3), "log": str(log)}


def bounded_integer(minimum, maximum):
    def convert(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return number
    return convert


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("doctor", "build", "launch", "smoke", "checkpoint"):
        command = commands.add_parser(action)
        command.add_argument("--project", choices=PROJECTS, default="arcade")
        if action == "doctor":
            command.add_argument("--json", action="store_true", help="full read-only inventory")
        else:
            command.add_argument("--dry-run", action="store_true", help="print the plan; create nothing")
            command.add_argument("--note", default="", help="annotation saved with the checkpoint")
        if action == "build":
            command.add_argument("--jobs", type=bounded_integer(1, MAX_JOBS), default=min(4, os.cpu_count() or 1))
        if action in ("launch", "smoke"):
            command.add_argument("--fullscreen", action="store_true", help="use fullscreen instead of a 960x540 window")
            command.add_argument("--keyboard", action="store_true", help="enable the runtime's physical keyboard controls")
            command.add_argument("--test-directory", type=Path, help="explicitly enable the local test bridge in this directory")
            command.add_argument("--protect-zero", choices=("false", "true"), default="false",
                                 help="experimental compatibility default: false")
            command.add_argument("--log-level", choices=("trace", "debug", "info", "warn", "error"), default="info")
        if action == "smoke":
            command.add_argument("--seconds", type=bounded_integer(1, 300), default=30)
    args = parser.parse_args()
    if args.action == "doctor":
        state = report(args.project)
        if args.json:
            print(json.dumps(state, indent=2))
        else:
            print(f"Workspace: {ROOT}\nProject: {args.project}\nPython: {state['python']} ({state['machine']})")
            for name, item in state["tools"].items():
                print(f"{name}: {item.get('version', 'MISSING')} ({item['path']})")
            for repo in state["repositories"]:
                print(f"Git {repo['path']}: {repo.get('revision', 'not a repository')} dirty={repo.get('dirty', 'n/a')}")
                if repo.get("status"):
                    print(repo["status"])
            for item in [*state["local_dependencies"].values(), *state["artifacts"], *state["codegen_inputs"]]:
                print(f"{'OK' if item['exists'] else 'MISSING'} {item['path']}")
        for building in (True, False):
            try:
                preflight(args.project, building)
            except RuntimeError as error:
                print(str(error), file=sys.stderr)
                return 1
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    checkpoint = ROOT / "out/checkpoints" / f"{stamp}-{args.action}-{args.project}"
    environment = {}
    if args.action in ("launch", "smoke"):
        environment = {"MVK_CONFIG_LOG_LEVEL": "1",
                       "FUTARI_TEST_DIR": str(args.test_directory.resolve()) if args.test_directory else ""}
    plan = build_plan(args.project, args.jobs) if args.action == "build" else []
    if args.action in ("launch", "smoke"):
        plan = [launch_plan(args, checkpoint)]
        print(f"Experimental launch: protect_zero={args.protect_zero}; base game data; no synthetic input or screenshots.")
        if args.test_directory:
            print("Local test bridge enabled explicitly; this helper sends no test commands.")
    show_plan(plan, environment)
    print(f"Checkpoint: {checkpoint}")
    if args.action == "smoke":
        print(f"Smoke duration: {args.seconds}s; survival is not gameplay validation.")
    if args.dry_run:
        return 0
    if args.action != "checkpoint":
        preflight(args.project, args.action == "build")
    checkpoint.mkdir(parents=True)
    metadata = {"action": args.action, "note": args.note, "environment_overrides": environment,
                "before": report(args.project, digest=True), "steps": [], "status": "running"}
    metadata_path = checkpoint / "checkpoint.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    success = True
    try:
        for index, (cwd, command) in enumerate(plan, 1):
            step = run(command, cwd, checkpoint / f"{index:02d}-{args.action}.log", environment,
                       args.seconds if args.action == "smoke" else None)
            metadata["steps"].append(step)
            fatal_lines = []
            if args.action in ("launch", "smoke"):
                for log in (Path(step["log"]), checkpoint / "runtime.log"):
                    if log.is_file():
                        with log.open(errors="replace") as stream:
                            fatal_lines.extend(line.strip() for line in stream if FATAL.search(line))
                step["fatal_signatures"] = fatal_lines[:20]
            expected_timeout = args.action == "smoke" and step["reason"] == "time_limit"
            success = not fatal_lines and (expected_timeout or (step["reason"] == "exited" and step["returncode"] == 0))
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            if not success:
                break
        metadata["status"] = "completed" if success else "failed"
    except (OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        metadata.update(status="failed", error=str(error))
        success = False
    finally:
        metadata["after"] = report(args.project, digest=True)
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"{metadata['status']}: {metadata_path}")
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"futari: {error}", file=sys.stderr)
        sys.exit(1)
