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


class PrivilegeBackend(ABC):
    """Common interface for all three privilege levels."""

    name: str = "abstract"
    capabilities: frozenset[Capability] = frozenset()
    #: package operations need a shell; path-only backends leave this False
    supports_packages: bool = False
    #: database operations need the bundled glibc sqlite3; overridden as a
    #: property by _SqliteMixin
    supports_sqlite: bool = False

    # --- introspection -------------------------------------------------
    def can(self, cap: Capability) -> bool:
        return cap in self.capabilities

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
                seen.add(leaf)
                try:
                    out.append((leaf, self.size(leaf)))
                except (BackendError, OSError):
                    continue
        return out

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
            f"-exec stat -c '%s\\t%n' {{}} +"
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

    def count_leaves(self, path: str) -> Stats:
        """Count the files under ``path`` as the shell sees them.

        ``measure()`` returns bytes only (one ``du`` call). The delete paths
        also report *how many* files went away, so they need a count too.
        """
        return Stats(files=len(self.leaves(path)), bytes=0)

    def clear_app_cache(self, package: str, force_stop: bool = True) -> Stats:
        pkg = _validate_package(package)
        before = 0
        try:
            out = self._shell(f"du -sk -- /data/data/{pkg}/cache 2>/dev/null")
            before = int(out.split()[0]) * 1024 if out.strip() else 0
        except (BackendError, ValueError, IndexError):
            before = 0

        if force_stop:
            # Failure here is not fatal: the cache may still be clearable.
            try:
                self._shell(f"am force-stop {pkg}")
            except BackendError:
                pass

        self._shell(f"pm clear --cache-only {pkg}")
        return Stats(files=0, bytes=before)

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
    name = "nonroot"
    capabilities = frozenset({Capability.APP_DATA, Capability.SHARED_STORAGE})

    def __init__(self, allowed_roots: Iterable[str], vacuum_runner=None) -> None:
        # Empty is allowed: introspection works, every guarded access is denied.
        self._roots = [os.path.realpath(r) for r in allowed_roots if r]
        self._vacuum = vacuum_runner

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
    side passes a runner that prefixes commands with ``su -c``.

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
        code, _, _ = self._runner.run(f"su -c {shell_quote('test -e ' + shell_quote(path))}")
        return code == 0

    # -- package ops ----------------------------------------------------
    def _run_raw(self, command: str) -> tuple[int, str, str]:
        if self._runner is None:
            raise BackendError("root backend without su runner cannot run shell commands")
        return self._runner.run(f"su -c {shell_quote(command)}")

    def _shell(self, command: str) -> str:
        if self._runner is None:
            raise BackendError("root backend without su runner cannot run shell commands")
        code, out, err = self._runner.run(f"su -c {shell_quote(command)}")
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
