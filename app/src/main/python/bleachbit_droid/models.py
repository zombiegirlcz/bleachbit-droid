"""Data model for the Python core.

Everything here is plain data so it can be serialised to/from JSON across the
Chaquopy boundary (Kotlin <-> Python) and written to the structured log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Capability(str, Enum):
    """What a path/backend can reach."""

    APP_DATA = "app_data"
    SHARED_STORAGE = "shared_storage"
    OTHER_APP_DATA = "other_app_data"
    SYSTEM = "system"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Action(str, Enum):
    SCAN = "scan"
    DELETE = "delete"
    SKIP = "skip"
    ERROR = "error"


class Result(str, Enum):
    OK = "ok"
    DRY_RUN = "dry_run"
    DENIED = "denied"
    MISSING = "missing"
    FAILED = "failed"


class Kind(str, Enum):
    """How a cleaner reaches its targets."""

    PATH = "path"        # filesystem globs, wiped through backend.delete()
    PACKAGE = "package"  # app packages, wiped through `pm clear --cache-only`
    SQLITE = "sqlite"    # databases, reclaimed with VACUUM through the guest toolchain


@dataclass(frozen=True)
class Target:
    """One path pattern belonging to a cleaner.

    ``pattern`` may contain ``*`` (single segment glob) and ``**`` (recursive).
    ``requires`` is the capability needed to actually reach the path.
    """

    pattern: str
    requires: Capability
    recursive: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "requires": self.requires.value,
            "recursive": self.recursive,
        }


@dataclass(frozen=True)
class CleanerSpec:
    """Static declaration of a cleaner (see AGENTS.md section 8)."""

    name: str
    description: str
    requires: tuple[Capability, ...]
    risk: Risk
    paths: tuple[Target, ...] = ()
    kind: Kind = Kind.PATH
    # For kind=PACKAGE: only consider packages unused for at least N days.
    unused_days: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("cleaner name must not be empty")
        if not self.requires:
            raise ValueError(f"cleaner {self.name!r} must declare at least one capability")
        if self.kind is Kind.PATH and not self.paths:
            raise ValueError(f"cleaner {self.name!r} must declare at least one path")
        if self.kind is Kind.PACKAGE and not self.paths:
            # package cleaners have no paths by design
            pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "requires": [c.value for c in self.requires],
            "risk": self.risk.value,
            "kind": self.kind.value,
            "unused_days": self.unused_days,
            "paths": [p.to_dict() for p in self.paths],
        }


@dataclass
class Entry:
    """A single scan hit: either a filesystem entry or an app package."""

    cleaner: str
    path: str
    size: int
    is_dir: bool
    requires: Capability
    package: str | None = None
    # False when the backend cannot know the size without deleting (packages).
    size_known: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "cleaner": self.cleaner,
            "path": self.path,
            "size": self.size,
            "size_known": self.size_known,
            "is_dir": self.is_dir,
            "requires": self.requires.value,
            "package": self.package,
        }


@dataclass
class LogRecord:
    """One structured log line (JSON lines, AGENTS.md section 8)."""

    backend: str
    path: str
    action: Action
    bytes: int = 0
    result: Result = Result.OK
    reason: str = ""
    cleaner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "path": self.path,
            "action": self.action.value,
            "bytes": self.bytes,
            "result": self.result.value,
            "reason": self.reason,
            "cleaner": self.cleaner,
        }


@dataclass
class ScanReport:
    backend: str
    entries: list[Entry] = field(default_factory=list)
    log: list[LogRecord] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    @property
    def unknown_size_entries(self) -> int:
        return sum(1 for e in self.entries if not e.size_known)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "total_bytes": self.total_bytes,
            "unknown_size_entries": self.unknown_size_entries,
            "entries": [e.to_dict() for e in self.entries],
            "log": [r.to_dict() for r in self.log],
        }


@dataclass
class CleanReport:
    backend: str
    dry_run: bool
    freed_bytes: int = 0
    deleted: int = 0
    failed: int = 0
    log: list[LogRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "dry_run": self.dry_run,
            "freed_bytes": self.freed_bytes,
            "deleted": self.deleted,
            "failed": self.failed,
            "log": [r.to_dict() for r in self.log],
        }


__all__ = [
    "Action",
    "Capability",
    "CleanReport",
    "CleanerSpec",
    "Entry",
    "Kind",
    "LogRecord",
    "Result",
    "Risk",
    "ScanReport",
    "Target",
]
