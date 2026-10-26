"""Recursive cleaners must delete files but keep the directory tree.

`/data/local/tmp` on Android is not a tmpfs and is not wiped on reboot — it is
an ordinary directory on `/data` that accumulates whatever tools and one-off
files were put there. Measured on the test device: 75 root-owned and 41
shell-owned files, **zero** app-owned, plus subdirectories that hold actual
tools (`shz/`, `lib/`, `ad/`) and a dead AdGuard tmpfs mount (`adg-ca-copy/`).

The `staging_tmp` cleaner is recursive on purpose (loose junk sits everywhere,
including inside those tool directories) but must only ever remove *files*.
Removing an empty directory would take `lib/arm64/` or `shz/` with it, which is
how you break tooling that lives there.

Regression: `PrivilegeBackend.list_sized()` passed empty directories through on
the local backend (`_iter_leaves()` returns them so a clean can tidy up after
itself), while the shell backend filtered them with `-type f -o -type l`. The
two backends therefore disagreed about what "deletable" means, and a local
scan offered directories as entries.

`staging_tmp` uses a literal absolute pattern, so it cannot be pointed at a
fixture tree. The behaviour is therefore pinned where it lives: on
`list_sized()` and on `engine` running a placeholder-based cleaner over the
same kind of tree.

Run from the ``app`` directory:

    python3 -m pytest src/test/python -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PY_ROOT = HERE.parents[2] / "main" / "python"
sys.path.insert(0, str(PY_ROOT))

from bleachbit_droid import engine  # noqa: E402
from bleachbit_droid.backends import NonRootBackend  # noqa: E402
from bleachbit_droid.cleaners import by_name  # noqa: E402
from bleachbit_droid.models import Risk  # noqa: E402


@pytest.fixture()
def tmp_tree(tmp_path: Path) -> Path:
    """Mimics /data/local/tmp: loose junk at the top, tools in subdirs."""
    (tmp_path / "loose.apk").write_bytes(b"x" * 100)
    (tmp_path / "bb.png").write_bytes(b"p" * 50)

    tool = tmp_path / "shz"
    tool.mkdir()
    (tool / "server.bin").write_bytes(b"y" * 200)

    lib = tmp_path / "lib" / "arm64"
    lib.mkdir(parents=True)
    (lib / "libshizuku.so").write_bytes(b"z" * 300)

    # An empty directory: the case that used to leak through as an entry.
    (tmp_path / "bbt").mkdir()

    return tmp_path


# ---------------------------------------------------------------------------
# the cleaner declaration
# ---------------------------------------------------------------------------


def test_staging_tmp_is_high_risk_and_recursive():
    spec = by_name("staging_tmp")
    assert spec.risk is Risk.HIGH
    assert [p.pattern for p in spec.paths] == ["/data/local/tmp/**"]


# ---------------------------------------------------------------------------
# list_sized: files only, directories never (the regression)
# ---------------------------------------------------------------------------


def test_list_sized_never_returns_a_directory(tmp_tree: Path):
    backend = NonRootBackend([str(tmp_tree)])

    sized = dict(backend.list_sized(f"{tmp_tree}/**"))

    paths = set(sized)
    assert str(tmp_tree / "loose.apk") in paths
    assert str(tmp_tree / "bb.png") in paths
    assert str(tmp_tree / "shz" / "server.bin") in paths
    assert str(tmp_tree / "lib" / "arm64" / "libshizuku.so") in paths

    # Not even the empty directory may appear: `lib/arm64/` and `shz/` are the
    # tools' home and a recursive clean would delete them with it.
    assert str(tmp_tree / "bbt") not in paths
    assert str(tmp_tree / "shz") not in paths
    assert str(tmp_tree / "lib" / "arm64") not in paths

    for path in paths:
        assert not Path(path).is_dir(), f"directory returned by list_sized: {path}"


def test_list_sized_reports_real_sizes(tmp_tree: Path):
    backend = NonRootBackend([str(tmp_tree)])
    sized = dict(backend.list_sized(f"{tmp_tree}/**"))
    assert sized[str(tmp_tree / "loose.apk")] == 100
    assert sized[str(tmp_tree / "shz" / "server.bin")] == 200


def test_leaves_still_returns_empty_dirs_for_tidying(tmp_tree: Path):
    """`leaves()` keeps its contract — only `list_sized()` filters.

    `delete()` uses `leaves()` to count what went away, and after a clean the
    empty directories are legitimately part of that count. Filtering in
    `leaves()` instead would silently change `Stats.files`.
    """
    backend = NonRootBackend([str(tmp_tree)])
    leaves = set(backend.leaves(str(tmp_tree)))
    assert str(tmp_tree / "bbt") in leaves


# ---------------------------------------------------------------------------
# engine over a placeholder-based cleaner: directories survive the apply
# ---------------------------------------------------------------------------


def test_apply_keeps_directories(tmp_tree: Path):
    """`app_cache` is `<app_cache>/**`, i.e. the same recursive shape."""
    roots = {
        "app_files": str(tmp_tree),
        "app_cache": str(tmp_tree),
        "external": str(tmp_tree),
        "sdcard": str(tmp_tree),
        "android_data": str(tmp_tree),
        "android_media": str(tmp_tree),
    }
    backend = NonRootBackend([str(tmp_tree)])

    report = engine.apply(backend, roots, ["app_cache"], dry_run=False)

    assert report.freed_bytes > 0
    # files gone
    assert not (tmp_tree / "loose.apk").exists()
    assert not (tmp_tree / "shz" / "server.bin").exists()
    assert not (tmp_tree / "lib" / "arm64" / "libshizuku.so").exists()
    # directories still there
    assert (tmp_tree / "shz").is_dir()
    assert (tmp_tree / "lib" / "arm64").is_dir()
    assert (tmp_tree / "bbt").is_dir()


def test_dry_run_touches_nothing(tmp_tree: Path):
    roots = {
        "app_files": str(tmp_tree),
        "app_cache": str(tmp_tree),
        "external": str(tmp_tree),
        "sdcard": str(tmp_tree),
        "android_data": str(tmp_tree),
        "android_media": str(tmp_tree),
    }
    backend = NonRootBackend([str(tmp_tree)])

    report = engine.apply(backend, roots, ["app_cache"], dry_run=True)

    assert report.freed_bytes > 0
    assert (tmp_tree / "loose.apk").exists()
    assert (tmp_tree / "shz" / "server.bin").exists()


def test_scan_never_offers_a_directory(tmp_tree: Path):
    roots = {
        "app_files": str(tmp_tree),
        "app_cache": str(tmp_tree),
        "external": str(tmp_tree),
        "sdcard": str(tmp_tree),
        "android_data": str(tmp_tree),
        "android_media": str(tmp_tree),
    }
    backend = NonRootBackend([str(tmp_tree)])

    report = engine.scan(backend, roots, ["app_cache"])

    for entry in report.entries:
        assert entry.is_dir is False, f"directory offered as entry: {entry.path}"
