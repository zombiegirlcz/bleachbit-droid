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
    if spec.kind is Kind.PACKAGE:
        assert not spec.paths, "package cleaner must not declare paths"
    else:
        # path and sqlite cleaners both reach targets through globs
        assert spec.paths, f"{spec.kind.value} cleaner must declare paths"
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


def test_root_backend_sends_plain_commands_to_its_runner():
    """The runner is root, so commands must NOT be wrapped in `su -c`.

    Regression: every command used to be re-wrapped in `su -c '...'`, which
    spawned a fresh `su` process per command inside the persistent root shell
    the Kotlin side keeps open exactly to avoid that.
    """
    shell = FakeShell({"du": (0, "2048\t/x\n", "")})
    backend = RootBackend(shell)

    stats = backend.delete("/data/system/dropbox/x")

    assert stats.bytes == 2048
    assert shell.calls, "nothing reached the runner"
    assert not any(c.startswith("su -c") for c in shell.calls), shell.calls


def test_root_backend_clears_cache_as_the_apps_own_uid():
    """Root must not create root-owned files inside an app's data dir.

    Deleting as root leaves root-owned files behind, the app can no longer
    write them, and the next launch misbehaves. The cache is therefore cleared
    through `su <uid>`, so the filesystem sees the app's own uid.
    """
    shell = FakeShell({
        "stat": (0, "10316\n", ""),
        "du": (0, "150748\t/data/data/com.example/cache\n", ""),
        "am force-stop": (0, "", ""),
    })
    backend = RootBackend(shell)

    stats = backend.clear_app_cache("com.example", force_stop=True)

    # measured before deleting, so the caller can report what was freed
    assert stats.bytes == 150748 * 1024
    # the delete itself ran as the app's uid, not as root
    assert any(
        c.startswith("su 10316 -c") and "find" in c and "-delete" in c
        for c in shell.calls
    ), shell.calls
    # and never through the package manager, which would also wipe bookkeeping
    assert not any("pm clear" in c for c in shell.calls), shell.calls
    # nothing may ever be chowned
    assert not any("chown" in c for c in shell.calls), shell.calls


def test_root_backend_refuses_app_without_data_directory():
    shell = FakeShell({"stat": (1, "", "No such file or directory")})
    backend = RootBackend(shell)
    with pytest.raises(BackendError):
        backend.clear_app_cache("com.gone")
    assert not any("find" in c for c in shell.calls), shell.calls


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


def test_api_accepts_stringified_cleaner_list(roots, tree: Path):
    """Kotlin's JSONObject.put(String, Object) stringifies a List, so the field
    can arrive as "[app_cache, thumbnails]" instead of a JSON array. That used
    to make Python iterate characters and fail with KeyError: '['."""
    payload = json.dumps(
        {"mode": "nonroot", "cleaners": "[app_cache, thumbnails]", **roots}
    )
    out = json.loads(api.scan(payload))
    assert "error" not in out, out
    assert out["total_bytes"] > 0


def test_as_list_variants():
    assert api._as_list(None) is None
    assert api._as_list([]) == []
    assert api._as_list("") == []
    assert api._as_list("[a, b]") == ["a", "b"]
    assert api._as_list("a,b") == ["a", "b"]
    assert api._as_list(["a"]) == ["a"]

# ---------------------------------------------------------------------------
# sqlite cleaners (VACUUM through the injected runner)
# ---------------------------------------------------------------------------

def _sqlite_specs():
    from bleachbit_droid.cleaners import all_cleaners
    from bleachbit_droid.models import Kind

    return [c for c in all_cleaners() if c.kind is Kind.SQLITE]


def test_sqlite_cleaners_exist():
    names = {c.name for c in _sqlite_specs()}
    assert {"own_databases", "other_databases", "shared_databases"} <= names


def test_nonroot_without_vacuum_runner_cannot_run_sqlite(roots):
    backend = NonRootBackend([roots["app_files"]])
    runnable = {s.name for s in engine.select_cleaners(backend)}
    assert "own_databases" not in runnable
    items = {i["name"]: i for i in engine.describe(backend)}
    assert items["own_databases"]["enabled"] is False
    assert "sqlite3" in items["own_databases"]["missing"]


def test_nonroot_with_vacuum_runner_can_run_own_databases(roots):
    backend = NonRootBackend([roots["app_files"]], vacuum_runner=lambda path: (0, "", ""))
    runnable = {s.name for s in engine.select_cleaners(backend)}
    assert "own_databases" in runnable
    # other apps' databases still need a higher tier
    assert "other_databases" not in runnable


def test_scan_finds_databases(roots, tree: Path):
    db = tree / "app.db"
    db.write_bytes(b"SQLite format 3" + b"\0" * 100)

    backend = NonRootBackend([str(tree)], vacuum_runner=lambda path: (0, "", ""))
    report = engine.scan(backend, roots, ["own_databases"])

    paths = {e.path for e in report.entries}
    assert str(db) in paths
    assert report.total_bytes == len(b"SQLite format 3" + b"\0" * 100)


def test_scan_ignores_directories_named_db(roots, tree: Path):
    (tree / "notadb.db").mkdir()

    backend = NonRootBackend([str(tree)], vacuum_runner=lambda path: (0, "", ""))
    report = engine.scan(backend, roots, ["own_databases"])
    assert report.entries == []


def test_apply_calls_vacuum_runner_not_delete(roots, tree: Path):
    db = tree / "app.db"
    db.write_bytes(b"x" * 500)
    called = []

    def vacuum(path):
        called.append(path)
        # Emulate VACUUM rewriting the file smaller.
        open(path, "wb").write(b"y" * 100)
        return (0, "", "")

    backend = NonRootBackend([str(tree)], vacuum_runner=vacuum)
    report = engine.apply(backend, roots, ["own_databases"], dry_run=False)

    assert called == [str(db)]
    assert db.exists(), "VACUUM must not delete the database"
    assert report.freed_bytes == 400
    assert report.failed == 0


def test_apply_dry_run_does_not_vacuum(roots, tree: Path):
    (tree / "app.db").write_bytes(b"x" * 500)
    called = []

    backend = NonRootBackend([str(tree)], vacuum_runner=lambda p: called.append(p) or (0, "", ""))
    report = engine.apply(backend, roots, ["own_databases"], dry_run=True)

    assert called == []
    assert report.freed_bytes == 500
    assert all(r.result.value == "dry_run" for r in report.log)


def test_apply_reports_vacuum_failure(roots, tree: Path):
    (tree / "app.db").write_bytes(b"x" * 500)

    backend = NonRootBackend(
        [str(tree)], vacuum_runner=lambda path: (1, "", "file is not a database")
    )
    report = engine.apply(backend, roots, ["own_databases"], dry_run=False)

    assert report.failed == 1
    assert report.freed_bytes == 0
    assert any("not a database" in r.reason for r in report.log)


def test_vacuum_grows_file_reports_zero_not_negative(roots, tree: Path):
    db = tree / "app.db"
    db.write_bytes(b"x" * 100)

    def vacuum(path):
        open(path, "wb").write(b"y" * 500)
        return (0, "", "")

    backend = NonRootBackend([str(tree)], vacuum_runner=vacuum)
    report = engine.apply(backend, roots, ["own_databases"], dry_run=False)
    assert report.freed_bytes == 0


def test_api_registers_vacuum_runner(roots, tree: Path):
    db = tree / "app.db"
    db.write_bytes(b"x" * 400)
    seen = []

    api.set_vacuum_runner(lambda path: json.dumps(
        {"code": 0 if seen.append(path) is None else 0, "stdout": "", "stderr": ""}
    ))
    try:
        payload = json.dumps(
            {"mode": "nonroot", "cleaners": ["own_databases"], "dry_run": False, **roots}
        )
        out = json.loads(api.apply_clean(payload))
        assert "error" not in out, out
        assert seen == [str(db)]
    finally:
        api._VACUUM_RUNNER = None


def test_api_accepts_object_runner_with_run_method(roots, tree: Path):
    """Chaquopy hands Python a Java object that is not `callable()`.

    Regression: `_vacuum_callable()` used to call the proxy directly, which
    raised `TypeError: VacuumRunner is not callable` on device.
    """
    db = tree / "app.db"
    db.write_bytes(b"x" * 400)
    seen = []

    class JavaStyleRunner:
        """Mimics a Chaquopy proxy: a method, no __call__."""

        def run(self, path):
            seen.append(path)
            return json.dumps({"code": 0, "stdout": "", "stderr": ""})

    api.set_vacuum_runner(JavaStyleRunner())
    try:
        assert not callable(api._VACUUM_RUNNER)
        payload = json.dumps(
            {"mode": "nonroot", "cleaners": ["own_databases"], "dry_run": False, **roots}
        )
        out = json.loads(api.apply_clean(payload))
        assert "error" not in out, out
        assert seen == [str(db)]
    finally:
        api._VACUUM_RUNNER = None


# ---------------------------------------------------------------------------
# non-root tier: the shared-storage grant and the whitelist move together
# ---------------------------------------------------------------------------

def _nonroot_payload(tree: Path, all_files: bool, cleaners: list[str]) -> str:
    """An api payload shaped like the one PythonBridge builds on device."""
    return json.dumps(
        {
            "mode": "nonroot",
            "app_files": str(tree / "app"),
            "app_cache": str(tree / "app" / "cache"),
            "external": str(tree / "app" / "ext"),
            "sdcard": str(tree / "sd"),
            "android_media": str(tree / "sd"),
            "all_files": all_files,
            "cleaners": cleaners,
            "dry_run": False,
        }
    )


@pytest.fixture()
def shared_tree(tmp_path: Path) -> Path:
    """A fake /sdcard next to a fake app sandbox."""
    (tmp_path / "app" / "cache").mkdir(parents=True)
    (tmp_path / "app" / "ext").mkdir(parents=True)
    thumbs = tmp_path / "sd" / "DCIM" / ".thumbnails"
    thumbs.mkdir(parents=True)
    (thumbs / "t.jpg").write_bytes(b"t" * 64)
    ext_cache = tmp_path / "sd" / "Android" / "data" / "com.x" / "cache"
    ext_cache.mkdir(parents=True)
    (ext_cache / "c.bin").write_bytes(b"c" * 128)
    return tmp_path


def test_nonroot_without_all_files_access_does_not_offer_shared_storage(shared_tree: Path):
    """Promising a clean we cannot perform is worse than saying we cannot.

    Regression: the backend advertised ``shared_storage`` unconditionally while
    its whitelist held only the app's own directories. Enumeration does not go
    through the guard, so a scan found /sdcard and every delete then failed
    with "outside allowed roots".
    """
    out = json.loads(api.list_cleaners(_nonroot_payload(shared_tree, False, [])))

    assert out["backend"]["capabilities"] == ["app_data"]
    by = {c["name"]: c for c in out["cleaners"]}
    for name in ("thumbnails", "external_app_cache", "shared_databases"):
        assert not by[name]["enabled"], name
        assert "shared_storage" in by[name]["missing"], by[name]
        assert by[name]["missing_labels"], "the UI needs a Czech reason"


def test_nonroot_with_all_files_access_actually_deletes_in_shared_storage(shared_tree: Path):
    payload = _nonroot_payload(
        shared_tree, True, ["thumbnails", "external_app_cache"]
    )

    scanned = json.loads(api.scan(payload))
    assert scanned["total_bytes"] == 64 + 128

    report = json.loads(api.apply_clean(payload))
    assert report["failed"] == 0, report["log"]
    assert report["deleted"] == 2
    assert report["freed_bytes"] == 64 + 128
    assert not (shared_tree / "sd" / "DCIM" / ".thumbnails" / "t.jpg").exists()
    assert not list((shared_tree / "sd" / "Android" / "data" / "com.x" / "cache").iterdir())


# ---------------------------------------------------------------------------
# empty files are still files
# ---------------------------------------------------------------------------

def test_zero_length_files_are_scanned_and_deleted(tree: Path, roots: dict[str, str]):
    """Regression: a `size == 0` guard in scan() reported them and left them.

    They were listed by the scan, so the user was told they would go, and then
    every clean silently kept them forever.
    """
    (tree / "cache" / "empty.bin").write_bytes(b"")
    backend = NonRootBackend([str(tree)])

    scanned = engine.scan(backend, roots, ["app_cache"])
    assert str(tree / "cache" / "empty.bin") in [e.path for e in scanned.entries]

    engine.apply(backend, roots, ["app_cache"], dry_run=False)
    assert not (tree / "cache" / "empty.bin").exists()


# ---------------------------------------------------------------------------
# databases: only where sqlite3 can run as the owner of the file
# ---------------------------------------------------------------------------

def test_foreign_databases_are_not_offered_on_any_tier():
    """The bundled sqlite3 can only ever run as our own uid.

    It lives in our filesDir (mode 0700) and the device ships no system
    sqlite3, so VACUUMing another app's database would mean running as root
    and leaving root-owned journal files in foreign app data — the failure this
    project already paid for once. The cleaner must be withheld, not offered
    and then failed.
    """
    for backend in (
        RootBackend(FakeShell(), vacuum_runner=lambda path: (0, "", "")),
        AdbBackend(FakeShell(), "/app", vacuum_runner=lambda path: (0, "", "")),
    ):
        assert backend.supports_sqlite
        assert backend.can_vacuum([Capability.APP_DATA])
        assert not backend.can_vacuum([Capability.OTHER_APP_DATA])

        by = {c["name"]: c for c in engine.describe(backend)}
        assert not by["other_databases"]["enabled"], backend.name
        assert "sqlite3_as_owner" in by["other_databases"]["missing"]
        # ...while our own databases stay available.
        assert by["own_databases"]["enabled"], backend.name
        assert "other_databases" not in [
            s.name for s in engine.select_cleaners(backend)
        ]


def test_nonroot_vacuum_respects_the_whitelist(tmp_path: Path):
    """Regression: `kind=sqlite` bypassed the root whitelist entirely.

    `_SqliteMixin.vacuum_db` handed the path straight to the runner, so any
    database the process could open was rewritable regardless of the roots the
    backend was configured with (AGENTS.md §6/4).
    """
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"SQLite format 3\x00")
    inside_root = tmp_path / "allowed"
    inside_root.mkdir()
    inside = inside_root / "ok.db"
    inside.write_bytes(b"SQLite format 3\x00")

    vacuumed: list[str] = []

    def runner(path: str):
        vacuumed.append(path)
        return 0, "", ""

    backend = NonRootBackend([str(inside_root)], vacuum_runner=runner)

    backend.vacuum_db(str(inside))
    assert vacuumed == [str(inside)]

    with pytest.raises(BackendError):
        backend.vacuum_db(str(outside))
    assert vacuumed == [str(inside)], "the runner must not even be called"
