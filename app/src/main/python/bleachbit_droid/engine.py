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

from .backends import BackendError, PrivilegeBackend, Stats
from .capabilities import CAPABILITY_LABELS, missing
from .cleaners import all_cleaners, by_name
from .models import (
    Action,
    Capability,
    CleanerSpec,
    CleanReport,
    Entry,
    Kind,
    LogRecord,
    Result,
    ScanReport,
)


_PLACEHOLDER = re.compile(r"<([a-z_]+)>")

#: How many deletions to hand the backend at once. Big enough that the shell
#: round trip stops dominating, small enough that the progress panel keeps
#: moving instead of jumping in one block at the end.
DELETE_FLUSH = 100


# ---------------------------------------------------------------------------
# path expansion
# ---------------------------------------------------------------------------

def expand(path: str, roots: dict[str, str]) -> str:
    """Replace ``<name>`` tokens using ``roots``.

    An unknown token is an error, and so is an **empty or relative** one.
    ``<app_cache>/**`` with an empty root expands to ``/**``, whose `find`
    start directory is ``/`` — the scan would walk the whole filesystem and
    the apply would delete every file it could reach, APKs included. Kotlin
    fills these from ``context.cacheDir``/``filesDir`` so they are never
    empty today, but one ``?: ""`` away from that is a wiped device, which is
    too cheap a mistake to leave to the caller.
    """

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in roots:
            raise KeyError(f"unknown path placeholder <{key}>")
        value = roots[key]
        if not value or not value.startswith("/"):
            raise BackendError(
                f"path placeholder <{key}> is {value!r}; expected an absolute path"
            )
        return value

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
        if spec.kind is Kind.PACKAGE and not backend.supports_package_clear:
            continue
        if spec.kind is Kind.SQLITE and not backend.can_vacuum(spec.requires):
            continue
        result.append(spec)
    return result


#: Czech labels for the non-capability entries of a cleaner's ``missing`` list.
#: The UI prints these verbatim, so they have to read as sentences fragments,
#: not as internal tokens.
MISSING_LABELS: dict[str, str] = {
    "shell": "přístup k shellu",
    "sqlite3": "nástroj sqlite3",
    "sqlite3_as_owner": "sqlite3 nemůže běžet pod uid vlastníka databáze",
    "package_clear": "pm clear na tomto zařízení neodpovídá",
}


def describe(backend: PrivilegeBackend) -> list[dict]:
    """Registry view used by the UI: every cleaner + whether it is runnable."""
    out = []
    for spec in all_cleaners():
        miss: list = list(missing(spec.requires, backend.capabilities))
        if spec.kind is Kind.PACKAGE and not backend.supports_packages:
            if "shell" not in miss:
                miss.append("shell")
        elif spec.kind is Kind.PACKAGE and not backend.supports_package_clear:
            # The tier has the privilege, the device just will not answer.
            if "package_clear" not in miss:
                miss.append("package_clear")
        if spec.kind is Kind.SQLITE and not backend.can_vacuum(spec.requires):
            # Two different reasons, and the user can only act on one of them:
            # a missing tool is a bug, a database we may not touch as its owner
            # is a permanent property of the tier.
            token = "sqlite3" if not backend.supports_sqlite else "sqlite3_as_owner"
            if token not in miss:
                miss.append(token)
        item = spec.to_dict()
        item["enabled"] = not miss
        item["missing"] = [getattr(m, "value", m) for m in miss]
        item["missing_labels"] = [
            CAPABILITY_LABELS[m] if isinstance(m, Capability)
            else MISSING_LABELS.get(m, str(m))
            for m in miss
        ]
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
        # Remembered so `cleaner_done` can report what *this* cleaner found.
        # It used to send the running total of the whole scan, which the UI
        # then printed next to the cleaner's name as if it were its own.
        base_bytes = report.total_bytes
        base_entries = len(report.entries)
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
                # Zero-length files used to be dropped here, which meant they
                # were scanned, reported and then never removed: a clean left
                # them behind forever. `list_sized()` only ever yields files
                # (never the glob root, never a directory), so an empty entry
                # is a real file that belongs to the cleaner.
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
            bytes=report.total_bytes - base_bytes,
            entries=len(report.entries) - base_entries,
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

    # Pass the callback down: scanning is the long part of a clean on a shell
    # backend, and without this the UI showed nothing at all between
    # "Spouštím čištění…" and the first delete.
    scanned = scan(backend, roots, names, unused_packages, on_event)
    total = len(scanned.entries)
    emit("scanned", entries=total, bytes=scanned.total_bytes)

    def record_deleted(index: int, entry: Entry, stats: Stats) -> None:
        report.deleted += stats.files
        report.freed_bytes += stats.bytes
        if not stats.size_known:
            report.unknown += 1
        report.log.append(
            LogRecord(
                backend=backend.name,
                cleaner=entry.cleaner,
                path=entry.path,
                action=Action.DELETE,
                bytes=stats.bytes,
                result=Result.OK,
                reason="" if stats.size_known else "velikost na této úrovni nezjistitelná",
            )
        )
        emit(
            "deleted",
            index=index,
            total=total,
            path=entry.path,
            cleaner=entry.cleaner,
            bytes=stats.bytes,
            size_known=stats.size_known,
        )

    def record_failed(index: int, entry: Entry, reason: str) -> None:
        report.failed += 1
        report.log.append(
            LogRecord(
                backend=backend.name,
                cleaner=entry.cleaner,
                path=entry.path,
                action=Action.DELETE,
                bytes=entry.size,
                result=Result.FAILED,
                reason=reason,
            )
        )
        emit(
            "failed",
            index=index,
            total=total,
            path=entry.path,
            cleaner=entry.cleaner,
            bytes=entry.size,
            reason=reason,
        )

    # Plain file deletions are batched (see PrivilegeBackend.delete_many): on a
    # shell backend the per-file path cost four commands per file, so clearing
    # /data/dalvik-cache meant thousands of round trips for a scan that had
    # already measured everything in two. Reporting stays per file and in scan
    # order, so the log and the progress panel look exactly as before.
    pending: list[tuple[int, Entry]] = []

    def flush() -> None:
        if not pending:
            return
        try:
            results = backend.delete_many([(e.path, e.size) for _, e in pending])
        except (BackendError, OSError) as exc:
            results = {e.path: str(exc) for _, e in pending}
        for index, entry in pending:
            reason = results.get(entry.path, "backend nenahlásil tuto cestu")
            if reason:
                record_failed(index, entry, reason)
            else:
                record_deleted(index, entry, Stats(files=1, bytes=entry.size))
        pending.clear()

    for index, entry in enumerate(scanned.entries, start=1):
        if not backend.can(entry.requires):
            flush()
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

        # Packages and databases are one device operation each, so they are
        # handled individually — and they flush the batch first to keep every
        # report line in scan order.
        is_sqlite = bool(entry.cleaner) and by_name(entry.cleaner).kind is Kind.SQLITE
        if entry.package or is_sqlite:
            flush()
            try:
                if entry.package:
                    stats = backend.clear_app_cache(entry.package)
                else:
                    stats = backend.vacuum_db(entry.path)
            except (BackendError, OSError) as exc:
                record_failed(index, entry, str(exc))
                continue
            record_deleted(index, entry, stats)
            continue

        pending.append((index, entry))
        if len(pending) >= DELETE_FLUSH:
            flush()

    flush()

    emit(
        "done",
        freed_bytes=report.freed_bytes,
        deleted=report.deleted,
        failed=report.failed,
        unknown=report.unknown,
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
