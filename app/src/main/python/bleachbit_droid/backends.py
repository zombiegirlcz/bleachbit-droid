"""PrivilegeBackend implementations.

Rule of the project (AGENTS.md §10): every filesystem mutation goes through a
backend. No cleaner and no engine may call ``os.remove``/``shutil.rmtree``
directly.

Two kinds of operations exist:
  * path operations  — measure/delete a file or tree
  * package operations — ask Android itself to clear an app's cache
    (`pm clear --cache-only`), optionally after force-stopping it
"""

from __future__ import annotations

import glob
import os
import shutil
import stat
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable

from .models import Capability


class BackendError(RuntimeError):
    pass


@dataclass
class Stats:
    files: int = 0
    bytes: int = 0
    #: False when the backend could not measure what it removed. The ADB tier
    #: cannot read /data/data/<pkg>/cache (uid 2000 gets EACCES, verified on
    #: device), so reporting 0 B there would be a lie the UI then printed as
    #: "Uvolněno 0 B" after successfully clearing hundreds of megabytes.
    size_known: bool = True


class PrivilegeBackend(ABC):
    """Common interface for all three privilege levels."""

    name: str = "abstract"
    capabilities: frozenset[Capability] = frozenset()
    #: package operations need a shell; path-only backends leave this False
    supports_packages: bool = False
    #: database operations need the bundled glibc sqlite3; overridden as a
    #: property by _SqliteMixin
    supports_sqlite: bool = False
    #: Which capabilities' databases this backend may actually rebuild.
    #:
    #: The bundled sqlite3 lives inside *our* app's filesDir (mode 0700) and
    #: can only ever run as our own uid, and the device ships no system
    #: sqlite3 (verified: nothing in /system/bin, /system/xbin or the runtime
    #: apex). So even the root tier cannot VACUUM another app's database as
    #: that app's uid — and doing it as root would leave root-owned journal
    #: files in foreign app data, which is the exact failure this project
    #: already paid for once (AGENTS.md §6/2). Those cleaners are therefore
    #: not offered at all instead of being offered and failing.
    vacuum_capabilities: frozenset[Capability] = frozenset(
        {Capability.APP_DATA, Capability.SHARED_STORAGE}
    )

    # --- introspection -------------------------------------------------
    def can(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def can_vacuum(self, requires: Iterable[Capability]) -> bool:
        """True when a ``kind=sqlite`` cleaner needing ``requires`` can run."""
        if not self.supports_sqlite:
            return False
        return all(cap in self.vacuum_capabilities for cap in requires)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "supports_packages": self.supports_packages,
            "supports_sqlite": self.supports_sqlite,
        }

    # --- filesystem ----------------------------------------------------
    @abstractmethod
    def measure(self, path: str) -> Stats:
        """Return size stats for ``path`` (file, symlink or tree)."""

    @abstractmethod
    def delete(self, path: str) -> Stats:
        """Delete ``path`` and return what was actually removed.

        Must raise ``BackendError`` when the operation is not permitted or
        fails. Must never follow symlinks out of ``path``.
        """

    @abstractmethod
    def exists(self, path: str) -> bool:
        ...

    # --- path enumeration ----------------------------------------------
    #
    # The engine must never call `glob`/`os.walk` itself: on the ADB and root
    # tiers this process cannot see the paths it is asked to clean, so a local
    # enumeration would silently report 0 B. Each backend answers these in the
    # namespace it can actually reach.

    def expand(self, pattern: str) -> list[str]:
        """Return paths matching ``pattern`` as this backend sees them."""
        return sorted(glob.glob(pattern, recursive=True))

    def leaves(self, path: str) -> list[str]:
        """Deletable entries under ``path``; never ``path`` itself."""
        return _iter_leaves(path)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path) and not os.path.islink(path)

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def size(self, path: str) -> int:
        return os.path.getsize(path)

    def list_sized(self, pattern: str) -> list[tuple[str, int]]:
        """Files matching ``pattern`` with their sizes, in one pass.

        This is what ``engine.scan`` uses. The obvious alternative — expand the
        pattern, walk every match, then ``stat`` each leaf — costs one round
        trip per file, which on a shell backend (Shizuku/`su`) means a process
        per file and a scan that takes minutes.
        """
        out: list[tuple[str, int]] = []
        seen: set[str] = set()
        for match in self.expand(pattern):
            for leaf in self.leaves(match):
                if leaf in seen:
                    continue
                # Files only. `_iter_leaves()` also returns *empty directories*
                # (so a clean can tidy up after itself), and the shell backend
                # filters to `-type f -o -type l` on its own. Without this check
                # the two backends disagreed: a local scan offered empty
                # directories as deletable entries, so a recursive cleaner such
                # as staging_tmp would remove the directory tree that holds the
                # tools living in /data/local/tmp.
                if self.is_dir(leaf) and not self.is_file(leaf):
                    continue
                seen.add(leaf)
                try:
                    out.append((leaf, self.size(leaf)))
                except (BackendError, OSError):
                    continue
        return out

    # --- bulk deletion -------------------------------------------------
    def delete_many(self, entries: list[tuple[str, int]]) -> dict[str, str]:
        """Delete several already-measured entries at once.

        ``entries`` is ``(path, size)`` as ``engine.scan`` measured it. The
        return value maps every path to ``""`` when it was removed or to a
        failure reason when it was not, so the caller can still log one line
        per file.

        Why this exists: the per-file ``delete()`` path costs four shell
        commands on a shell backend (``du``, ``test -f``, ``test -d``, ``rm``),
        so clearing /data/dalvik-cache meant thousands of round trips even
        though the scan had already measured every file in two commands.
        Backends that can delete in bulk override this; the default keeps the
        old behaviour for the local tier, where a syscall is free.
        """
        results: dict[str, str] = {}
        for path, _size in entries:
            try:
                self.delete(path)
                results[path] = ""
            except (BackendError, OSError) as exc:
                results[path] = str(exc)
        return results

    # --- packages ------------------------------------------------------
    def clear_app_cache(self, package: str, force_stop: bool = True) -> Stats:
        """Clear one app's cache through Android, not by touching files."""
        raise BackendError(f"backend {self.name!r} cannot clear app caches")

    def installed_packages(self) -> list[str]:
        """Package names visible to this backend."""
        raise BackendError(f"backend {self.name!r} cannot list packages")

    # --- databases -----------------------------------------------------
    def vacuum_db(self, path: str) -> Stats:
        """Rebuild a SQLite database so freed pages actually leave the file.

        ``DELETE`` alone only moves pages onto the free list; the file keeps its
        size and the data stays on disk. ``VACUUM`` rewrites it. Android ships no
        tool that can do this, which is why the app bundles sqlite3.
        """
        raise BackendError(f"backend {self.name!r} cannot vacuum databases")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tree_stats(path: str) -> Stats:
    """Walk ``path`` without following symlinks and sum regular file sizes."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return Stats(files=1, bytes=st.st_size)

    stats = Stats()
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISDIR(est.st_mode):
                        stack.append(entry.path)
                    else:
                        stats.files += 1
                        stats.bytes += est.st_size
        except (FileNotFoundError, NotADirectoryError):
            continue
        except PermissionError as exc:
            raise BackendError(f"permission denied: {current}") from exc
    return stats


def _iter_leaves(path: str) -> list[str]:
    """Return deletable leaves under ``path`` (never the root dir itself)."""
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


def _find_base_and_glob(pattern: str) -> tuple[str, str]:
    """Split a glob pattern into a `find` start dir and a `-path` glob.

    ``find``'s ``*`` already crosses ``/``, so ``**`` is folded to ``*``; that
    keeps recursive patterns working on Android's toybox ``find``, which has no
    ``globstar``.
    """
    first_wild = min(
        (i for i in (pattern.find("*"), pattern.find("?"), pattern.find("[")) if i >= 0),
        default=-1,
    )
    if first_wild < 0:
        return os.path.dirname(pattern) or "/", pattern
    cut = pattern.rfind("/", 0, first_wild)
    base = pattern[:cut] if cut > 0 else "/"
    return base, pattern.replace("**", "*")


def _local_delete(path: str) -> Stats:
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        stats = _tree_stats(path)
        shutil.rmtree(path)
        return stats
    stats = Stats(files=1, bytes=st.st_size)
    os.unlink(path)
    return stats


def shell_quote(value: str) -> str:
    """POSIX single-quote escaping."""
    return "'" + value.replace("'", "'\\''") + "'"


def _validate_package(package: str) -> str:
    """Package names are used inside shell commands — validate strictly."""
    if not package or not all(c.isalnum() or c in "._" for c in package):
        raise BackendError(f"invalid package name: {package!r}")
    if not package[0].isalpha():
        raise BackendError(f"invalid package name: {package!r}")
    return package


class _ShellMixin:
    """Package operations shared by the shell-based backends.

    Also implements path enumeration through the shell: a backend that reaches
    other apps' data cannot see those paths from this process, so `glob` and
    `os.walk` here would return nothing.
    """

    supports_packages = True

    def _shell(self, command: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _shell_ok(self, command: str) -> str:
        return self._shell(command)

    def _run_raw(self, command: str) -> tuple[int, str, str]:  # pragma: no cover
        """Run a command whose non-zero exit is expected (probes)."""
        raise NotImplementedError

    # -- path enumeration -----------------------------------------------
    def expand(self, pattern: str) -> list[str]:
        """Return paths matching ``pattern`` as the shell sees them.

        Android's toybox ``find`` has no ``globstar``, so ``**`` is folded to
        ``*`` (find's ``*`` already crosses ``/``). A non-zero exit is not an
        error: a pattern whose base directory is missing simply matches
        nothing.
        """
        base, glob_pattern = _find_base_and_glob(pattern)
        code, out, _ = self._run_raw(
            f"test -d {shell_quote(base)} && "
            f"find {shell_quote(base)} -path {shell_quote(glob_pattern)}"
        )
        if code != 0:
            return []
        return sorted(line for line in out.splitlines() if line.strip())

    def leaves(self, path: str) -> list[str]:
        """Deletable entries under ``path`` as the shell sees them."""
        if self.is_file(path) and not self.is_dir(path):
            return [path]
        code, out, _ = self._run_raw(
            f"find {shell_quote(path)} -mindepth 1 \\( -type f -o -type l \\)"
        )
        if code != 0:
            return []
        return [line for line in out.splitlines() if line.strip()]

    def is_dir(self, path: str) -> bool:
        code, _, _ = self._run_raw(f"test -d {shell_quote(path)}")
        return code == 0

    def is_file(self, path: str) -> bool:
        code, _, _ = self._run_raw(f"test -f {shell_quote(path)}")
        return code == 0

    def size(self, path: str) -> int:
        code, out, _ = self._run_raw(f"stat -c %s -- {shell_quote(path)}")
        if code != 0:
            raise BackendError(f"cannot stat {path}")
        try:
            return int(out.strip().splitlines()[0])
        except (IndexError, ValueError) as exc:
            raise BackendError(f"cannot parse stat output: {out!r}") from exc

    def list_sized(self, pattern: str) -> list[tuple[str, int]]:
        """One ``find`` + ``stat`` for the whole pattern.

        ``-path`` matches both files that match the glob and everything under a
        directory that matches it (find's ``*`` crosses ``/``), so a single
        command covers the whole cleaner target. ``-exec ... +`` batches the
        stats instead of spawning one process per file.
        """
        base, glob_pattern = _find_base_and_glob(pattern)
        nested = glob_pattern.rstrip("/") + "/*"
        code, out, _ = self._run_raw(
            f"test -d {shell_quote(base)} && "
            f"find {shell_quote(base)} "
            f"\\( -path {shell_quote(glob_pattern)} -o -path {shell_quote(nested)} \\) "
            f"\\( -type f -o -type l \\) "
            f"-exec stat -c '%s\t%n' {{}} +"
        )
        if code != 0:
            return []
        result: list[tuple[str, int]] = []
        seen: set[str] = set()
        for line in out.splitlines():
            if "\t" not in line:
                continue
            size_text, path = line.split("\t", 1)
            try:
                size = int(size_text)
            except ValueError:
                continue
            if path in seen:
                continue
            seen.add(path)
            result.append((path, size))
        return result

    #: Paths per `rm` command. Long enough to amortise the round trip, short
    #: enough to stay well under the kernel's argument limit even with the
    #: long paths under /data/data.
    DELETE_BATCH = 200

    def delete_many(self, entries: list[tuple[str, int]]) -> dict[str, str]:
        """Remove many files with one ``rm`` per batch.

        The scan already measured every file, so there is nothing left to ask
        the device: the only thing needed is the removal itself. A batch that
        reports failure is retried file by file, because `rm` tells us only
        that *something* in the batch could not be removed, and the log has to
        name the file that actually failed.
        """
        results: dict[str, str] = {}
        batch: list[str] = []

        def flush() -> None:
            if not batch:
                return
            args = " ".join(shell_quote(p) for p in batch)
            code, out, err = self._run_raw(f"rm -f -- {args}")
            if code == 0:
                for path in batch:
                    results[path] = ""
            else:
                # Pinpoint the failures; `rm` exits non-zero for the whole
                # batch even when a single file was undeletable.
                for path in batch:
                    one, out1, err1 = self._run_raw(f"rm -f -- {shell_quote(path)}")
                    results[path] = "" if one == 0 else (
                        err1.strip() or out1.strip() or f"rm exit {one}"
                    )
            batch.clear()

        for path, _size in entries:
            batch.append(path)
            if len(batch) >= self.DELETE_BATCH:
                flush()
        flush()
        return results

    def count_leaves(self, path: str) -> Stats:
        """Count the files under ``path`` as the shell sees them.

        ``measure()`` returns bytes only (one ``du`` call). The delete paths
        also report *how many* files went away, so they need a count too.
        """
        return Stats(files=len(self.leaves(path)), bytes=0)

    def clear_app_cache(self, package: str, force_stop: bool = True) -> Stats:
        pkg = _validate_package(package)
        before = 0
        # uid 2000 cannot even traverse /data/data/<pkg> (verified on device:
        # `su 2000 -c du -sk /data/data/com.android.vending/cache` -> EACCES),
        # so the size stays *unknown* rather than silently becoming zero.
        known = False
        try:
            out = self._shell(f"du -sk -- /data/data/{pkg}/cache 2>/dev/null")
            if out.strip():
                before = int(out.split()[0]) * 1024
                known = True
        except (BackendError, ValueError, IndexError):
            before = 0

        if force_stop:
            # Failure here is not fatal: the cache may still be clearable.
            try:
                self._shell(f"am force-stop {pkg}")
            except BackendError:
                pass

        self._shell(f"pm clear --cache-only {pkg}")
        return Stats(files=0, bytes=before, size_known=known)

    def installed_packages(self) -> list[str]:
        out = self._shell("pm list packages")
        packages = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.append(line[len("package:"):])
        return sorted(packages)


class _SqliteMixin:
    """Database operations backed by a callback injected from Kotlin.

    The callback is ``(path) -> (code, stdout, stderr)`` and is expected to run
    the bundled glibc ``sqlite3``. Keeping it injected means the Python core
    stays testable without Android and knows nothing about the loader.
    """

    @property
    def supports_sqlite(self) -> bool:
        """True only when a runner was actually injected.

        A backend class that merely inherits the mixin cannot vacuum anything;
        reporting True there would offer the cleaner in the UI and fail at
        apply time.
        """
        try:
            return self._vacuum_runner() is not None
        except BackendError:
            return False

    def _vacuum_runner(self):
        raise NotImplementedError

    def vacuum_db(self, path: str) -> Stats:
        """Rebuild the database and report how much actually left the disk.

        Sizes are asked from ``self.size()``, not ``os.path.getsize``: on the
        ADB and root tiers this process cannot see the file at all, so a local
        stat would silently report 0.
        """
        try:
            before = self.size(path)
        except (BackendError, OSError):
            before = 0
        code, out, err = self._vacuum_runner()(path)
        if code != 0:
            raise BackendError(f"sqlite3 exit {code}: {err.strip() or out.strip()}")
        try:
            after = self.size(path)
        except (BackendError, OSError):
            after = 0
        return Stats(files=1, bytes=max(before - after, 0))


# ---------------------------------------------------------------------------
# non-root: only our own app data + shared storage the process can reach
# ---------------------------------------------------------------------------

class NonRootBackend(_SqliteMixin, PrivilegeBackend):
    """Our own app data, plus shared storage when the app may actually reach it.

    ``shared_storage`` is not a constant here, and that was a real bug: the
    class used to advertise ``SHARED_STORAGE`` unconditionally while the
    whitelist only ever held the app's own three directories. Enumeration does
    not go through ``_guard()``, so a scan happily found /sdcard/DCIM/.thumbnails
    and every single delete then failed with "outside allowed roots" — the
    default tier could not clean the two cleaners worth hundreds of megabytes.

    The capability now follows the roots the backend was actually given, so the
    UI shows "chybí: sdílené úložiště" (with its existing button to grant
    all-files access) instead of promising a clean that cannot happen.
    """

    name = "nonroot"
    capabilities = frozenset({Capability.APP_DATA})

    def __init__(
        self,
        allowed_roots: Iterable[str],
        vacuum_runner=None,
        shared_storage: bool = False,
    ) -> None:
        # Empty is allowed: introspection works, every guarded access is denied.
        self._roots = [os.path.realpath(r) for r in allowed_roots if r]
        self._vacuum = vacuum_runner
        self.capabilities = frozenset(
            {Capability.APP_DATA}
            | ({Capability.SHARED_STORAGE} if shared_storage else set())
        )

    def _vacuum_runner(self):
        if self._vacuum is None:
            raise BackendError("no sqlite3 runner registered")
        return self._vacuum

    def _guard(self, path: str) -> str:
        real = os.path.realpath(path)
        for root in self._roots:
            try:
                if os.path.commonpath([real, root]) == root:
                    return real
            except ValueError:
                continue
        raise BackendError(f"outside allowed roots: {path}")

    def measure(self, path: str) -> Stats:
        return _tree_stats(self._guard(path))

    def delete(self, path: str) -> Stats:
        return _local_delete(self._guard(path))

    def exists(self, path: str) -> bool:
        try:
            return os.path.lexists(self._guard(path))
        except BackendError:
            return False

    def vacuum_db(self, path: str) -> Stats:
        """Rebuild a database, but only inside the allowed roots.

        ``_SqliteMixin.vacuum_db`` hands the path straight to the injected
        runner, so without this override the whitelist (AGENTS.md §6/4) held
        for ``delete()`` and not for ``kind=sqlite``: any database the process
        could open was rewritable regardless of the configured roots.
        """
        return _SqliteMixin.vacuum_db(self, self._guard(path))


# ---------------------------------------------------------------------------
# Shizuku / ADB: can reach other apps' data via `sh -c` run as shell (uid 2000)
# ---------------------------------------------------------------------------

class AdbBackend(_SqliteMixin, _ShellMixin, PrivilegeBackend):
    """Executes through a ``ShizukuRunner`` supplied by the Kotlin side.

    The runner interface is a single method ``run(command: str) -> (exit_code,
    stdout, stderr)``. Keeping it duck-typed means the Python core stays
    testable without Android.
    """

    name = "adb"
    capabilities = frozenset(
        {Capability.APP_DATA, Capability.SHARED_STORAGE, Capability.OTHER_APP_DATA}
    )

    def __init__(self, runner, app_files_dir: str = "", vacuum_runner=None) -> None:
        self._runner = runner
        self._app_files_dir = os.path.realpath(app_files_dir) if app_files_dir else ""
        self._vacuum = vacuum_runner

    def _vacuum_runner(self):
        if self._vacuum is None:
            raise BackendError("no sqlite3 runner registered")
        return self._vacuum

    def _guarded(self, path: str) -> str:
        if not os.path.isabs(path) or ".." in path.split(os.sep):
            raise BackendError(f"unsafe path: {path}")
        return path

    def _run_raw(self, command: str) -> tuple[int, str, str]:
        return self._runner.run(command)

    def _shell(self, command: str) -> str:
        code, out, err = self._runner.run(command)
        if code != 0:
            raise BackendError(f"sh exit {code}: {err.strip() or out.strip()}")
        return out

    def expand(self, pattern: str) -> list[str]:
        """Guard the whole pattern first, then let the shell glob it.

        The pattern itself may contain wildcards, so only the base directory is
        checked for ``..``; the guard is a smell test, not a sandbox.
        """
        self._guarded(pattern)
        return _ShellMixin.expand(self, pattern)

    def measure(self, path: str) -> Stats:
        out = self._shell(f"du -sb -- {shell_quote(self._guarded(path))}")
        try:
            size = int(out.split()[0])
        except (IndexError, ValueError) as exc:
            raise BackendError(f"cannot parse du output: {out!r}") from exc
        return Stats(files=0, bytes=size)

    def delete(self, path: str) -> Stats:
        target = self._guarded(path)
        before = self.measure(target).bytes
        counted = self.count_leaves(target).files
        self._shell(f"rm -rf -- {shell_quote(target)}")
        return Stats(files=counted, bytes=before)

    def exists(self, path: str) -> bool:
        code, _, _ = self._runner.run(f"test -e {shell_quote(self._guarded(path))}")
        return code == 0


# ---------------------------------------------------------------------------
# root: everything, optionally through `su`
# ---------------------------------------------------------------------------

class RootBackend(_SqliteMixin, _ShellMixin, PrivilegeBackend):
    """Everything, through direct syscalls when already root, or through a
    ``su`` runner supplied by the Kotlin side.

    In Chaquopy ``os.geteuid()`` is never 0, so on a rooted device the Kotlin
    side passes a runner that is *already* root — one persistent ``su`` shell
    that every command is written into. Commands therefore go out unwrapped.

    The SQLite mixin is inherited here as well: without it the root tier
    advertised every database cleaner as "missing: sqlite3", even though the
    bundled sqlite3 runs just as well as it does on the non-root tier.
    """

    name = "root"
    capabilities = frozenset(Capability)

    def __init__(self, runner=None, vacuum_runner=None) -> None:
        self._runner = runner
        self._vacuum = vacuum_runner
        if runner is None and os.geteuid() != 0:
            raise BackendError("root backend requires euid 0 or a su runner")

    def _vacuum_runner(self):
        if self._vacuum is None:
            raise BackendError("no sqlite3 runner registered")
        return self._vacuum

    # -- path ops -------------------------------------------------------
    def measure(self, path: str) -> Stats:
        if self._runner is None:
            return _tree_stats(path)
        out = self._shell(f"du -sb -- {shell_quote(path)}")
        try:
            return Stats(files=0, bytes=int(out.split()[0]))
        except (IndexError, ValueError) as exc:
            raise BackendError(f"cannot parse du output: {out!r}") from exc

    def delete(self, path: str) -> Stats:
        if self._runner is None:
            return _local_delete(path)
        before = self.measure(path).bytes
        counted = self.count_leaves(path).files
        self._shell(f"rm -rf -- {shell_quote(path)}")
        return Stats(files=counted, bytes=before)

    def exists(self, path: str) -> bool:
        if self._runner is None:
            return os.path.lexists(path)
        code, _, _ = self._run_raw(f"test -e {shell_quote(path)}")
        return code == 0

    def delete_many(self, entries: list[tuple[str, int]]) -> dict[str, str]:
        """Batch through the shell, or fall back to syscalls when already root."""
        if self._runner is None:
            return PrivilegeBackend.delete_many(self, entries)
        return _ShellMixin.delete_many(self, entries)

    # -- package ops ----------------------------------------------------
    def package_uid(self, package: str) -> int | None:
        """Owner uid of ``/data/data/<package>``, or None when it is gone.

        Read from the directory itself rather than parsed out of
        ``dumpsys package``: one stat, no fragile text parsing, and it also
        tells us the directory still exists.
        """
        code, out, _ = self._run_raw(
            f"stat -c %u -- {shell_quote('/data/data/' + _validate_package(package))}"
        )
        if code != 0:
            return None
        try:
            return int(out.strip().splitlines()[0])
        except (IndexError, ValueError):
            return None

    def clear_app_cache(self, package: str, force_stop: bool = True) -> Stats:
        """Clear one app's cache **as that app's own uid**.

        Why not just ``rm -rf`` as root: a file created by root inside an app's
        data directory stays owned by root, the app can no longer write it, and
        the next launch misbehaves (this is the failure mode that caused a
        bootloop elsewhere in this project). Deleting as the owning uid means
        every remaining file keeps a consistent owner, and nothing is chowned.

        ``pm clear --cache-only`` is deliberately not used on this tier: it
        also wipes package-manager bookkeeping, and we can do the narrower,
        safer thing here.
        """
        pkg = _validate_package(package)
        uid = self.package_uid(pkg)
        if uid is None:
            raise BackendError(f"no data directory for {pkg}")

        cache = f"/data/data/{pkg}/cache"
        before = 0
        try:
            out = self._shell(f"du -sk -- {shell_quote(cache)} 2>/dev/null || true")
            before = int(out.split()[0]) * 1024 if out.strip() else 0
        except (BackendError, ValueError, IndexError):
            before = 0

        if force_stop:
            # Failing here is not fatal: an open cache file may still delete.
            try:
                self._shell(f"am force-stop {pkg}")
            except BackendError:
                pass

        # -mindepth 1 keeps the cache directory itself; -delete removes files
        # and empty directories bottom-up. Running through `su <uid>` is the
        # whole point: the filesystem sees the app's own uid, not root's.
        code, out, err = self._runner.run(
            f"su {uid} -c {shell_quote(f'find {cache} -mindepth 1 -delete 2>&1')}"
        )
        if code != 0:
            raise BackendError(
                f"cannot clear cache of {pkg} as uid {uid}: {err.strip() or out.strip()}"
            )
        return Stats(files=0, bytes=before)

    # The runner is the thing that is root, not the command.
    #
    # This used to wrap every single command in `su -c '...'`, but the Kotlin
    # runner keeps one persistent `su` shell alive (ShellSession) precisely so
    # that a scan does not spawn a process per command — and the wrapper put
    # that process back, one `su` exec for every `test -e`. Whoever supplies
    # the runner is responsible for it being root; `detect()` verifies it with
    # `id`, and the one place that deliberately drops privileges
    # (`clear_app_cache`) still spells out `su <uid> -c` itself.
    def _run_raw(self, command: str) -> tuple[int, str, str]:
        if self._runner is None:
            raise BackendError("root backend without su runner cannot run shell commands")
        return self._runner.run(command)

    def _shell(self, command: str) -> str:
        if self._runner is None:
            raise BackendError("root backend without su runner cannot run shell commands")
        code, out, err = self._runner.run(command)
        if code != 0:
            raise BackendError(f"su exit {code}: {err.strip() or out.strip()}")
        return out


__all__ = [
    "AdbBackend",
    "BackendError",
    "NonRootBackend",
    "PrivilegeBackend",
    "RootBackend",
    "Stats",
    "shell_quote",
]
