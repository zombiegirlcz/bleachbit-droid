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

import json

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PY_ROOT = HERE.parents[2] / "main" / "python"
sys.path.insert(0, str(PY_ROOT))

from bleachbit_droid import engine  # noqa: E402
from bleachbit_droid import api  # noqa: E402
from bleachbit_droid.backends import (  # noqa: E402
    AdbBackend,
    BackendError,
    NonRootBackend,
    RootBackend,
    _find_base_and_glob,
    shell_quote,
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

        def list_sized(self, pattern):
            calls.append("list_sized")
            return real.list_sized(pattern)

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


# ---------------------------------------------------------------------------
# SQLite support must be advertised by every tier that can actually vacuum
# ---------------------------------------------------------------------------


def test_root_backend_reports_sqlite_when_runner_injected():
    """Regression: RootBackend did not inherit _SqliteMixin.

    The root tier therefore listed every database cleaner as
    "missing: sqlite3" although the bundled sqlite3 works there too.
    """
    backend = RootBackend(None if __import__("os").geteuid() == 0 else FakeShell(),
                          vacuum_runner=lambda path: (0, "", ""))
    assert backend.supports_sqlite is True


def test_root_backend_without_vacuum_runner_cannot_vacuum():
    backend = RootBackend(None if __import__("os").geteuid() == 0 else FakeShell())
    assert backend.supports_sqlite is False
    with pytest.raises(BackendError):
        backend.vacuum_db("/tmp/nope.db")


def test_api_passes_vacuum_runner_to_root_backend():
    seen = []

    class Runner:
        def run(self, command):
            return 0, "uid=0(root) gid=0(root)", ""

    class Vac:
        def run(self, path):
            seen.append(path)
            return json.dumps({"code": 0, "stdout": "", "stderr": ""})

    api.set_root_runner(Runner())
    api.set_vacuum_runner(Vac())
    try:
        payload = json.dumps({"mode": "root", "cleaners": ["own_databases"]})
        out = json.loads(api.list_cleaners(payload))
        assert "error" not in out, out
        assert out["backend"]["supports_sqlite"] is True
        assert seen == [], "listing cleaners must not vacuum anything"
    finally:
        api._ROOT_RUNNER = None
        api._VACUUM_RUNNER = None


def test_list_sized_uses_a_real_tab_in_the_stat_format():
    """Regression: the format string had a literal backslash-t.

    ``stat -c '%s\t%n'`` inside a Python string that ends up in a shell
    command must contain an actual TAB. With ``'%s\\t%n'`` (two characters,
    backslash and t) the output was ``4096\\t/path`` and the ``split("\t")``
    below never matched, so list_sized() silently returned nothing and every
    scan reported 0 B.
    """
    shell = FakeShell(
        {
            "find": (0, "4096\t/sdcard/c/a.bin\n2048\t/sdcard/c/sub/b.bin\n", ""),
            "test -d": (0, "", ""),
        }
    )
    backend = AdbBackend(shell, "/data/user/0/x/files")

    sized = backend.list_sized("/sdcard/c/**")

    assert sized == [("/sdcard/c/a.bin", 4096), ("/sdcard/c/sub/b.bin", 2048)]
    joined = " | ".join(shell.calls)
    # The command must carry a real TAB, not the two characters '\' and 't'.
    assert "\t" in joined
    assert "\\t" not in joined


def test_list_sized_is_one_shell_call_for_the_whole_target():
    """A target with N files must cost one command, not N."""
    lines = "".join(f"10\t/sdcard/c/f{i}.bin\n" for i in range(500))
    shell = FakeShell({"find": (0, lines, ""), "test -d": (0, "", "")})
    backend = AdbBackend(shell, "/data/user/0/x/files")

    sized = backend.list_sized("/sdcard/c/**")

    assert len(sized) == 500
    assert len(shell.calls) == 1


def test_list_sized_skips_malformed_lines():
    shell = FakeShell(
        {
            "find": (0, "10\t/good\nnotanumber\t/bad\n\n20\t/also-good\n", ""),
            "test -d": (0, "", ""),
        }
    )
    backend = AdbBackend(shell, "/data/user/0/x/files")
    assert backend.list_sized("/x/**") == [("/good", 10), ("/also-good", 20)]


# ---------------------------------------------------------------------------
# deletion is bulk too, not just enumeration
# ---------------------------------------------------------------------------

def test_shell_delete_is_one_command_per_batch():
    """Regression: apply cost four shell commands per file.

    ``delete()`` measured (``du``), probed (``test -f``, ``test -d``) and only
    then removed, for every single file — even though the scan had already
    measured all of them in two commands. On /data/dalvik-cache (851 MB,
    ~2000 files) that is thousands of round trips through `su`.
    """
    shell = FakeShell()
    backend = RootBackend(shell)
    entries = [(f"/data/dalvik-cache/arm64/f{i}.oat", 100) for i in range(20)]

    results = backend.delete_many(entries)

    assert all(reason == "" for reason in results.values()), results
    assert len(shell.calls) == 1, shell.calls
    assert shell.calls[0].startswith("rm -f --")
    for path, _size in entries:
        assert shell_quote(path) in shell.calls[0]


def test_shell_delete_splits_into_batches():
    shell = FakeShell()
    backend = RootBackend(shell)
    count = backend.DELETE_BATCH * 2 + 5
    entries = [(f"/data/local/tmp/f{i}", 1) for i in range(count)]

    backend.delete_many(entries)

    assert len(shell.calls) == 3, len(shell.calls)


def test_shell_delete_pinpoints_the_file_that_failed():
    """`rm` fails for the whole batch, the log has to name the real culprit."""
    locked = "/data/local/tmp/locked"
    shell = FakeShell({
        f"rm -f -- {shell_quote(locked)}": (1, "", "Permission denied"),
        f"{shell_quote(locked)}": (1, "", "Permission denied"),
    })
    backend = RootBackend(shell)
    entries = [("/data/local/tmp/a", 1), (locked, 1), ("/data/local/tmp/b", 1)]

    results = backend.delete_many(entries)

    assert results["/data/local/tmp/a"] == ""
    assert results["/data/local/tmp/b"] == ""
    assert "Permission denied" in results[locked]


def test_apply_deletes_through_the_batch_api(tmp_path: Path):
    """The engine must route plain files through delete_many, in scan order."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(5):
        (cache / f"f{i}.bin").write_bytes(b"x" * 10)

    roots = {
        "app_files": str(tmp_path),
        "app_cache": str(cache),
        "external": str(tmp_path),
        "sdcard": str(tmp_path),
        "android_data": str(tmp_path),
        "android_media": str(tmp_path),
    }

    batched: list[list[str]] = []

    class CountingBackend(NonRootBackend):
        def delete_many(self, entries):
            batched.append([p for p, _ in entries])
            return NonRootBackend.delete_many(self, entries)

    backend = CountingBackend([str(tmp_path)])
    report = engine.apply(backend, roots, ["app_cache"], dry_run=False)

    assert report.deleted == 5
    assert report.freed_bytes == 50
    assert len(batched) == 1, batched
    assert not list(cache.iterdir())
