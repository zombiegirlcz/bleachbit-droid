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

    # --- introspection -------------------------------------------------
    def can(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "supports_packages": self.supports_packages,
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

    # --- packages ------------------------------------------------------
    def clear_app_cache(self, package: str, force_stop: bool = True) -> Stats:
        """Clear one app's cache through Android, not by touching files."""
        raise BackendError(f"backend {self.name!r} cannot clear app caches")

    def installed_packages(self) -> list[str]:
        """Package names visible to this backend."""
        raise BackendError(f"backend {self.name!r} cannot list packages")


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
    """Package operations shared by the shell-based backends."""

    supports_packages = True

    def _shell(self, command: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _shell_ok(self, command: str) -> str:
        return self._shell(command)

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


# ---------------------------------------------------------------------------
# non-root: only our own app data + shared storage the process can reach
# ---------------------------------------------------------------------------

class NonRootBackend(PrivilegeBackend):
    name = "nonroot"
    capabilities = frozenset({Capability.APP_DATA, Capability.SHARED_STORAGE})

    def __init__(self, allowed_roots: Iterable[str]) -> None:
        # Empty is allowed: introspection works, every guarded access is denied.
        self._roots = [os.path.realpath(r) for r in allowed_roots if r]

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

class AdbBackend(_ShellMixin, PrivilegeBackend):
    """Executes through a ``ShizukuRunner`` supplied by the Kotlin side.

    The runner interface is a single method ``run(command: str) -> (exit_code,
    stdout, stderr)``. Keeping it duck-typed means the Python core stays
    testable without Android.
    """

    name = "adb"
    capabilities = frozenset(
        {Capability.APP_DATA, Capability.SHARED_STORAGE, Capability.OTHER_APP_DATA}
    )

    def __init__(self, runner, app_files_dir: str = "") -> None:
        self._runner = runner
        self._app_files_dir = os.path.realpath(app_files_dir) if app_files_dir else ""

    def _guarded(self, path: str) -> str:
        if not os.path.isabs(path) or ".." in path.split(os.sep):
            raise BackendError(f"unsafe path: {path}")
        return path

    def _shell(self, command: str) -> str:
        code, out, err = self._runner.run(command)
        if code != 0:
            raise BackendError(f"sh exit {code}: {err.strip() or out.strip()}")
        return out

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
        self._shell(f"rm -rf -- {shell_quote(target)}")
        return Stats(files=0, bytes=before)

    def exists(self, path: str) -> bool:
        code, _, _ = self._runner.run(f"test -e {shell_quote(self._guarded(path))}")
        return code == 0


# ---------------------------------------------------------------------------
# root: everything, optionally through `su`
# ---------------------------------------------------------------------------

class RootBackend(_ShellMixin, PrivilegeBackend):
    """Everything, through direct syscalls when already root, or through a
    ``su`` runner supplied by the Kotlin side.

    In Chaquopy ``os.geteuid()`` is never 0, so on a rooted device the Kotlin
    side passes a runner that prefixes commands with ``su -c``.
    """

    name = "root"
    capabilities = frozenset(Capability)

    def __init__(self, runner=None) -> None:
        self._runner = runner
        if runner is None and os.geteuid() != 0:
            raise BackendError("root backend requires euid 0 or a su runner")

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
        self._shell(f"rm -rf -- {shell_quote(path)}")
        return Stats(files=0, bytes=before)

    def exists(self, path: str) -> bool:
        if self._runner is None:
            return os.path.lexists(path)
        code, _, _ = self._runner.run(f"su -c {shell_quote('test -e ' + shell_quote(path))}")
        return code == 0

    # -- package ops ----------------------------------------------------
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
