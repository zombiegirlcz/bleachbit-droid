"""JSON-in / JSON-out entry points called from Kotlin via Chaquopy.

Contract: every function takes a single JSON string and returns a JSON string.
Errors are returned as ``{"error": "..."}`` instead of raising, so a Python
exception can never crash the Android UI thread.
"""

from __future__ import annotations

import json
import traceback
from typing import Any, Callable

from . import engine
from .backends import AdbBackend, BackendError, NonRootBackend, RootBackend


def _safe(fn: Callable[[dict[str, Any]], Any]) -> Callable[[str], str]:
    def wrapper(payload: str = "{}") -> str:
        try:
            data = json.loads(payload) if payload else {}
            return json.dumps(fn(data), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - boundary must never raise
            return json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=5),
                },
                ensure_ascii=False,
            )

    return wrapper


def _as_list(value):
    """Coerce a JSON field into a list of strings.

    Kotlin's JSONObject.put(String, Object) does not wrap collections, so an
    older client sends the *string* "[app_cache, thumbnails]" instead of a
    JSON array. Iterating that string yields characters and every lookup fails
    with KeyError: '['. Accept both shapes.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return [part.strip() for part in text.split(",") if part.strip()]
    return [str(value)]


def _roots(data: dict[str, Any]) -> dict[str, str]:
    """Build the placeholder -> path mapping from the Kotlin-supplied context."""
    return {
        "app_files": data.get("app_files") or "",
        "app_cache": data.get("app_cache") or "",
        "external": data.get("external") or "",
        "sdcard": data.get("sdcard") or "/sdcard",
        "android_data": data.get("android_data") or "/data/data",
        "android_media": data.get("android_media") or "/data/media/0",
    }


def _backend(data: dict[str, Any]):
    """Instantiate the requested backend (``nonroot`` / ``adb`` / ``root``)."""
    mode = data.get("mode", "nonroot")
    roots = _roots(data)

    if mode == "root":
        runner = _ROOT_RUNNER
        if runner is None:
            raise BackendError("root backend requested but no su runner registered")
        return RootBackend(runner)
    if mode == "adb":
        runner = _ADB_RUNNER
        if runner is None:
            raise BackendError("adb backend requested but no runner registered")
        return AdbBackend(runner, roots["app_files"])
    if mode == "nonroot":
        allowed = [p for p in (roots["app_files"], roots["app_cache"], roots["external"]) if p]
        return NonRootBackend(allowed)
    raise BackendError(f"unknown mode {mode!r}")


# ---------------------------------------------------------------------------
# Shell runner injection (Shizuku for ADB tier, su for root tier)
# ---------------------------------------------------------------------------

class _ShellRunner:
    """Wraps a Kotlin ``ShellRunner`` (``run(cmd) -> json``)."""

    def __init__(self, fn: Callable[[str], Any]) -> None:
        self._fn = fn

    def run(self, command: str) -> tuple[int, str, str]:
        out = self._fn(command)
        parsed = json.loads(out) if isinstance(out, str) else out
        return (
            int(parsed["code"]),
            str(parsed.get("stdout", "")),
            str(parsed.get("stderr", "")),
        )


_ADB_RUNNER: _ShellRunner | None = None
_ROOT_RUNNER: _ShellRunner | None = None


def set_adb_runner(fn: Callable[[str], Any]) -> None:
    """Register the Kotlin Shizuku bridge (called once at app start)."""
    global _ADB_RUNNER
    _ADB_RUNNER = _ShellRunner(fn)


def set_root_runner(fn: Callable[[str], Any]) -> None:
    """Register the Kotlin ``su`` bridge; only call when root was granted."""
    global _ROOT_RUNNER
    _ROOT_RUNNER = _ShellRunner(fn)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

@_safe
def detect(data: dict[str, Any]) -> dict[str, Any]:
    """Report which privilege level is actually usable right now."""
    available: list[str] = ["nonroot"]
    notes: dict[str, str] = {}

    if _ROOT_RUNNER is not None:
        try:
            code, out, _ = _ROOT_RUNNER.run("id")
            if code == 0 and "uid=0" in out:
                available.append("root")
            else:
                notes["root"] = f"id said {out.strip()!r} (exit {code})"
        except Exception as exc:  # noqa: BLE001
            notes["root"] = f"{type(exc).__name__}: {exc}"
    else:
        notes["root"] = "su runner not registered"

    if _ADB_RUNNER is not None:
        try:
            code, _, _ = _ADB_RUNNER.run("id")
            if code == 0:
                available.append("adb")
            else:
                notes["adb"] = f"id exited {code}"
        except Exception as exc:  # noqa: BLE001
            notes["adb"] = f"{type(exc).__name__}: {exc}"
    else:
        notes["adb"] = "runner not registered"

    return {"available": available, "notes": notes}


@_safe
def list_cleaners(data: dict[str, Any]) -> dict[str, Any]:
    backend = _backend(data)
    return {
        "backend": backend.to_dict(),
        "cleaners": engine.describe(backend),
    }


@_safe
def scan(data: dict[str, Any]) -> dict[str, Any]:
    backend = _backend(data)
    report = engine.scan(
        backend,
        _roots(data),
        _as_list(data.get("cleaners")),
        _as_list(data.get("unused_packages")),
    )
    return report.to_dict()


@_safe
def apply_clean(data: dict[str, Any]) -> dict[str, Any]:
    backend = _backend(data)
    report = engine.apply(
        backend,
        _roots(data),
        _as_list(data.get("cleaners")),
        bool(data.get("dry_run", True)),
        _as_list(data.get("unused_packages")),
    )
    return report.to_dict()


def _as_callback(value):
    """Wrap a Kotlin object exposing ``onEvent(String)`` as a Python callable.

    Chaquopy passes Java objects into Python as ``PyObject``. Calling
    ``callAttr`` on it from a worker thread is safe, and exceptions are
    swallowed because progress reporting must never abort a clean.
    """
    if value is None:
        return None
    if callable(value):
        return value

    def invoke(event: dict) -> None:
        value.callAttr("onEvent", json.dumps(event, ensure_ascii=False))

    return invoke


@_safe
def apply_clean_streaming(data: dict[str, Any]) -> dict[str, Any]:
    """Like ``apply_clean`` but forwards every step to ``data["callback"]``.

    The callback receives JSON strings; the UI renders them as a live log.
    """
    backend = _backend(data)
    report = engine.apply(
        backend,
        _roots(data),
        _as_list(data.get("cleaners")),
        bool(data.get("dry_run", True)),
        _as_list(data.get("unused_packages")),
        _as_callback(data.get("callback")),
    )
    return report.to_dict()


__all__ = [
    "apply_clean",
    "apply_clean_streaming",
    "detect",
    "list_cleaners",
    "scan",
    "set_adb_runner",
    "set_root_runner",
]
