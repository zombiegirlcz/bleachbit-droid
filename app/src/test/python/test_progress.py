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