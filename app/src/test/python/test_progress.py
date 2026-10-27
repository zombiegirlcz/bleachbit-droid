"""Progress callbacks must reach Python as objects, not as strings.

Regression: the Kotlin bridge used to put the `ProgressSink` object into the
JSON payload (`put("callback", sink)`). `JSONObject` then serialised it into
``"cz.zombie.bleachbitdroid.ProgressSink@aee8990"``, so Python received a
String, ``_as_callback`` treated it as a plain value, and every progress event
went nowhere — the scan worked, the panel stayed empty.

The fix passes the callback as a separate Chaquopy argument. These tests pin
the Python side of that contract: a callable, a Java-style object with
``callAttr``/``onEvent``, and the payload not carrying a callback at all.

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

from bleachbit_droid import api, engine  # noqa: E402
from bleachbit_droid.backends import NonRootBackend  # noqa: E402


class FakePyObject:
    """Mimics a Chaquopy proxy: not callable, exposes ``callAttr``.

    Chaquopy hands Kotlin objects to Python as proxies with the Java methods
    reachable through ``callAttr``; ``callable(proxy)`` is False.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def callAttr(self, name: str, arg: str) -> None:  # noqa: N802 - Java naming
        assert name == "onEvent"
        self.events.append(json.loads(arg))


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(b"x" * 100)
    (cache / "b.bin").write_bytes(b"y" * 200)
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


def test_as_callback_accepts_non_callable_proxy():
    proxy = FakePyObject()
    assert not callable(proxy)

    fn = api._as_callback(proxy)
    fn({"type": "scan_start", "backend": "nonroot"})

    assert proxy.events == [{"type": "scan_start", "backend": "nonroot"}]


def test_as_callback_passes_callables_through():
    seen = []
    fn = api._as_callback(lambda e: seen.append(e))
    fn({"type": "x"})
    assert seen == [{"type": "x"}]


def test_as_callback_none_stays_none():
    assert api._as_callback(None) is None


def test_scan_streaming_takes_callback_as_second_argument(roots, tree: Path):
    """The public API must accept the callback outside the JSON payload."""
    proxy = FakePyObject()
    payload = json.dumps({"mode": "nonroot", "cleaners": ["app_cache"], **roots})

    out = json.loads(api.scan_streaming(payload, proxy))

    assert "error" not in out, out
    types = [e["type"] for e in proxy.events]
    assert "scan_start" in types
    assert "scan_done" in types
    # one found event per file
    assert types.count("found") == 2


def test_scan_streaming_without_callback_still_works(roots, tree: Path):
    payload = json.dumps({"mode": "nonroot", "cleaners": ["app_cache"], **roots})
    out = json.loads(api.scan_streaming(payload))
    assert "error" not in out, out
    assert out["total_bytes"] == 300


def test_apply_clean_streaming_takes_callback_as_second_argument(roots, tree: Path):
    proxy = FakePyObject()
    payload = json.dumps(
        {"mode": "nonroot", "cleaners": ["app_cache"], "dry_run": True, **roots}
    )

    out = json.loads(api.apply_clean_streaming(payload, proxy))

    assert "error" not in out, out
    types = [e["type"] for e in proxy.events]
    assert "start" in types
    assert "would_delete" in types


def test_progress_callback_never_breaks_a_scan(roots, tree: Path):
    """A raising callback must not abort the run (progress is best effort)."""

    class Boom:
        def callAttr(self, name, arg):  # noqa: N802
            raise RuntimeError("callback blew up")

    payload = json.dumps({"mode": "nonroot", "cleaners": ["app_cache"], **roots})
    out = json.loads(api.scan_streaming(payload, Boom()))

    assert "error" not in out, out
    assert out["total_bytes"] == 300


def test_engine_scan_emits_progress_events(tree: Path, roots: dict[str, str]):
    events: list[dict] = []
    backend = NonRootBackend([str(tree)])

    report = engine.scan(backend, roots, ["app_cache"], on_event=events.append)

    assert report.total_bytes == 300
    types = [e["type"] for e in events]
    assert types[0] == "scan_start"
    assert types[-1] == "scan_done"
    assert "cleaner_start" in types and "cleaner_done" in types
    found = [e for e in events if e["type"] == "found"]
    assert len(found) == 2
    assert all("path" in e and "bytes" in e for e in found)
    # running totals must be monotonic
    totals = [e["total_bytes"] for e in found]
    assert totals == sorted(totals)

class CallableProxyWithOnEvent:
    """Mimics a Chaquopy proxy: it *is* callable and also has ``onEvent``.

    This is the shape that broke progress. `_as_callback()` used to test
    `callable(value)` first, so it handed the proxy straight to the engine.
    Calling the proxy does not reach `onEvent(String)`; it raises, and because
    progress reporting is best-effort the exception was swallowed and every
    event was lost — the scan worked and the panel stayed empty.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.called_directly = False

    def __call__(self, *args, **kwargs):  # noqa: D105
        self.called_directly = True
        raise TypeError("proxy is not directly callable")

    def onEvent(self, json_text: str) -> None:  # noqa: N802 - Java naming
        self.events.append(json.loads(json_text))


def test_as_callback_prefers_onEvent_over_call(roots, tree: Path):
    proxy = CallableProxyWithOnEvent()
    assert callable(proxy)  # the trap: this used to short-circuit

    payload = json.dumps({"mode": "nonroot", "cleaners": ["app_cache"], **roots})
    out = json.loads(api.scan_streaming(payload, proxy))

    assert "error" not in out, out
    assert proxy.called_directly is False, "proxy was called instead of onEvent()"
    types = [e["type"] for e in proxy.events]
    assert "scan_start" in types and "scan_done" in types
    assert types.count("found") == 2


def test_as_callback_falls_back_to_plain_callable():
    """A real Python callable has no onEvent and must still work."""
    seen = []

    def cb(event):
        seen.append(event)

    fn = api._as_callback(cb)
    fn({"type": "x"})
    assert seen == [{"type": "x"}]


# ---------------------------------------------------------------------------
# the numbers in the events have to be the numbers the UI prints
# ---------------------------------------------------------------------------

def test_cleaner_done_reports_only_its_own_share(tmp_path: Path):
    """Regression: it carried the running total of the whole scan.

    The UI printed that next to the cleaner's name ("→ thumbnails: 901 MB"),
    so every cleaner after the first looked like it had found everything.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(b"x" * 100)
    files_cache = tmp_path / "cache2"
    files_cache.mkdir()
    (files_cache / "b.bin").write_bytes(b"y" * 250)

    roots = {
        "app_files": str(tmp_path),
        "app_cache": str(cache),
        "external": str(tmp_path),
        "sdcard": str(tmp_path),
        "android_data": str(tmp_path),
        "android_media": str(tmp_path),
    }
    events: list[dict] = []
    backend = NonRootBackend([str(tmp_path)])

    engine.scan(backend, roots, ["app_cache"], on_event=events.append)

    done = [e for e in events if e["type"] == "cleaner_done"]
    assert len(done) == 1
    assert done[0]["bytes"] == 100, done[0]
    assert done[0]["entries"] == 1
    assert done[0]["total_bytes"] == 100


def test_apply_reports_the_scan_it_runs(tree: Path, roots: dict[str, str]):
    """Regression: apply() called scan() without the callback.

    Scanning is the long part of a clean on a shell backend, so the UI showed
    nothing at all between "Spouštím čištění…" and the first delete.
    """
    events: list[dict] = []
    backend = NonRootBackend([str(tree)])

    engine.apply(backend, roots, ["app_cache"], dry_run=True, on_event=events.append)

    types = [e["type"] for e in events]
    assert "start" in types
    assert "scan_start" in types, types
    assert "found" in types, types
    assert "scanned" in types


def test_done_event_carries_what_was_freed(tree: Path, roots: dict[str, str]):
    """Regression: `done` only had `freed_bytes`, the sink only read
    `total_bytes`, so every clean ended with "Uvolněno 0 B"."""
    events: list[dict] = []
    backend = NonRootBackend([str(tree)])

    report = engine.apply(
        backend, roots, ["app_cache"], dry_run=False, on_event=events.append
    )

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["freed_bytes"] == report.freed_bytes > 0
    assert done[0]["unknown"] == 0

    scanned = [e for e in events if e["type"] == "scanned"]
    assert scanned[0]["bytes"] == report.freed_bytes


def test_package_clear_without_a_measurable_size_is_reported_as_unknown():
    """The ADB tier cannot read /data/data/<pkg>/cache (uid 2000 gets EACCES,
    verified on device), so it must say "unknown", never "0 B"."""
    from bleachbit_droid.backends import AdbBackend

    class Shell:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, command: str):
            self.calls.append(command)
            if command.startswith("du "):
                return 1, "", "du: Permission denied"
            if command.startswith("pm list packages"):
                return 0, "package:com.example\n", ""
            return 0, "", ""

    shell = Shell()
    backend = AdbBackend(shell, "/app")

    stats = backend.clear_app_cache("com.example")
    assert stats.bytes == 0
    assert stats.size_known is False
    assert any("pm clear --cache-only" in c for c in shell.calls), shell.calls

    events: list[dict] = []
    report = engine.apply(
        backend,
        {k: "/x" for k in
         ("app_files", "app_cache", "external", "sdcard", "android_data", "android_media")},
        ["other_app_cache"],
        dry_run=False,
        on_event=events.append,
    )
    assert report.unknown == 1
    assert report.failed == 0
    deleted = [e for e in events if e["type"] == "deleted"]
    assert deleted and deleted[0]["size_known"] is False
    assert [e for e in events if e["type"] == "done"][0]["unknown"] == 1
