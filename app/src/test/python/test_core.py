"""Regression tests for the Python core.

Run from ``app/src/main/python``'s parent, i.e.:

    cd app && python3 -m pytest src/test/python -q

All deletion tests use throwaway fixture trees, never real system data
(AGENTS.md §7).
"""

from __future__ import annotations

import json
import os
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
    shell_quote,
)
from bleachbit_droid.capabilities import missing  # noqa: E402
from bleachbit_droid.cleaners import all_cleaners, by_name  # noqa: E402
from bleachbit_droid.models import Capability, Risk  # noqa: E402


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


# ---------------------------------------------------------------------------
# cleaner metadata (AGENTS.md §8)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", all_cleaners(), ids=lambda s: s.name)
def test_cleaner_metadata_complete(spec):
    assert spec.name and spec.description
    assert spec.requires, "cleaner must declare requires"
    assert spec.paths, "cleaner must declare paths"
    assert isinstance(spec.risk, Risk)
    for target in spec.paths:
        assert target.pattern.startswith("/") or target.pattern.startswith("<")


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


# ---------------------------------------------------------------------------
# capability gating
# ---------------------------------------------------------------------------

def test_missing_capabilities():
    have = frozenset({Capability.APP_DATA})
    assert missing((Capability.APP_DATA,), have) == []
    assert missing((Capability.APP_DATA, Capability.SYSTEM), have) == [Capability.SYSTEM]


def test_nonroot_cannot_run_system_cleaners(roots):
    backend = NonRootBackend([roots["app_files"]])
    runnable = {s.name for s in engine.select_cleaners(backend)}
    assert "app_cache" in runnable
    assert "logcat_dumps" not in runnable
    assert "apk_leftovers" not in runnable


def test_describe_marks_missing(roots):
    backend = NonRootBackend([roots["app_files"]])
    items = {i["name"]: i for i in engine.describe(backend)}
    assert items["app_cache"]["enabled"] is True
    assert items["logcat_dumps"]["enabled"] is False
    assert "system" in items["logcat_dumps"]["missing"]


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


def test_shell_quote():
    assert shell_quote("a b") == "'a b'"
    assert shell_quote("it's") == "'it'\\''s'"


def test_adb_backend_rejects_relative_and_traversal(tmp_path: Path):
    class FakeRunner:
        def run(self, command):
            return 0, "", ""

    backend = AdbBackend(FakeRunner(), str(tmp_path))
    with pytest.raises(BackendError):
        backend._guarded("relative/path")
    with pytest.raises(BackendError):
        backend._guarded("/data/../etc")
    assert backend._guarded("/data/local/tmp/x") == "/data/local/tmp/x"


def test_adb_backend_uses_runner():
    calls = []

    class FakeRunner:
        def run(self, command):
            calls.append(command)
            if command.startswith("du"):
                return 0, "4096\t/x\n", ""
            return 0, "", ""

    backend = AdbBackend(FakeRunner(), "/app")
    stats = backend.delete("/data/local/tmp/x")
    assert stats.bytes == 4096
    assert any(c.startswith("rm -rf") for c in calls)


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
# api boundary never raises
# ---------------------------------------------------------------------------

def test_api_returns_error_json_on_bad_mode():
    out = json.loads(api.list_cleaners('{"mode": "bogus"}'))
    assert "error" in out


def test_api_detect_returns_available_list():
    out = json.loads(api.detect("{}"))
    assert "available" in out and "nonroot" in out["available"]


def test_api_list_cleaners_with_roots(roots):
    payload = json.dumps({"mode": "nonroot", **roots})
    out = json.loads(api.list_cleaners(payload))
    assert out["backend"]["name"] == "nonroot"
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
