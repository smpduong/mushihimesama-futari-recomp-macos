"""Canonical path checks for outputs produced from user-owned inputs."""
from pathlib import Path

try:
    from .build_from_game import BuildError, canonical, git_worktree_roots, overlaps
except ImportError:
    from build_from_game import BuildError, canonical, git_worktree_roots, overlaps


def validate_output_path(output: Path, protected_paths, *, allow_existing_directory=False) -> Path:
    """Reject unsafe, aliased, broad, or input-overlapping output locations."""
    raw = Path(output).expanduser()
    target = canonical(raw)
    if target in {Path(target.anchor), Path.home().resolve(strict=True)} or len(target.parts) < 4:
        raise BuildError(f"output path is too broad: {target}")
    if target.exists():
        if target.is_symlink():
            raise BuildError(f"output path is a symlink: {target}")
        if not allow_existing_directory or not target.is_dir():
            raise BuildError(f"output path already exists or is not a directory: {target}")

    protected = {canonical(Path(item)) for item in protected_paths}
    protected.update(git_worktree_roots(target))
    for item in tuple(protected):
        protected.update(git_worktree_roots(item))
    for item in protected:
        if overlaps(target, item):
            raise BuildError(f"output path overlaps a repository or input: {target} and {item}")
    return target


def package_input_paths(paths):
    """Protect each supplied package and its containing input directory."""
    protected = []
    for item in paths:
        path = canonical(Path(item))
        if path.is_symlink() or not path.is_file():
            raise BuildError(f"package input is missing or unsafe: {path}")
        protected.extend((path, path.parent))
    return protected
