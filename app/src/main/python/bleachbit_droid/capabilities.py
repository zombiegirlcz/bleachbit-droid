"""Capability taxonomy.

A cleaner declares which capabilities it needs; a backend declares which
capabilities it provides. A cleaner may only run when its requirements are a
subset of the backend's capabilities. This is the core of the project thesis:
higher privilege => deeper cleaning.
"""

from __future__ import annotations

from enum import Enum

from .models import Capability  # re-exported for convenience

# Ordered from least to most privileged.
CAPABILITY_ORDER: tuple[Capability, ...] = (
    Capability.APP_DATA,
    Capability.SHARED_STORAGE,
    Capability.OTHER_APP_DATA,
    Capability.SYSTEM,
)

# Human readable description (Czech, shown in UI).
CAPABILITY_LABELS: dict[Capability, str] = {
    Capability.APP_DATA: "Vlastní data aplikace",
    Capability.SHARED_STORAGE: "Sdílené úložiště",
    Capability.OTHER_APP_DATA: "Data jiných aplikací",
    Capability.SYSTEM: "Systémové cesty",
}


def missing(required: tuple[Capability, ...], provided: frozenset[Capability]) -> list[Capability]:
    """Return capabilities required by a cleaner but not provided by a backend."""
    return [cap for cap in required if cap not in provided]


def rank(cap: Capability) -> int:
    return CAPABILITY_ORDER.index(cap)


__all__ = ["Capability", "CAPABILITY_ORDER", "CAPABILITY_LABELS", "missing", "rank"]