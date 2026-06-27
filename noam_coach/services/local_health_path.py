"""Safe resolution of local Apple Health export paths.

This feature is intentionally local-only: the Telegram bot process must run on
exactly the Windows computer that owns the path the user sends in chat.  A bot
running on a remote server cannot access ``C:\\Users\\...`` on the user's PC.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_EXPORT_SUFFIXES = {".zip", ".xml"}
_PATH_PREFIXES = ("/importpath", "importpath:", "path:", "נתיב:", "ייבוא:")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class LocalHealthPathError(ValueError):
    """Raised when a local path is unsafe, missing or does not contain an export."""


@dataclass(frozen=True)
class LocalHealthSelection:
    """A validated local export selected from a file path or directory."""

    path: Path
    from_directory: bool
    candidate_count: int


def normalize_path_text(text: str) -> str:
    """Remove the optional command/prefix and one matching pair of quotes."""
    value = (text or "").strip()
    lowered = value.casefold()
    for prefix in _PATH_PREFIXES:
        if lowered.startswith(prefix.casefold()):
            value = value[len(prefix) :].strip()
            break
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def looks_like_local_health_path(text: str) -> bool:
    """Conservatively detect a path without hijacking ordinary free text."""
    value = normalize_path_text(text)
    if not value:
        return False
    lowered = value.casefold()
    if lowered.endswith(tuple(SUPPORTED_EXPORT_SUFFIXES)):
        return True
    if _WINDOWS_ABSOLUTE_RE.match(value):
        return True
    return value.startswith(("/", "~/", "./", "../"))


def allowed_roots_from_text(raw: str, *, default_home: Path | None = None) -> tuple[Path, ...]:
    """Parse semicolon/newline separated roots; default to the current user home."""
    pieces = [part.strip() for part in re.split(r"[;\n]+", raw or "") if part.strip()]
    if not pieces:
        pieces = [str(default_home or Path.home())]
    roots: list[Path] = []
    for piece in pieces:
        expanded = os.path.expandvars(os.path.expanduser(piece))
        root = Path(expanded)
        if not root.is_absolute():
            raise LocalHealthPathError(
                f"שורש מורשה חייב להיות נתיב מלא: {piece}"
            )
        roots.append(root.resolve(strict=False))
    return tuple(dict.fromkeys(roots))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_under_roots(path: Path, roots: tuple[Path, ...]) -> None:
    if not any(_is_within(path, root) for root in roots):
        allowed = "; ".join(str(root) for root in roots)
        raise LocalHealthPathError(
            "הנתיב נמצא מחוץ לתיקיות המורשות. "
            f"תיקיות מותרות: {allowed}"
        )


def resolve_local_health_export(
    text: str,
    *,
    allowed_roots: tuple[Path, ...],
    max_bytes: int,
    recursive: bool = False,
) -> LocalHealthSelection:
    """Resolve a local file/folder to one safe ZIP/XML export.

    If a folder is supplied, the newest supported file is selected.  The search
    is non-recursive by default to avoid unexpectedly scanning an entire disk.
    """
    raw = normalize_path_text(text)
    if not raw:
        raise LocalHealthPathError("לא התקבל נתיב.")
    if raw.startswith(("\\\\", "//")):
        raise LocalHealthPathError("נתיבי רשת/UNC אינם מותרים.")

    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        raise LocalHealthPathError("יש לשלוח נתיב מלא, למשל C:\\Users\\...\\Desktop")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LocalHealthPathError("הנתיב לא קיים במחשב שמריץ את הבוט.") from exc

    _validate_under_roots(resolved, allowed_roots)

    from_directory = resolved.is_dir()
    candidates: list[Path]
    if from_directory:
        iterator = resolved.rglob("*") if recursive else resolved.glob("*")
        candidates = []
        for item in iterator:
            if not item.is_file() or item.suffix.casefold() not in SUPPORTED_EXPORT_SUFFIXES:
                continue
            item_resolved = item.resolve(strict=True)
            _validate_under_roots(item_resolved, allowed_roots)
            candidates.append(item_resolved)
        if not candidates:
            scope = "ובתיקיות המשנה" if recursive else "בתיקייה"
            raise LocalHealthPathError(f"לא נמצא קובץ ZIP או XML {scope}.")
        # Newest export wins; ZIP is preferred only when timestamps are equal.
        resolved = max(
            candidates,
            key=lambda path: (path.stat().st_mtime_ns, path.suffix.casefold() == ".zip"),
        )
    elif resolved.is_file():
        candidates = [resolved]
    else:
        raise LocalHealthPathError("הנתיב אינו קובץ או תיקייה רגילים.")

    if resolved.suffix.casefold() not in SUPPORTED_EXPORT_SUFFIXES:
        raise LocalHealthPathError("צריך קובץ ZIP או XML של ייצוא Apple Health.")
    if resolved.is_symlink():
        raise LocalHealthPathError("קישור סמלי אינו מורשה לייבוא.")
    size = resolved.stat().st_size
    if size <= 0:
        raise LocalHealthPathError("קובץ הייצוא ריק.")
    if size > max_bytes:
        raise LocalHealthPathError(
            f"קובץ הייצוא גדול מהמגבלה המקומית ({max_bytes // (1024 * 1024)}MB)."
        )
    return LocalHealthSelection(
        path=resolved,
        from_directory=from_directory,
        candidate_count=len(candidates),
    )
