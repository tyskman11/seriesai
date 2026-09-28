from __future__ import annotations

from pathlib import Path


def project_relative(project_root: Path, path: Path | str) -> str:
    """Persist paths relative to the portable project root, with forward slashes."""
    return Path(path).resolve().relative_to(project_root.resolve()).as_posix()


def project_path(project_root: Path, stored_path: Path | str) -> Path:
    path = Path(stored_path)
    return path if path.is_absolute() else project_root / path
