"""Regression tests for the Python core.

Run from the ``app`` directory:

    python3 -m pytest src/test/python -q

All deletion tests use throwaway fixture trees, never real system data
(AGENTS.md §7). Package operations use fake shell runners.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PY_ROOT = HERE.parents[2] / "main" / "python"
sys.path.insert(0, str(PY_ROOT))

from bleachbit_droid import api, engine  # noqa: E402
from bleachbit_droid.backends import (  # noqa: E402
    AdbBackend,
    BackendError,
    NonRootBackend,
    RootBackend,
    _validate_package,
    shell_quote,
)
from bleachbit_droid.capabilities import missing  # noqa: E402
from bleachbit_droid.cleaners import all_cleaners, by_name  # noqa: E402
from bleachbit_droid.models import Capability, Kind, Risk  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """Fake app sandbox with a cache dir containing files and a nested dir."""
    cache = tmp_path / "cache"
    (cache / "sub").mkdir(parents=True)
    (cache / "a.bin").write_bytes(b"x" * 100)
    (cache / "sub" / "b.bin").write_bytes(b"y" * 250)
    (tmp_path / "keep.txt").write_bytes(b"z" * 10)
    return tmp_path


@pytest.fixture()
def roots(tree: Path) -> dict[str, str]:
    return {
        "app_files": str(tree),
        "app_cache": str(tree / "cache"),
        "external": str(tree),
        "sdcard": str(tree),
        "android_data": str(tree),
        "android_media": str(tree),
    }


class FakeShell:
    """Records commands and answers like a shell would."""

    def __init__(self, replies: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.calls: list[str] = []
        self.replies = replies or {}

    def run(self, command: str) -> tuple[int, str, str]:
        self.calls.append(command)
        for prefix, reply in self.replies.items():
            if command.startswith(prefix):
                return reply
        return 0, "", ""


# ---------------------------------------------------------------------------
# cleaner metadata (AGENTS.md §8)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", all_cleaners(), ids=lambda s: s.name)
def test_cleaner_metadata_complete(spec):
    assert spec.name and spec.description
    assert spec.requires, "cleaner must declare requires"
    assert isinstance(spec.risk, Risk)
    if spec.kind is Kind.PATH:
        assert spec.paths, "path cleaner must declare paths"
        for target in spec.paths:
            assert target.pattern.startswith("/") or target.pattern.startswith("<")
    else:
        assert not spec.paths, "package cleaner should not declare paths"


def test_cleaner_names_unique():
    names = [s.name for s in all_cleaners()]
    assert len(names) == len(set(names))


def test_by_name_unknown_raises():
    with pytest.raises(KeyError):
        by_name("nope")


def test_cleaner_requires_nonempty():
    from bleachbit_droid.models import CleanerSpec, Target

    with pytest.raises(ValueError):
        CleanerSpec(
            name="bad",
            description="x",
            requires=(),
            paths=(Target("/tmp/**", Capability.APP_DATA),),
            risk=Risk.LOW,
        )


def test_path_cleaner_needs_paths():
    from bleachbit_droid.models import CleanerSpec

    with pytest.raises(ValueError):
        CleanerSpec(
            name="bad",
            description="x",
            requires=(Capability.APP_DATA,),
            risk=Risk.LOW,
        )


# ---------------------------------------------------------------------------
# capability gating
# ---------------------------------------------------------------------------

def test_missing_capabilities():
    have = frozenset({Capability.APP_DATA})
    assert missing((Capability.APP_DATA,), have) == []
    assert missing((Capability.APP_DATA, Capability.SYSTEM), have) == [Capability.SYSTEM]


def test_nonroot_cannot_run_privileged_cleaners(roots):
    backend = NonRootBackend([roots["app_files"]])
    runnable = {s.name for s in engine.select_cleaners(backend)}
    assert "app_cache" in runnable
    assert "logcat_dumps" not in runnable
    assert "apk_leftovers" not in runnable
    # package cleaners need a shell
    assert "other_app_cache" not in runnable
    assert "unused_app_cache" not in runnable


def test_describe_marks_missing(roots):
    backend = NonRootBackend([roots["app_files"]])
    items = {i["name"]: i for i in engine.describe(backend)}
    assert items["app_cache"]["enabled"] is True
    assert items["logcat_dumps"]["enabled"] is False
    assert "system" in items["logcat_dumps"]["missing"]
    assert items["other_app_cache"]["enabled"] is False
    assert "shell" in items["other_app_cache"]["missing"]


def test_adb_can_run_package_cleaners(roots):
    backend = AdbBackend(FakeShell(), roots["app_files"])
    runnable = {s.name for s in engine.select_cleaners(backend)}
    assert "other_app_cache" in runnable
    assert "unused_app_cache" in runnable
    assert "logcat_dumps" not in runnable


# ---------------------------------------------------------------------------
# backend guards
# ---------------------------------------------------------------------------

def test_nonroot_guard_denies_outside_roots(tree: Path):
    backend = NonRootBackend([str(tree / "cache")])
    with pytest.raises(BackendError):
        backend.delete(str(tree / "keep.txt"))
    assert (tree / "keep.txt").exists()


def test_nonroot_guard_allows_inside(tree: Path):
    backend = NonRootBackend([str(tree)])
    stats = backend.delete(str(tree / "cache" / "a.bin"))
    assert stats.bytes == 100
    assert not (tree / "cache" / "a.bin").exists()


def test_nonroot_empty_roots_denies_everything(tree: Path):
    backend = NonRootBackend([])
    with pytest.raises(BackendError):
        backend.measure(str(tree))


def test_nonroot_cannot_clear_packages(tree: Path):
    backend = NonRootBackend([str(tree)])
    with pytest.raises(BackendError):
        backend.clear_app_cache("com.example")
    with pytest.raises(BackendError):
        backend.installed_packages()


def test_shell_quote():
    assert shell_quote("a b") == "'a b'"
    assert shell_quote("it's") == "'it'\\''s'"


@pytest.mark.parametrize(
    "bad",
    ["", "with space", "-rm", "a/b", "a;rm -rf /", "$(id)", "x`id`"],
)
def test_validate_package_rejects_injection(bad):
    with pytest.raises(BackendError):
        _validate_package(bad)


def test_validate_package_accepts_normal_names():
    assert _validate_package("com.example.app") == "com.example.app"
    assert _validate_package("a_b.c") == "a_b.c"


def test_adb_backend_rejects_relative_and_traversal(tmp_path: Path):
    backend = AdbBackend(FakeShell(), str(tmp_path))
    with pytest.raises(BackendError):
        backend._guarded("relative/path")
    with pytest.raises(BackendError):
        backend._guarded("/data/../etc")
    assert backend._guarded("/data/local/tmp/x") == "/data/local/tmp/x"


def test_adb_backend_uses_runner():
    shell = FakeShell({"du": (0, "4096\t/x\n", "")})
    backend = AdbBackend(shell, "/app")
    stats = backend.delete("/data/local/tmp/x")
    assert stats.bytes == 4096
    assert any(c.startswith("rm -rf") for c in shell.calls)


# ---------------------------------------------------------------------------
# package operations (ADB / root)
# ---------------------------------------------------------------------------

def test_adb_clear_app_cache_runs_pm_and_force_stop():
    shell = FakeShell({"du": (0, "16\t/x\n", "")})
    backend = AdbBackend(shell, "/app")

    stats = backend.clear_app_cache("com.example", force_stop=True)

    assert stats.bytes == 16 * 1024
    assert any(c == "am force-stop com.example" for c in shell.calls)
    assert any(c == "pm clear --cache-only com.example" for c in shell.calls)


def test_adb_clear_app_cache_can_skip_force_stop():
    shell = FakeShell()
    backend = AdbBackend(shell, "/app")
    backend.clear_app_cache("com.example", force_stop=False)
    assert not any(c.startswith("am force-stop") for c in shell.calls)


def test_adb_clear_app_cache_survives_force_stop_failure():
    shell = FakeShell({"am force-stop": (1, "", "nope")})
    backend = AdbBackend(shell, "/app")
    backend.clear_app_cache("com.example", force_stop=True)
    assert any(c.startswith("pm clear") for c in shell.calls)


def test_adb_clear_app_cache_propagates_pm_failure():
    shell = FakeShell({"pm clear": (1, "", "Error: no such package")})
    backend = AdbBackend(shell, "/app")
    with pytest.raises(BackendError):
        backend.clear_app_cache("com.example")


def test_adb_installed_packages_parses_pm_output():
    shell = FakeShell({"pm list": (0, "package:com.a\npackage:com.b\n", "")})
    backend = AdbBackend(shell, "/app")
    assert backend.installed_packages() == ["com.a", "com.b"]


def test_root_backend_uses_su_runner():
    shell = FakeShell({"su -c 'du": (0, "2048\t/x\n", "")})
    backend = RootBackend(shell)

    stats = backend.delete("/data/system/dropbox/x")

    assert stats.bytes == 2048
    assert all(c.startswith("su -c") for c in shell.calls)


def test_root_backend_clear_app_cache_goes_through_su():
    shell = FakeShell()
    backend = RootBackend(shell)
    backend.clear_app_cache("com.example", force_stop=True)
    assert any("pm clear --cache-only com.example" in c for c in shell.calls)
    assert all(c.startswith("su -c") for c in shell.calls)


def test_root_backend_requires_runner_when_not_root():
    if __import__("os").geteuid() == 0:
        pytest.skip("running as root")
    with pytest.raises(BackendError):
        RootBackend()


# ---------------------------------------------------------------------------
# scan / apply
# ---------------------------------------------------------------------------

def test_scan_finds_cache_files(roots, tree: Path):
    backend = NonRootBackend([str(tree)])
    report = engine.scan(backend, roots, ["app_cache"])
    paths = {e.path for e in report.entries}
    assert str(tree / "cache" / "a.bin") in paths
    assert str(tree / "cache" / "sub" / "b.bin") in paths
    assert report.total_bytes == 350


def test_scan_dry_run_does_not_delete(roots, tree: Path):
    backend = NonRootBackend([str(tree)])
    report = engine.apply(backend, roots, ["app_cache"], dry_run=True)
    assert report.dry_run is True
    assert report.freed_bytes == 350
    assert report.deleted == 0
    assert (tree / "cache" / "a.bin").exists()
    assert all(r.result.value == "dry_run" for r in report.log)


def test_apply_deletes_only_selected(roots, tree: Path):
    backend = NonRootBackend([str(tree)])
    report = engine.apply(backend, roots, ["app_cache"], dry_run=False)
    assert report.deleted == 2
    assert report.freed_bytes == 350
    assert not (tree / "cache" / "a.bin").exists()
    assert not (tree / "cache" / "sub" / "b.bin").exists()
    assert (tree / "keep.txt").exists()


def test_apply_keeps_cache_directory(roots, tree: Path):
    backend = NonRootBackend([str(tree)])
    engine.apply(backend, roots, ["app_cache"], dry_run=False)
    assert (tree / "cache").is_dir()


def test_apply_log_is_json_lines_serialisable(roots, tree: Path):
    backend = NonRootBackend([str(tree)])
    report = engine.apply(backend, roots, ["app_cache"], dry_run=True)
    for record in report.log:
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        parsed = json.loads(line)
        assert set(parsed) >= {"backend", "path", "action", "bytes", "result", "reason"}
        assert parsed["backend"] == "nonroot"


def test_expand_unknown_placeholder():
    with pytest.raises(KeyError):
        engine.expand_pattern("<nope>/x", {})


def test_expand_collapses_slashes():
    assert engine.expand_pattern("<a>//x", {"a": "/root/"}) == "/root/x"


# ---------------------------------------------------------------------------
# package cleaners through the engine
# ---------------------------------------------------------------------------

def test_scan_package_cleaner_uses_backend_listing(roots):
    shell = FakeShell({"pm list": (0, "package:com.a\npackage:com.b\n", "")})
    backend = AdbBackend(shell, roots["app_files"])
    report = engine.scan(backend, roots, ["other_app_cache"])

    packages = sorted(e.package for e in report.entries if e.package)
    assert packages == ["com.a", "com.b"]
    assert all(not e.size_known for e in report.entries)


def test_scan_unused_cleaner_needs_kotlin_list(roots):
    shell = FakeShell({"pm list": (0, "package:com.a\npackage:com.b\n", "")})
    backend = AdbBackend(shell, roots["app_files"])

    without = engine.scan(backend, roots, ["unused_app_cache"])
    assert without.entries == []

    with_list = engine.scan(
        backend, roots, ["unused_app_cache"], unused_packages=["com.b"]
    )
    assert [e.package for e in with_list.entries] == ["com.b"]


def test_apply_package_cleaner_clears_through_backend(roots):
    shell = FakeShell({"du -sk": (0, "32\t/x\n", "")})
    backend = AdbBackend(shell, roots["app_files"])

    report = engine.apply(
        backend, roots, ["unused_app_cache"], dry_run=False, unused_packages=["com.b"]
    )

    assert report.failed == 0
    assert report.freed_bytes == 32 * 1024
    assert any(c == "pm clear --cache-only com.b" for c in shell.calls)
    assert any("com.a" not in c for c in shell.calls)


def test_apply_package_cleaner_dry_run_touches_nothing(roots):
    shell = FakeShell()
    backend = AdbBackend(shell, roots["app_files"])
    report = engine.apply(
        backend, roots, ["unused_app_cache"], dry_run=True, unused_packages=["com.b"]
    )
    assert report.dry_run
    assert shell.calls == []


def test_apply_package_cleaner_counts_failures(roots):
    shell = FakeShell({"pm clear": (1, "", "boom")})
    backend = AdbBackend(shell, roots["app_files"])
    report = engine.apply(
        backend, roots, ["unused_app_cache"], dry_run=False, unused_packages=["com.b"]
    )
    assert report.failed == 1
    assert report.freed_bytes == 0


# ---------------------------------------------------------------------------
# api boundary never raises
# ---------------------------------------------------------------------------

def test_api_returns_error_json_on_bad_mode():
    out = json.loads(api.list_cleaners('{"mode": "bogus"}'))
    assert "error" in out


def test_api_root_mode_without_runner_errors():
    out = json.loads(api.list_cleaners('{"mode": "root"}'))
    assert "error" in out
    assert "su runner" in out["error"]


def test_api_detect_returns_available_list():
    out = json.loads(api.detect("{}"))
    assert "available" in out and "nonroot" in out["available"]


def test_api_detect_picks_up_registered_runners():
    api.set_adb_runner(lambda cmd: json.dumps({"code": 0, "stdout": "", "stderr": ""}))
    api.set_root_runner(lambda cmd: json.dumps({"code": 0, "stdout": "uid=0(root)", "stderr": ""}))
    try:
        out = json.loads(api.detect("{}"))
        assert "adb" in out["available"]
        assert "root" in out["available"]
    finally:
        api._ADB_RUNNER = None
        api._ROOT_RUNNER = None


def test_api_list_cleaners_with_roots(roots):
    payload = json.dumps({"mode": "nonroot", **roots})
    out = json.loads(api.list_cleaners(payload))
    assert out["backend"]["name"] == "nonroot"
    assert out["backend"]["supports_packages"] is False
    assert any(c["name"] == "app_cache" for c in out["cleaners"])


def test_api_scan_and_apply(roots, tree: Path):
    payload = json.dumps({"mode": "nonroot", "cleaners": ["app_cache"], **roots})
    scanned = json.loads(api.scan(payload))
    assert scanned["total_bytes"] == 350

    payload_dry = json.dumps(
        {"mode": "nonroot", "cleaners": ["app_cache"], "dry_run": True, **roots}
    )
    dry = json.loads(api.apply_clean(payload_dry))
    assert dry["dry_run"] is True
    assert (tree / "cache" / "a.bin").exists()

    payload_real = json.dumps(
        {"mode": "nonroot", "cleaners": ["app_cache"], "dry_run": False, **roots}
    )
    real = json.loads(api.apply_clean(payload_real))
    assert real["deleted"] == 2
    assert not (tree / "cache" / "a.bin").exists()


def test_api_scan_denies_cleaner_without_capability(roots):
    payload = json.dumps({"mode": "nonroot", "cleaners": ["logcat_dumps"], **roots})
    out = json.loads(api.scan(payload))
    assert out["entries"] == []
    assert out["total_bytes"] == 0
