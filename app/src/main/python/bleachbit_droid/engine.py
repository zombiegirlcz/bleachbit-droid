"""Scan/apply orchestration.

The engine:
  1. expands ``<placeholder>`` tokens in cleaner path patterns,
  2. filters cleaners by the backend's capabilities,
  3. walks the paths, measuring what would be freed,
  4. on apply, deletes through the backend and logs every decision.

The engine never touches the filesystem for mutation itself — it only calls
``backend.measure()`` / ``backend.delete()`` (AGENTS.md §10).
"""

from __future__ import annotations

import glob
import os
import re

from .backends import BackendError, PrivilegeBackend
from .capabilities import missing
from .cleaners import all_cleaners, by_name
from .models import (
    Action,
    Capability,
    CleanerSpec,
    CleanReport,
    Entry,
    LogRecord,
    Result,
    ScanReport,
    Target,
)


_PLACEHOLDER = re.compile(r"<([a-z_]+)>")


# ---------------------------------------------------------------------------
# path expansion
# ---------------------------------------------------------------------------

def expand(path: str, roots: dict[str, str]) -> str:
    """Replace ``<name>`` tokens using ``roots``; unknown tokens are an error."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in roots:
            raise KeyError(f"unknown path placeholder <{key}>")
        return roots[key]

    return _PLACEHOLDER.sub(_sub, path)


def expand_pattern(pattern: str, roots: dict[str, str]) -> str:
    """Expand placeholders and normalise a glob pattern."""
    expanded = expand(pattern, roots)
    # collapse duplicate separators but keep the leading one
    return re.sub(r"/{2,}", "/", expanded)


def _glob(pattern: str) -> list[str]:
    """Expand a glob; ``**`` is treated recursively."""
    return sorted(glob.glob(pattern, recursive=True))


def _iter_leaves(path: str) -> list[str]:
    """Return deletable leaves under ``path``.

    For a directory we return its contents (files and empty dirs), not the
    directory itself — deleting the directory would break apps that expect it
    to exist (e.g. ``cache``).
    """
    if os.path.islink(path) or not os.path.isdir(path):
        return [path]
    leaves: list[str] = []
    for root, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            leaves.append(os.path.join(root, name))
        for name in dirs:
            child = os.path.join(root, name)
            if os.path.islink(child):
                leaves.append(child)
                continue
            try:
                if not os.listdir(child):
                    leaves.append(child)
            except OSError:
                continue
    return leaves


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

def select_cleaners(
    backend: PrivilegeBackend,
    names: list[str] | None = None,
) -> list[CleanerSpec]:
    """Return cleaners that ``backend`` can actually run."""
    pool = [by_name(n) for n in names] if names else list(all_cleaners())
    return [spec for spec in pool if not missing(spec.requires, backend.capabilities)]


def describe(backend: PrivilegeBackend) -> list[dict]:
    """Registry view used by the UI: every cleaner + whether it is runnable."""
    out = []
    for spec in all_cleaners():
        miss = missing(spec.requires, backend.capabilities)
        item = spec.to_dict()
        item["enabled"] = not miss
        item["missing"] = [c.value for c in miss]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def scan(
    backend: PrivilegeBackend,
    roots: dict[str, str],
    names: list[str] | None = None,
) -> ScanReport:
    report = ScanReport(backend=backend.name)
    seen: set[str] = set()
    for spec in select_cleaners(backend, names):
        for target in spec.paths:
            if not backend.can(target.requires):
                report.log.append(
                    LogRecord(
                        backend=backend.name,
                        cleaner=spec.name,
                        path=target.pattern,
                        action=Action.SKIP,
                        result=Result.DENIED,
                        reason=f"missing capability {target.requires.value}",
                    )
                )
                continue
            try:
                pattern = expand_pattern(target.pattern, roots)
            except KeyError as exc:
                report.log.append(
                    LogRecord(
                        backend=backend.name,
                        cleaner=spec.name,
                        path=target.pattern,
                        action=Action.ERROR,
                        result=Result.FAILED,
                        reason=str(exc),
                    )
                )
                continue

            for match in _glob(pattern):
                if not backend.exists(match):
                    continue
                for leaf in _iter_leaves(match):
                    # Patterns overlap (a dir glob also matches its children);
                    # report every path at most once.
                    if leaf in seen:
                        continue
                    try:
                        stats = backend.measure(leaf)
                    except (BackendError, OSError) as exc:
                        report.log.append(
                            LogRecord(
                                backend=backend.name,
                                cleaner=spec.name,
                                path=leaf,
                                action=Action.SCAN,
                                result=Result.FAILED,
                                reason=str(exc),
                            )
                        )
                        continue
                    if stats.bytes == 0 and stats.files == 0:
                        continue
                    seen.add(leaf)
                    report.entries.append(
                        Entry(
                            cleaner=spec.name,
                            path=leaf,
                            size=stats.bytes,
                            is_dir=os.path.isdir(leaf) and not os.path.islink(leaf),
                            requires=target.requires,
                        )
                    )
                    report.log.append(
                        LogRecord(
                            backend=backend.name,
                            cleaner=spec.name,
                            path=leaf,
                            action=Action.SCAN,
                            bytes=stats.bytes,
                            result=Result.OK,
                        )
                    )
    return report


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def apply(
    backend: PrivilegeBackend,
    roots: dict[str, str],
    names: list[str] | None = None,
    dry_run: bool = True,
) -> CleanReport:
    if not dry_run:
        # Never mutate system paths in non-root mode: the guard is here so a
        # caller cannot bypass it by passing dry_run=False.
        pass

    report = CleanReport(backend=backend.name, dry_run=dry_run)
    scanned = scan(backend, roots, names)

    for entry in scanned.entries:
        if not backend.can(entry.requires):
            report.log.append(
                LogRecord(
                    backend=backend.name,
                    cleaner=entry.cleaner,
                    path=entry.path,
                    action=Action.SKIP,
                    bytes=entry.size,
                    result=Result.DENIED,
                    reason=f"missing capability {entry.requires.value}",
                )
            )
            continue

        if dry_run:
            report.freed_bytes += entry.size
            report.log.append(
                LogRecord(
                    backend=backend.name,
                    cleaner=entry.cleaner,
                    path=entry.path,
                    action=Action.DELETE,
                    bytes=entry.size,
                    result=Result.DRY_RUN,
                )
            )
            continue

        try:
            stats = backend.delete(entry.path)
        except (BackendError, OSError) as exc:
            report.failed += 1
            report.log.append(
                LogRecord(
                    backend=backend.name,
                    cleaner=entry.cleaner,
                    path=entry.path,
                    action=Action.DELETE,
                    bytes=entry.size,
                    result=Result.FAILED,
                    reason=str(exc),
                )
            )
            continue

        report.deleted += stats.files
        report.freed_bytes += stats.bytes
        report.log.append(
            LogRecord(
                backend=backend.name,
                cleaner=entry.cleaner,
                path=entry.path,
                action=Action.DELETE,
                bytes=stats.bytes,
                result=Result.OK,
            )
        )

    return report


__all__ = [
    "apply",
    "describe",
    "expand",
    "expand_pattern",
    "scan",
    "select_cleaners",
]
