"""Scan/apply orchestration.

The engine:
  1. expands ``<placeholder>`` tokens in cleaner path patterns,
  2. filters cleaners by the backend's capabilities,
  3. walks the paths, measuring what would be freed,
  4. on apply, deletes through the backend and logs every decision.

Package cleaners (``kind=Kind.PACKAGE``) do not touch files at all: they ask
the backend to clear an app's cache through Android.

The engine never touches the filesystem for mutation itself — it only calls
``backend.measure()`` / ``backend.delete()`` / ``backend.clear_app_cache()``
(AGENTS.md §10).
"""

from __future__ import annotations

from typing import Any, Callable

import re

from .backends import BackendError, PrivilegeBackend
from .capabilities import missing
from .cleaners import all_cleaners, by_name
from .models import (
    Action,
    CleanerSpec,
    CleanReport,
    Entry,
    Kind,
    LogRecord,
    Result,
    ScanReport,
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
    return re.sub(r"/{2,}", "/", expanded)


def _glob(backend: PrivilegeBackend, pattern: str) -> list[str]:
    """Match ``pattern`` in the backend's namespace, not this process's."""
    return backend.expand(pattern)


def _iter_leaves(backend: PrivilegeBackend, path: str) -> list[str]:
    """Return deletable leaves under ``path`` (never the root dir itself)."""
    return backend.leaves(path)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

def select_cleaners(
    backend: PrivilegeBackend,
    names: list[str] | None = None,
) -> list[CleanerSpec]:
    """Return cleaners that ``backend`` can actually run."""
    pool = [by_name(n) for n in names] if names else list(all_cleaners())
    result = []
    for spec in pool:
        if missing(spec.requires, backend.capabilities):
            continue
        if spec.kind is Kind.PACKAGE and not backend.supports_packages:
            continue
        if spec.kind is Kind.SQLITE and not backend.supports_sqlite:
            continue
        result.append(spec)
    return result


def describe(backend: PrivilegeBackend) -> list[dict]:
    """Registry view used by the UI: every cleaner + whether it is runnable."""
    out = []
    for spec in all_cleaners():
        miss = missing(spec.requires, backend.capabilities)
        if spec.kind is Kind.PACKAGE and not backend.supports_packages:
            miss = miss + ["shell"] if "shell" not in miss else miss
        if spec.kind is Kind.SQLITE and not backend.supports_sqlite:
            miss = miss + ["sqlite3"] if "sqlite3" not in miss else miss
        item = spec.to_dict()
        item["enabled"] = not miss
        item["missing"] = list(miss)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# package selection
# ---------------------------------------------------------------------------

def _package_candidates(
    backend: PrivilegeBackend,
    spec: CleanerSpec,
    unused: list[str] | None,
) -> list[str]:
    """Packages a package cleaner should operate on.

    ``unused`` is the list computed by Kotlin from UsageStatsManager and is
    only meaningful for cleaners with ``unused_days`` set. When Kotlin could
    not compute it (permission missing), the cleaner is skipped rather than
    guessing.
    """
    if spec.unused_days is not None:
        return list(unused or [])
    try:
        return backend.installed_packages()
    except BackendError:
        return []


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def scan(
    backend: PrivilegeBackend,
    roots: dict[str, str],
    names: list[str] | None = None,
    unused_packages: list[str] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> ScanReport:
    report = ScanReport(backend=backend.name)
    seen: set[str] = set()

    def emit(kind: str, **fields: Any) -> None:
        """Report progress; a failing callback must never abort a scan."""
        if on_event is None:
            return
        try:
            on_event({"type": kind, **fields})
        except Exception:  # noqa: BLE001 - progress is best effort
            pass

    specs = select_cleaners(backend, names)
    emit("scan_start", backend=backend.name, cleaners=[s.name for s in specs])

    for spec in specs:
        emit("cleaner_start", cleaner=spec.name, description=spec.description)
        if spec.kind is Kind.SQLITE:
            for target in spec.paths:
                if not backend.can(target.requires):
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
                for match in _glob(backend, pattern):
                    if not backend.exists(match) or not backend.is_file(match):
                        continue
                    if match in seen:
                        continue
                    seen.add(match)
                    try:
                        size = backend.size(match)
                    except (BackendError, OSError):
                        continue
                    report.entries.append(
                        Entry(
                            cleaner=spec.name,
                            path=match,
                            size=size,
                            is_dir=False,
                            requires=target.requires,
                        )
                    )
                    report.log.append(
                        LogRecord(
                            backend=backend.name,
                            cleaner=spec.name,
                            path=match,
                            action=Action.SCAN,
                            bytes=size,
                            result=Result.OK,
                            reason="database",
                        )
                    )
            continue

        if spec.kind is Kind.PACKAGE:
            for pkg in _package_candidates(backend, spec, unused_packages):
                key = f"package:{pkg}"
                if key in seen:
                    continue
                seen.add(key)
                report.entries.append(
                    Entry(
                        cleaner=spec.name,
                        path=f"/data/data/{pkg}/cache",
                        size=0,
                        is_dir=True,
                        requires=spec.requires[0],
                        package=pkg,
                        size_known=False,
                    )
                )
                report.log.append(
                    LogRecord(
                        backend=backend.name,
                        cleaner=spec.name,
                        path=pkg,
                        action=Action.SCAN,
                        result=Result.OK,
                        reason="package cache, size known after clearing",
                    )
                )
            continue

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

            # One bulk pass per target: on the shell backends (Shizuku / su)
            # every per-file round trip costs a command, so a target with 2000
            # cache files used to mean 2000 shell calls and a scan that took
            # minutes. list_sized() asks for the whole target at once.
            try:
                sized = backend.list_sized(pattern)
            except (BackendError, OSError) as exc:
                report.log.append(
                    LogRecord(
                        backend=backend.name,
                        cleaner=spec.name,
                        path=pattern,
                        action=Action.SCAN,
                        result=Result.FAILED,
                        reason=str(exc),
                    )
                )
                continue

            for leaf, size in sized:
                if leaf in seen:
                    continue
                if size == 0:
                    continue
                seen.add(leaf)
                report.entries.append(
                    Entry(
                        cleaner=spec.name,
                        path=leaf,
                        size=size,
                        is_dir=False,
                        requires=target.requires,
                    )
                )
                report.log.append(
                    LogRecord(
                        backend=backend.name,
                        cleaner=spec.name,
                        path=leaf,
                        action=Action.SCAN,
                        bytes=size,
                        result=Result.OK,
                    )
                )
                emit(
                    "found",
                    cleaner=spec.name,
                    path=leaf,
                    bytes=size,
                    total_bytes=report.total_bytes,
                    entries=len(report.entries),
                )
        emit(
            "cleaner_done",
            cleaner=spec.name,
            entries=len(report.entries),
            total_bytes=report.total_bytes,
        )
    emit(
        "scan_done",
        entries=len(report.entries),
        total_bytes=report.total_bytes,
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
    unused_packages: list[str] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> CleanReport:
    """Scan, then delete through the backend, reporting progress as it goes.

    ``on_event`` receives one plain dict per step so the UI can show a live log
    the way BleachBit does. A failing callback must never abort a clean, so
    every call is guarded.
    """

    def emit(kind: str, **fields: Any) -> None:
        if on_event is None:
            return
        try:
            on_event({"type": kind, **fields})
        except Exception:  # noqa: BLE001 - progress must not break cleaning
            pass

    report = CleanReport(backend=backend.name, dry_run=dry_run)
    emit("start", backend=backend.name, dry_run=dry_run)

    scanned = scan(backend, roots, names, unused_packages)
    total = len(scanned.entries)
    emit("scanned", entries=total, bytes=scanned.total_bytes)

    for index, entry in enumerate(scanned.entries, start=1):
        if not backend.can(entry.requires):
            reason = f"missing capability {entry.requires.value}"
            report.log.append(
                LogRecord(
                    backend=backend.name,
                    cleaner=entry.cleaner,
                    path=entry.path,
                    action=Action.SKIP,
                    bytes=entry.size,
                    result=Result.DENIED,
                    reason=reason,
                )
            )
            emit(
                "skip",
                index=index,
                total=total,
                path=entry.path,
                cleaner=entry.cleaner,
                bytes=entry.size,
                reason=reason,
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
                    reason="" if entry.size_known else "size known after clearing",
                )
            )
            emit(
                "would_delete",
                index=index,
                total=total,
                path=entry.path,
                cleaner=entry.cleaner,
                bytes=entry.size,
            )
            continue

        try:
            if entry.package:
                stats = backend.clear_app_cache(entry.package)
            elif entry.cleaner and by_name(entry.cleaner).kind is Kind.SQLITE:
                stats = backend.vacuum_db(entry.path)
            else:
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
            emit(
                "failed",
                index=index,
                total=total,
                path=entry.path,
                cleaner=entry.cleaner,
                bytes=entry.size,
                reason=str(exc),
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
        emit(
            "deleted",
            index=index,
            total=total,
            path=entry.path,
            cleaner=entry.cleaner,
            bytes=stats.bytes,
        )

    emit(
        "done",
        freed_bytes=report.freed_bytes,
        deleted=report.deleted,
        failed=report.failed,
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
