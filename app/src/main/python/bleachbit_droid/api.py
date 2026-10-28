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

    vacuum = _vacuum_callable()
    # Kotlin sends `context.packageName`; without it a package cleaner reaches
    # our own package and `am force-stop` kills the clean halfway through.
    own = str(data.get("own_package") or "")

    if mode == "root":
        runner = _ROOT_RUNNER
        if runner is None:
            raise BackendError("root backend requested but no su runner registered")
        return RootBackend(runner, vacuum_runner=vacuum, own_package=own)
    if mode == "adb":
        runner = _ADB_RUNNER
        if runner is None:
            raise BackendError("adb backend requested but no runner registered")
        return AdbBackend(
            runner, roots["app_files"], vacuum_runner=vacuum, own_package=own
        )
    if mode == "nonroot":
        allowed = [p for p in (roots["app_files"], roots["app_cache"], roots["external"]) if p]
        # Shared storage is only reachable with MANAGE_EXTERNAL_STORAGE. The
        # whitelist used to hold the app's own three directories only, while
        # the backend advertised `shared_storage` regardless — so `thumbnails`
        # and `external_app_cache` (the two that actually hold hundreds of
        # megabytes) were scanned and then failed on every file with
        # "outside allowed roots". Grant and capability now move together.
        shared = bool(data.get("all_files"))
        if shared:
            allowed += [p for p in (roots["sdcard"], roots["android_media"]) if p]
        return NonRootBackend(allowed, vacuum_runner=vacuum, shared_storage=shared)
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
_VACUUM_RUNNER: Callable[[str], Any] | None = None


def _vacuum_callable():
    """Adapt the Kotlin ``VACUUM`` runner to the callback the backends expect.

    Kotlin hands us ``(path) -> json`` with ``{code, stdout, stderr}``; the
    backends call ``runner(path) -> (code, stdout, stderr)``.

    Chaquopy passes a Java object as a proxy that is *not* ``callable()``, it
    only exposes the methods of the class. So accept either a plain Python
    callable (tests) or an object with a ``run(path)`` method (``VacuumRunner``).
    """
    if _VACUUM_RUNNER is None:
        return None

    target = _VACUUM_RUNNER
    invoke = target if callable(target) else target.run

    def run(path: str):
        out = invoke(path)
        parsed = json.loads(out) if isinstance(out, str) else out
        return (
            int(parsed["code"]),
            str(parsed.get("stdout", "")),
            str(parsed.get("stderr", "")),
        )

    return run


def set_vacuum_runner(fn: Callable[[str], Any]) -> None:
    """Register the Kotlin ``sqlite3 VACUUM`` bridge.

    Separate from the shell runners on purpose: on the non-root tier there is
    no shell at all, the app just starts the bundled loader itself.
    """
    global _VACUUM_RUNNER
    _VACUUM_RUNNER = fn


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


def _streaming(fn: Callable[[dict[str, Any], Any], Any]) -> Callable[..., str]:
    """Like ``_safe``, but takes a Java callback as a *second argument*.

    The callback cannot travel inside the JSON payload. ``JSONObject.put``
    would turn the Kotlin object into the string
    ``cz.zombie.bleachbitdroid.ProgressSink@aee8990`` — which is exactly what
    happened, and why the progress panel stayed empty while the scan itself
    worked. Chaquopy has to hand the object over separately.
    """

    def wrapper(payload: str = "{}", callback: Any = None) -> str:
        try:
            data = json.loads(payload) if payload else {}
            return json.dumps(fn(data, callback), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - boundary must never raise
            return json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=5),
                },
                ensure_ascii=False,
            )

    return wrapper


def _as_callback(value):
    """Wrap a Kotlin object exposing ``onEvent(String)`` as a Python callable.

    Order matters. A Chaquopy proxy *is* ``callable()`` — it forwards
    ``__call__`` — but calling it does **not** reach ``onEvent(String)``; it
    raises, and because progress reporting is best-effort the exception is
    swallowed and every event is silently lost. That is exactly why the
    progress panel stayed empty after the callback started arriving as a real
    object instead of a string.

    So look for ``onEvent`` first and only fall back to ``__call__`` for plain
    Python callables (tests). The ``callAttr`` branch is the last resort for
    proxies that expose neither attribute nor are callable.
    """
    if value is None:
        return None

    on_event = getattr(value, "onEvent", None)
    if on_event is not None and callable(on_event):
        def invoke(event: dict) -> None:
            on_event(json.dumps(event, ensure_ascii=False))

        return invoke

    if callable(value):
        return value

    def invoke(event: dict) -> None:
        value.callAttr("onEvent", json.dumps(event, ensure_ascii=False))

    return invoke


@_streaming
def apply_clean_streaming(data: dict[str, Any], callback: Any = None) -> dict[str, Any]:
    """Like ``apply_clean`` but forwards every step to ``callback``.

    The callback receives JSON strings; the UI renders them as a live log.
    """
    backend = _backend(data)
    report = engine.apply(
        backend,
        _roots(data),
        _as_list(data.get("cleaners")),
        bool(data.get("dry_run", True)),
        _as_list(data.get("unused_packages")),
        _as_callback(callback),
    )
    return report.to_dict()


@_streaming
def scan_streaming(data: dict[str, Any], callback: Any = None) -> dict[str, Any]:
    """Like ``scan`` but forwards every step to ``callback``.

    A scan over a shell backend can take seconds per cleaner; without progress
    the UI looks frozen. The callback receives JSON strings, one per step.
    """
    backend = _backend(data)
    report = engine.scan(
        backend,
        _roots(data),
        _as_list(data.get("cleaners")),
        _as_list(data.get("unused_packages")),
        _as_callback(callback),
    )
    return report.to_dict()


__all__ = [
    "apply_clean",
    "apply_clean_streaming",
    "detect",
    "list_cleaners",
    "scan",
    "scan_streaming",
    "set_adb_runner",
    "set_root_runner",
    "set_vacuum_runner",
]
