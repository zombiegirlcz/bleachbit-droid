"""Command line interface for the BleachBit Droid cleaning core.

Why this exists: the Android UI is a thin shell around the same functions, but
debugging on-device is slow (install, logcat, taps). The CLI exercises the
exact same code paths — ``engine.scan`` / ``engine.apply`` over a
``PrivilegeBackend`` — from a normal shell, so the logic can be developed and
verified without Android at all.

Usage (from ``app/src/main/python``)::

    python3 -m bleachbit_droid.cli detect
    python3 -m bleachbit_droid.cli cleaners --mode nonroot
    python3 -m bleachbit_droid.cli scan --mode nonroot --app-cache /tmp/c
    python3 -m bleachbit_droid.cli clean --mode nonroot --dry-run
    python3 -m bleachbit_droid.cli clean --mode adb --runner nh-adb

The three runners map to the three privilege tiers:

``local``   ``sh -c`` in this process        — non-root tier, desktop testing
``nh-adb``  ``nh adb shell sh -c``           — ADB tier over the adb bridge
``su``      ``su 0 -c``                      — root tier

Output is human readable Czech by default, ``--json`` prints the raw report the
Kotlin side would receive, ``--log`` adds one JSON line per action (AGENTS.md §8).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, Callable, Sequence

from . import engine
from .backends import AdbBackend, BackendError, NonRootBackend, RootBackend

#: One shell command must never hang the CLI forever.
TIMEOUT_SEC = 120

#: argv prefixes for the three runners; the command string is appended as one arg.
#:
#: ``nh adb shell`` already hands its argument to a shell on the device, so the
#: prefix must NOT add ``sh -c``: doing that made `nh adb shell sh -c "a && b"`
#: deliver only a fragment of the command to `sh` (the rest became positional
#: arguments), which silently produced empty output for every probe.
RUNNER_PREFIXES: dict[str, list[str]] = {
    "local": ["sh", "-c"],
    "nh-adb": ["nh", "adb", "shell"],
    "su": ["su", "0", "-c"],
}


# ---------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------


class CliShellRunner:
    """Executes one command through an argv prefix.

    The backend contract is ``run(command) -> (code, stdout, stderr)``; this
    class adapts whatever shell we have available (local ``sh``, the ``nh adb``
    bridge, ``su``) to it.
    """

    def __init__(self, prefix: Sequence[str]) -> None:
        self._prefix = list(prefix)

    def run(self, command: str) -> tuple[int, str, str]:
        argv = [*self._prefix, command]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SEC,
                check=False,
            )
        except FileNotFoundError as exc:
            return 127, "", f"{argv[0]}: {exc.strerror or exc}"
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout after {TIMEOUT_SEC}s"
        return proc.returncode, proc.stdout, proc.stderr


def make_vacuum_runner(args: argparse.Namespace) -> Callable[[str], tuple[int, str, str]] | None:
    """Build the ``VACUUM`` callback, or ``None`` when SQLite cleaners are off.

    Two shapes are supported:

    * the bundled guest toolchain (``--loader`` + ``--rootfs``) — exactly what
      the APK uses on the non-root tier, so this is the code path worth testing;
    * a plain ``sqlite3`` from ``PATH`` — convenient on a desktop.
    """
    if args.no_sqlite:
        return None

    if args.loader and args.rootfs:
        loader = os.path.abspath(args.loader)
        rootfs = os.path.abspath(args.rootfs)
        sqlite3 = os.path.join(rootfs, "usr", "bin", "sqlite3")

        def vacuum_bundled(path: str) -> tuple[int, str, str]:
            env = dict(os.environ)
            env["ELF_ROOTFS"] = rootfs
            try:
                proc = subprocess.run(
                    [loader, "--ownall", sqlite3, path, "VACUUM;"],
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SEC,
                    env=env,
                    check=False,
                )
            except FileNotFoundError as exc:
                return 127, "", f"loader: {exc.strerror or exc}"
            except subprocess.TimeoutExpired:
                return -1, "", f"timeout after {TIMEOUT_SEC}s"
            return proc.returncode, proc.stdout, proc.stderr

        return vacuum_bundled

    sqlite3 = args.sqlite3 or shutil.which("sqlite3")
    if not sqlite3:
        return None

    def vacuum_plain(path: str) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                [sqlite3, path, "VACUUM;"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SEC,
                check=False,
            )
        except FileNotFoundError as exc:
            return 127, "", f"sqlite3: {exc.strerror or exc}"
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout after {TIMEOUT_SEC}s"
        return proc.returncode, proc.stdout, proc.stderr

    return vacuum_plain


# ---------------------------------------------------------------------------
# backend + roots
# ---------------------------------------------------------------------------


def build_roots(args: argparse.Namespace) -> dict[str, str]:
    return {
        "app_files": args.app_files,
        "app_cache": args.app_cache,
        "external": args.external,
        "sdcard": args.sdcard,
        "android_data": args.android_data,
        "android_media": args.android_media,
    }


def build_backend(args: argparse.Namespace):
    """Instantiate the backend for ``--mode``.

    ``nonroot`` never uses a shell at all — only the whitelist of roots plus the
    SQLite callback. ``adb`` and ``root`` go through their runner.
    """
    roots = build_roots(args)
    vacuum = make_vacuum_runner(args)

    if args.mode == "nonroot":
        allowed = [p for p in (roots["app_files"], roots["app_cache"], roots["external"]) if p]
        return NonRootBackend(allowed, vacuum_runner=vacuum)

    if args.mode == "adb":
        runner = CliShellRunner(RUNNER_PREFIXES[args.runner])
        return AdbBackend(runner, roots["app_files"], vacuum_runner=vacuum)

    if args.mode == "root":
        runner = CliShellRunner(RUNNER_PREFIXES[args.runner])
        return RootBackend(runner, vacuum_runner=vacuum)

    raise BackendError(f"unknown mode {args.mode!r}")


def parse_cleaners(value: str | None) -> list[str] | None:
    if not value or value == "all":
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def human_bytes(size: int) -> str:
    units = ("B", "kB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1000 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}".replace(".", ",")
        value /= 1000
    return f"{int(size)} B"


def print_cleaners(items: list[dict[str, Any]]) -> None:
    for item in items:
        mark = "✓" if item["enabled"] else "✗"
        extra = f" • chybí: {', '.join(item['missing'])}" if item["missing"] else ""
        print(f"{mark} {item['name']:<18} {item['risk']:<7} {item['description']}{extra}")


def print_scan(report: dict[str, Any]) -> None:
    print(f"Nalezeno {human_bytes(report['total_bytes'])} v {len(report['entries'])} položkách")
    if not report["entries"]:
        return
    by_cleaner: dict[str, int] = {}
    for entry in report["entries"]:
        by_cleaner[entry["cleaner"]] = by_cleaner.get(entry["cleaner"], 0) + entry["size"]
    for name, size in sorted(by_cleaner.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<18} {human_bytes(size)}")


def print_clean(report: dict[str, Any]) -> None:
    if report["dry_run"]:
        print(f"Zkušební režim: uvolnilo by se {human_bytes(report['freed_bytes'])}")
    else:
        print(
            f"Uvolněno {human_bytes(report['freed_bytes'])} "
            f"({report['deleted']} souborů)"
        )
    if report["failed"]:
        print(f"  selhalo: {report['failed']}")


def print_log(report: dict[str, Any]) -> None:
    for record in report.get("log", []):
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_detect(args: argparse.Namespace) -> int:
    """Probe each tier by asking its runner for ``id``."""
    notes: dict[str, str] = {}
    available = ["nonroot"]

    for mode in ("adb", "root"):
        runner = CliShellRunner(RUNNER_PREFIXES[args.runner if mode == "adb" else "su"])
        code, out, err = runner.run("id")
        if code == 0 and "uid=" in out:
            available.append(mode)
            notes[mode] = out.strip()
        else:
            notes[mode] = f"exit {code}: {(err or out).strip() or 'no output'}"

    if args.json:
        print(json.dumps({"available": available, "notes": notes}, ensure_ascii=False))
    else:
        for mode in available:
            print(f"✓ {mode}")
        for mode, why in notes.items():
            if mode not in available:
                print(f"✗ {mode}: {why}")
    return 0


def cmd_cleaners(args: argparse.Namespace) -> int:
    backend = build_backend(args)
    items = engine.describe(backend)
    if args.json:
        print(json.dumps({"backend": backend.to_dict(), "cleaners": items}, ensure_ascii=False))
    else:
        print(f"backend {backend.name}, sqlite3={'ano' if backend.supports_sqlite else 'ne'}")
        print_cleaners(items)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    backend = build_backend(args)
    report = engine.scan(backend, build_roots(args), parse_cleaners(args.cleaners))
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print_scan(payload)
    if args.log:
        print_log(payload)
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    backend = build_backend(args)
    report = engine.apply(
        backend,
        build_roots(args),
        parse_cleaners(args.cleaners),
        dry_run=args.dry_run,
        unused_packages=parse_cleaners(args.unused_packages),
    )
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print_clean(payload)
    if args.log:
        print_log(payload)
    return 1 if payload["failed"] else 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # Device defaults so the CLI is useful both in the proot and through
    # `nh adb shell` without repeating five flags every time.
    device_files = os.environ.get(
        "BB_APP_FILES", "/data/user/0/cz.zombie.bleachbitdroid/files"
    )
    device_cache = os.environ.get(
        "BB_APP_CACHE", "/data/user/0/cz.zombie.bleachbitdroid/cache"
    )

    parser = argparse.ArgumentParser(
        prog="bleachbit-droid",
        description="BleachBit Droid cleaning core — scan and clean from a shell.",
    )
    # Shared options live on a parent parser so they may be written either
    # before or after the subcommand (`cli --json scan` and `cli scan --json`
    # both work). SUPPRESS keeps subparser defaults from overwriting values
    # already parsed before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="raw JSON output")
    common.add_argument("--log", action="store_true", help="one JSON line per action (stderr)")
    common.add_argument("--app-files", default=device_files)
    common.add_argument("--app-cache", default=device_cache)
    common.add_argument(
        "--external",
        default="/storage/emulated/0/Android/data/cz.zombie.bleachbitdroid/files",
    )
    common.add_argument("--sdcard", default="/sdcard")
    common.add_argument("--android-data", default="/data/data")
    common.add_argument("--android-media", default="/data/media/0")
    common.add_argument("--loader", help="bundled elf_loader binary")
    common.add_argument("--rootfs", help="bundled glibc rootfs directory")
    common.add_argument("--sqlite3", help="plain sqlite3 binary instead of the bundled one")
    common.add_argument(
        "--no-sqlite", action="store_true", help="pretend no sqlite3 is available"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect", parents=[common], help="which privilege tiers are usable")
    detect.add_argument("--runner", choices=sorted(RUNNER_PREFIXES), default="local")
    detect.set_defaults(func=cmd_detect)

    for name, func, help_text in (
        ("cleaners", cmd_cleaners, "list cleaners and why some are disabled"),
        ("scan", cmd_scan, "find what would be cleaned"),
        ("clean", cmd_clean, "delete (or dry-run) what was found"),
    ):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("--mode", choices=("nonroot", "adb", "root"), default="nonroot")
        p.add_argument("--runner", choices=sorted(RUNNER_PREFIXES), default="local")
        if name != "cleaners":
            p.add_argument("--cleaners", default="all", help="comma separated, or 'all'")
        if name == "clean":
            p.add_argument("--dry-run", action="store_true", help="report only, change nothing")
            p.add_argument(
                "--unused-packages", default=None, help="packages unused for >= N days"
            )
        p.set_defaults(func=func)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BackendError as exc:
        print(f"chyba: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())