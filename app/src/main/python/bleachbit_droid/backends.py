"""PrivilegeBackend implementations.

Rule of the project (AGENTS.md §10): every filesystem mutation goes through a
backend. No cleaner and no engine may call ``os.remove``/``shutil.rmtree``
directly.
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

    # --- introspection -------------------------------------------------
    def can(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "capabilities": sorted(c.value for c in self.capabilities),
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
                # different drives / mixed absolute-relative
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

class AdbBackend(PrivilegeBackend):
    """Executes through a ``ShizukuRunner`` supplied by the Kotlin side.

    The runner interface is a single method ``run(command: str) -> (exit_code,
    stdout, stderr)``. Keeping it duck-typed means the Python core stays
    testable without Android.
    """

    name = "adb"
    capabilities = frozenset(
        {Capability.APP_DATA, Capability.SHARED_STORAGE, Capability.OTHER_APP_DATA}
    )

    def __init__(self, runner, app_files_dir: str) -> None:
        self._runner = runner
        self._app_files_dir = os.path.realpath(app_files_dir)

    def _guarded(self, path: str) -> str:
        if not os.path.isabs(path) or ".." in path.split(os.sep):
            raise BackendError(f"unsafe path: {path}")
        return path

    def _run(self, command: str) -> str:
        code, out, err = self._runner.run(command)
        if code != 0:
            raise BackendError(f"sh exit {code}: {err.strip() or out.strip()}")
        return out

    def measure(self, path: str) -> Stats:
        out = self._run(f"du -sb -- {shell_quote(self._guarded(path))}")
        try:
            size = int(out.split()[0])
        except (IndexError, ValueError) as exc:
            raise BackendError(f"cannot parse du output: {out!r}") from exc
        return Stats(files=0, bytes=size)

    def delete(self, path: str) -> Stats:
        target = self._guarded(path)
        before = self.measure(target).bytes
        self._run(f"rm -rf -- {shell_quote(target)}")
        return Stats(files=0, bytes=before)

    def exists(self, path: str) -> bool:
        code, _, _ = self._runner.run(f"test -e {shell_quote(self._guarded(path))}")
        return code == 0


# ---------------------------------------------------------------------------
# root: everything
# ---------------------------------------------------------------------------

class RootBackend(PrivilegeBackend):
    name = "root"
    capabilities = frozenset(Capability)

    def __init__(self) -> None:
        if os.geteuid() != 0:
            raise BackendError("root backend requires euid 0")

    def measure(self, path: str) -> Stats:
        return _tree_stats(path)

    def delete(self, path: str) -> Stats:
        return _local_delete(path)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)


# ---------------------------------------------------------------------------
# utils
# ---------------------------------------------------------------------------

def shell_quote(value: str) -> str:
    """POSIX single-quote escaping."""
    return "'" + value.replace("'", "'\\''") + "'"


__all__ = [
    "AdbBackend",
    "BackendError",
    "NonRootBackend",
    "PrivilegeBackend",
    "RootBackend",
    "Stats",
    "shell_quote",
]