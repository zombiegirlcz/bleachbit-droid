"""BleachBit Droid — Python core.

Layout:
    capabilities  — capability taxonomy (what a backend can touch)
    models        — data classes (CleanerSpec, ScanResult, CleanResult)
    backends      — PrivilegeBackend implementations (nonroot / adb / root)
    cleaners      — cleaner registry
    engine        — scan/apply orchestration + structured logging
    api           — JSON-in/JSON-out entry points called from Kotlin
"""

from .api import apply_clean, detect, list_cleaners, scan  # noqa: F401

__all__ = ["apply_clean", "detect", "list_cleaners", "scan"]