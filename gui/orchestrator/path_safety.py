from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


class PathSafetyError(ValueError):
    pass


def path_has_env_segment(path_value: str) -> bool:
    normalized = str(path_value).replace("\\", "/")
    for part in normalized.split("/"):
        lowered = part.lower()
        if lowered == ".env" or lowered.startswith(".env."):
            return True
    return False


def validate_relative_project_path(path_value: str) -> str:
    if not path_value or not str(path_value).strip():
        raise PathSafetyError("Path must be a non-empty project-relative path.")

    raw = str(path_value).replace("\\", "/")
    if PureWindowsPath(path_value).is_absolute() or PurePosixPath(raw).is_absolute():
        raise PathSafetyError("Path must not be absolute.")
    if path_has_env_segment(raw):
        raise PathSafetyError("Path must not reference .env files.")

    parts = PurePosixPath(raw).parts
    if any(part in {"..", ""} for part in parts):
        raise PathSafetyError("Path must not contain .. segments.")
    return raw


def ensure_child_path(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise PathSafetyError(f"Path escapes allowed root: {candidate}")
    return resolved_candidate
