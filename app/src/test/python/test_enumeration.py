"""Tests for shell-side path enumeration (ADB and root tiers).

Regression: ``engine.scan`` used to call ``glob.glob`` / ``os.walk`` itself.
Those run in *this* process, and the ADB/root tiers cannot see other apps'
data from here, so a scan through Shizuku or ``su`` silently reported 0 B and
cleaned nothing. Enumeration now goes through the backend, which answers in the
namespace it can actually reach (local syscalls for non-root, ``find`` over the
shell for the others).

Run from the ``app`` directory:

    python3 -m pytest src/test/python -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PY_ROOT = HERE.parents[2] / "main" / "python"
sys.path.insert(0, str(PY_ROOT))

from bleachbit_droid import engine  # noqa: E402
from bleachbit_droid.backends import (  # noqa: E402
    AdbBackend,
    BackendError,
    NonRootBackend,
    _find_base_and_glob,
)


class FakeShell:
    """Records commands and answers like a shell would.

    ``replies`` maps a *substring* of the command to ``(exit_code, stdout,
    stderr)``; the first matching key in insertion order wins, anything else
    succeeds with no output. Substring matching (not prefix) is needed because
    the backend composes commands like
    ``test -d '<base>' && find '<base>' -path '<glob>'`` — a prefix match on
    ``test -d`` would swallow the whole thing and hide the ``find`` output.
    Order the keys so the most specific probe comes first.
    """

    def __init__(self, replies: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.calls: list[str] = []
        self.replies = replies or {}

    def run(self, command: str) -> tuple[int, str, str]:
        self.calls.append(command)
        for needle, reply in self.replies.items():
            if needle in command:
                return reply
        return 0, "", ""


# ---------------------------------------------------------------------------
# pattern splitting
# ---------------------------------------------------------------------------


def test_find_base_and_glob_splits_recursive_pattern():
    assert _find_base_and_glob("/data/data/*/cache/**") == (
        "/data/data",
        "/data/data/*/cache/*",
    )
    assert _find_base_and_glob("/sdcard/DCIM/.thumbnails/**") == (
        "/sdcard/DCIM/.thumbnails",
        "/sdcard/DCIM/.thumbnails/*",
    )


def test_find_base_and_glob_keeps_plain_path():
    base, pattern = _find_base_and_glob("/data/anr/x.log")
    assert base == "/data/anr"
    assert pattern == "/data/anr/x.log"


def test_find_base_and_glob_folds_double_star():
    """toybox find has no globstar; `*` already crosses `/`."""
    _, pattern = _find_base_and_glob("/a/b/**/*.db")
    assert "**" not in pattern


# ---------------------------------------------------------------------------
# ADB / shell backend
# ---------------------------------------------------------------------------


def test_adb_expand_asks_the_shell():
    shell = FakeShell(
        {
            "find": (0, "/data/data/com.a/cache\n/data/data/com.b/cache\n", ""),
            "test -d": (0, "", ""),
        }
    )
    backend = AdbBackend(shell, "/data/user/0/x/files")

    found = backend.expand("/data/data/*/cache")

    assert found == ["/data/data/com.a/cache", "/data/data/com.b/cache"]
    joined = " | ".join(shell.calls)
    assert "find '/data/data'" in joined or "find /data/data" in joined
    assert "-path" in joined


def test_adb_expand_missing_base_returns_empty():
    shell = FakeShell({"test -d": (1, "", "")})
    backend = AdbBackend(shell, "/data/user/0/x/files")
    assert backend.expand("/nonexistent/*/cache/**") == []


def test_adb_expand_rejects_relative_and_dotdot():
    backend = AdbBackend(FakeShell(), "/data/user/0/x/files")
    for bad in ("relative/path", "/data/../etc/passwd"):
        with pytest.raises(BackendError):
            backend.expand(bad)


def test_adb_leaves_never_returns_the_root():
    shell = FakeShell(
        {
            "find": (0, "/sdcard/c/a.bin\n/sdcard/c/sub/b.bin\n", ""),
            "test -f": (1, "", ""),
            "test -d": (0, "", ""),
        }
    )
    backend = AdbBackend(shell, "/data/user/0/x/files")

    leaves = backend.leaves("/sdcard/c")

    assert leaves == ["/sdcard/c/a.bin", "/sdcard/c/sub/b.bin"]
    assert "/sdcard/c" not in leaves
    assert any("-mindepth 1" in cmd for cmd in shell.calls)


def test_adb_size_parses_stat_output():
    shell = FakeShell({"stat -c %s": (0, "12345\n", "")})
    backend = AdbBackend(shell, "/data/user/0/x/files")
    assert backend.size("/sdcard/c/a.bin") == 12345


def test_adb_size_raises_when_stat_fails():
    shell = FakeShell({"stat -c %s": (1, "", "No such file")})
    backend = AdbBackend(shell, "/data/user/0/x/files")
    with pytest.raises(BackendError):
        backend.size("/sdcard/missing")


def test_adb_is_dir_and_is_file_use_test():
    shell = FakeShell({"test -d": (0, "", ""), "test -f": (1, "", "")})
    backend = AdbBackend(shell, "/data/user/0/x/files")
    assert backend.is_dir("/sdcard/c") is True
    assert backend.is_file("/sdcard/c") is False


# ---------------------------------------------------------------------------
# non-root backend stays local
# ---------------------------------------------------------------------------


def test_nonroot_expand_is_local():
    backend = NonRootBackend(["/tmp"])
    assert backend.expand("/tmp/definitely-not-here-*") == []


def test_nonroot_leaves_lists_files():
    backend = NonRootBackend(["/tmp"])
    assert backend.leaves("/tmp")  # non-empty on any real system


# ---------------------------------------------------------------------------
# the engine must go through the backend
# ---------------------------------------------------------------------------


def test_engine_scan_enumerates_through_the_backend(tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(b"x" * 100)

    calls: list[str] = []
    real = NonRootBackend([str(tmp_path)])

    class Spy:
        """Wraps a real backend and records which enumeration calls happen."""

        name = "nonroot"
        capabilities = real.capabilities
        supports_packages = False
        supports_sqlite = False

        def can(self, cap):
            return real.can(cap)

        def expand(self, pattern):
            calls.append("expand")
            return real.expand(pattern)

        def leaves(self, path):
            calls.append("leaves")
            return real.leaves(path)

        def exists(self, path):
            return real.exists(path)

        def is_dir(self, path):
            return real.is_dir(path)

        def is_file(self, path):
            return real.is_file(path)

        def size(self, path):
            return real.size(path)

        def measure(self, path):
            return real.measure(path)

        def delete(self, path):  # pragma: no cover - scan must never delete
            raise AssertionError("scan must not delete")

    roots = {
        "app_files": str(tmp_path),
        "app_cache": str(cache),
        "external": str(tmp_path),
        "sdcard": str(tmp_path),
        "android_data": str(tmp_path),
        "android_media": str(tmp_path),
    }

    report = engine.scan(Spy(), roots, ["app_cache"])

    assert calls, "engine did not enumerate through the backend"
    assert report.total_bytes == 100
