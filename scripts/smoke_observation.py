#!/usr/bin/env python3
"""Pure path, log, and process-evidence parsing for package_smoke.

This module performs observations only. Process ownership, subprocess calls,
signals, cleanup, and smoke acceptance remain in package_smoke.py.
"""
import os
import hashlib
import fnmatch
from pathlib import Path
import re
import stat


class ObservationError(RuntimeError):
    """An observation failed for a reason other than a normal rotation race."""


def _vanished(error):
    # ENOENT is an ordinary rotation race for an individual file. ENOTDIR
    # usually means the observed Logs path was replaced with a non-directory,
    # which is an unknown observation and must fail closed.
    return isinstance(error, FileNotFoundError)


def _read_bytes(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("log target is not a regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            return stream.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _read_text(path):
    return _read_bytes(path).decode("utf-8", errors="replace")


def _read_from_offset(path, state):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("log target is not a regular file")
        identity = (info.st_dev, info.st_ino)
        same_file = (isinstance(state, dict)
                     and (state.get("device"), state.get("inode")) == identity)
        offset = state.get("offset", 0) if same_file else 0
        if info.st_size < offset:
            same_file = False
            offset = 0
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            stream.seek(offset)
            text = stream.read().decode("utf-8", errors="replace")
            return identity, stream.tell(), text, (state.get("tail", "") if same_file else "")
    finally:
        if fd >= 0:
            os.close(fd)


def _list_regular_files(directory, pattern):
    """List matching regular non-symlink files without pathlib.glob suppression."""
    try:
        with os.scandir(directory) as entries:
            rows = []
            for entry in entries:
                if not fnmatch.fnmatchcase(entry.name, pattern):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    if _vanished(exc):
                        continue
                    raise ObservationError(f"cannot inspect log entry {entry.path}: {exc}") from exc
                if stat.S_ISREG(info.st_mode):
                    rows.append((Path(entry.path), info))
            return rows
    except OSError as exc:
        if _vanished(exc):
            return []
        raise ObservationError(f"cannot list log directory {directory}: {exc}") from exc


def observe_child_start_markers(log_dir):
    """Read complete marker records; disappearing rotated files are skipped."""
    log_dir = Path(log_dir)
    console_logs = [path for path, _info in _list_regular_files(log_dir, "*-console.log")]
    console_logs.sort()
    marker_re = re.compile(
        r"FUTARI_DIAGNOSTIC_CHILD_STARTED pid=(\d+) content=(\S+) log=(.*?) "
        r"\(process start only; not guest-init proof\)")
    legacy_marker_re = re.compile(r"FUTARI_DIAGNOSTIC_CHILD_STARTED pid=(\d+) content=(\S+) log=(\S+)")
    records = []
    for path in console_logs:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            text = _read_text(path)
        except OSError as exc:
            if _vanished(exc):
                continue
            raise ObservationError(f"cannot read launcher console log {path}: {exc}") from exc
        for line in text.splitlines():
            match = marker_re.search(line) or legacy_marker_re.search(line)
            if match:
                records.append({"pid": int(match.group(1)), "content": match.group(2),
                                "log": match.group(3), "line": line})
    return records


def retain_child_start_marker(previous, records, conflict_already_observed=False):
    """Preserve first complete marker, flagging ambiguity or later mutation."""
    if len(records) > 1:
        return previous, True
    if not records:
        return previous, conflict_already_observed
    current = records[0]
    if previous is None:
        return current, conflict_already_observed
    return previous, conflict_already_observed or current != previous


def verified_path_spellings(path):
    canonical = os.path.realpath(path)
    spellings = {canonical}
    for alias_root, canonical_root in (("/tmp", "/private/tmp"),
                                       ("/var", "/private/var")):
        if canonical.startswith(canonical_root + "/") and \
                os.path.realpath(alias_root) == os.path.realpath(canonical_root):
            spellings.add(canonical.replace(canonical_root + "/", alias_root + "/", 1))
    return spellings


def child_log_candidates(marker_log, user_data):
    """Return safe current/rotated logs newest-first under isolated UserData.

    A file disappearing during enumeration, stat, or resolution is normal log
    rotation. Permission failures and other I/O errors are surfaced, not
    converted into an empty observation.
    """
    path = Path(marker_log)
    if not path.is_absolute():
        return []
    try:
        user_root = Path(user_data).resolve(strict=True)
        logs_root = user_root / "Logs"
        if str(path.parent) not in verified_path_spellings(logs_root):
            return []
        parent = path.parent.resolve(strict=True)
        parent.relative_to(user_root)
    except OSError as exc:
        if _vanished(exc):
            return []
        raise ObservationError(f"cannot resolve child log directory: {exc}") from exc
    except ValueError:
        return []
    listed = [parent / path.name]
    listed.extend(sorted(candidate for candidate, _info in
                         _list_regular_files(parent, path.stem + ".*.log")))
    snapshots = []
    for candidate in dict.fromkeys(listed):
        try:
            info = candidate.lstat()
        except OSError as exc:
            if _vanished(exc):
                continue
            raise ObservationError(f"cannot inspect child log {candidate}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(user_root)
            # Recheck after resolution: a swapped symlink or removed path must
            # not become evidence for an outside file.
            current = candidate.lstat()
        except OSError as exc:
            if _vanished(exc):
                continue
            raise ObservationError(f"cannot resolve child log {candidate}: {exc}") from exc
        except ValueError:
            continue
        same_file = (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino)
        if stat.S_ISREG(current.st_mode) and same_file:
            snapshots.append((current.st_mtime_ns, resolved))
    snapshots.sort(key=lambda row: row[0], reverse=True)
    return [path for _, path in snapshots]


def resolve_child_log_path(marker_log, user_data):
    paths = child_log_candidates(marker_log, user_data)
    return paths[0] if paths else None


def title_preparation_observation(marker_log, user_data):
    marker = "Initializing shader storage for title 435A07D7..."
    for path in child_log_candidates(marker_log, user_data):
        try:
            contents = _read_text(path)
        except OSError as exc:
            if _vanished(exc):
                continue
            raise ObservationError(f"cannot read child log {path}: {exc}") from exc
        if marker in contents:
            digest = hashlib.sha256(contents.encode("utf-8")).hexdigest()
            return path, contents, digest
    return None


def read_log_snapshot(path):
    """Read one regular log, returning None only when rotation removed it."""
    try:
        return _read_text(path)
    except OSError as exc:
        if _vanished(exc):
            return None
        raise ObservationError(f"cannot read smoke log {path}: {exc}") from exc


def title_preparation_log(marker_log, user_data):
    observation = title_preparation_observation(marker_log, user_data)
    return observation[0] if observation else None


def title_observation_for_identity(identity, cached_identity, cached_observation,
                                   marker_log, user_data):
    """Reuse one immutable log snapshot only for its exact child identity."""
    if identity is not None and identity == cached_identity and cached_observation is not None:
        return cached_observation
    return title_preparation_observation(marker_log, user_data)


def read_new_log_text(user_data, offsets):
    """Read appended log bytes; rotation is tolerated, hard I/O is not."""
    try:
        root = Path(user_data).resolve(strict=True)
    except OSError as exc:
        if _vanished(exc):
            return ""
        raise ObservationError(f"cannot resolve isolated UserData: {exc}") from exc
    listed = [path for path, _info in _list_regular_files(root / "Logs", "*.log")]
    chunks = []
    for path in listed:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            key = str(path)
            state = offsets.get(key)
            identity, position, chunk, tail = _read_from_offset(resolved, state)
            # Only publish the new offset after a successful read.
            chunks.append(tail + chunk)
            offsets[key] = {"device": identity[0], "inode": identity[1],
                            "offset": position, "tail": (tail + chunk)[-512:]}
        except OSError as exc:
            if _vanished(exc):
                continue
            raise ObservationError(f"cannot read isolated log {path}: {exc}") from exc
        except ValueError:
            continue
    return "\n".join(chunks)


def read_log_files(log_dir):
    """Read final log snapshots, skipping rotation races but surfacing hard I/O."""
    log_dir = Path(log_dir)
    paths = [path for path, _info in _list_regular_files(log_dir, "*.log")]
    paths.sort()
    records = []
    for path in paths:
        try:
            info = path.lstat()
        except OSError as exc:
            if _vanished(exc):
                continue
            raise ObservationError(f"cannot inspect final smoke log {path}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(log_dir.resolve(strict=True))
            payload = _read_bytes(resolved)
        except OSError as exc:
            if _vanished(exc):
                continue
            raise ObservationError(f"cannot read final smoke log {path}: {exc}") from exc
        except ValueError:
            continue
        records.append((resolved, payload))
    return records


def parse_process_row(line):
    """Parse `ps -o pid,ppid,pgid,lstart,command` preserving command spaces."""
    fields = line.strip().split(maxsplit=8)
    if len(fields) != 9:
        return None
    try:
        return {"pid": int(fields[0]), "parent_pid": int(fields[1]),
                "process_group": int(fields[2]), "start_time": " ".join(fields[3:8]),
                "command": fields[8]}
    except ValueError:
        return None


def parse_process_command(command):
    """Split flattened argv at option boundaries while preserving spaces."""
    markers = list(re.finditer(r"\s--([A-Za-z0-9_-]+)=", command))
    if not markers:
        return None
    executable = command[:markers[0].start()].strip()
    arguments = {}
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(command)
        key = marker.group(1)
        value = command[marker.end():end].strip()
        if key in arguments or not value:
            return None
        arguments[key] = value
    return executable, arguments


def command_path_match(command, path, flag=None, trailing_slash=False):
    parsed = parse_process_command(command)
    if parsed is None:
        return None
    executable, arguments = parsed
    if flag is None:
        observed = executable
    else:
        key = flag.removeprefix("--").removesuffix("=")
        observed = arguments.get(key)
    if not observed or not Path(observed).is_absolute() or ".." in Path(observed).parts:
        return None
    if trailing_slash:
        observed_path = Path(observed)
        expected_parent = os.path.realpath(path)
        if (not observed_path.name.endswith(".log")
                or str(observed_path.parent) not in verified_path_spellings(path)
                or os.path.realpath(observed_path.parent) != expected_parent):
            return None
        return str(observed_path)
    if observed in verified_path_spellings(path) and os.path.realpath(observed) == os.path.realpath(path):
        return observed
    return None


def command_is_trial_game(command, trial, app):
    """Require exact relocated executable and canonicalized isolated paths."""
    parsed = parse_process_command(command)
    if parsed is None:
        return False
    executable, _arguments = parsed
    executable_path = app / "Contents/MacOS/futari15x360"
    executable_ok = (executable in verified_path_spellings(executable_path)
                     and os.path.realpath(executable) == os.path.realpath(executable_path))
    game_data_ok = command_path_match(command, app / "Contents/Resources/GameData",
                                      "--game_data_root=") is not None
    profile_name = next((name for name in ("base", "ver101", "blacklabel", "both")
                         if command_path_match(command, trial / "UserData/Profiles" / name,
                                               "--user_data_root=") is not None), None)
    profile_ok = profile_name is not None
    cache_ok = (profile_name is not None and command_path_match(
        command, trial / "UserData/Profiles" / profile_name / "cache", "--cache_root=") is not None)
    log_ok = command_path_match(command, trial / "UserData/Logs", "--log_file=",
                                trailing_slash=True) is not None
    return executable_ok and game_data_ok and profile_ok and cache_ok and log_ok


def trial_path_observations(command, trial, app):
    paths = {"executable": app / "Contents/MacOS/futari15x360",
             "game_data_root": app / "Contents/Resources/GameData"}
    profile = next((trial / "UserData/Profiles" / name for name in
                    ("base", "ver101", "blacklabel", "both")
                    if command_path_match(command, trial / "UserData/Profiles" / name,
                                          "--user_data_root=") is not None), None)
    if profile is not None:
        paths["user_data_root"] = profile
        paths["cache_root"] = profile / "cache"
    paths["log_file"] = trial / "UserData/Logs"
    observations = {}
    for label, expected in paths.items():
        flag = {"game_data_root": "--game_data_root=", "user_data_root": "--user_data_root=",
                "cache_root": "--cache_root=", "log_file": "--log_file="}.get(label)
        matched = command_path_match(command, expected, flag,
                                     trailing_slash=(label == "log_file"))
        canonical = os.path.realpath(matched) if matched and label == "log_file" else os.path.realpath(expected)
        observations[label] = {"raw": matched, "canonical": canonical,
                               "resolved_to_expected": matched is not None}
    return observations


def mapped_contains(mapped_files, expected_path):
    candidates = verified_path_spellings(expected_path)
    expected = os.path.realpath(expected_path)
    for line in mapped_files.splitlines():
        if not line.startswith("n"):
            continue
        observed = line[1:]
        if observed in candidates and os.path.realpath(observed) == expected:
            return True
    return False


def path_within(path, root):
    resolved = Path(os.path.realpath(path))
    resolved_root = Path(os.path.realpath(root))
    try:
        resolved.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def dyld_image_path(line):
    """Return absolute image paths, excluding dyld's non-path status text."""
    record = re.match(r"dyld\[\d+\]:\s*(.+?)\s*$", line)
    if record is None:
        return None
    image = re.match(r"(?:<[^>]+>\s+)?(/\S(?:.*\S)?)\s*$", record.group(1))
    return image.group(1) if image else None
