"""Cleaner registry.

Every cleaner must declare ``name``, ``description``, ``requires`` and ``risk``
(AGENTS.md §8). Path cleaners additionally declare ``paths``; package cleaners
declare ``kind=Kind.PACKAGE`` and are wiped through Android
(``pm clear --cache-only``) instead of by touching files.

Paths use ``<placeholder>`` tokens expanded by the engine with values supplied
from Kotlin:

    <app_files>     -> Context.getFilesDir()
    <app_cache>     -> Context.getCacheDir()
    <external>      -> Context.getExternalFilesDir(null)
    <sdcard>        -> /sdcard
    <android_data>  -> /data/data
    <android_media> -> /data/media/0

The sizes quoted in descriptions come from a real measurement on the test
device (Android 13, MIUI) and are there so the risk/reward of a cleaner is
visible in the UI without running a scan first.
"""

from __future__ import annotations

from .models import Capability, CleanerSpec, Kind, Risk, Target

APP_FILES = "<app_files>"
APP_CACHE = "<app_cache>"
EXTERNAL = "<external>"
SDCARD = "<sdcard>"
ANDROID_DATA = "<android_data>"
ANDROID_MEDIA = "<android_media>"


CLEANERS: tuple[CleanerSpec, ...] = (
    # -- non-root tier ---------------------------------------------------
    CleanerSpec(
        name="app_cache",
        description="Mezipaměť této aplikace",
        requires=(Capability.APP_DATA,),
        risk=Risk.LOW,
        paths=(
            Target(f"{APP_CACHE}/**", Capability.APP_DATA),
            Target(f"{APP_FILES}/cache/**", Capability.APP_DATA),
        ),
    ),
    CleanerSpec(
        name="thumbnails",
        description="Náhledy obrázků a videí",
        requires=(Capability.SHARED_STORAGE,),
        risk=Risk.LOW,
        paths=(
            Target(f"{ANDROID_MEDIA}/DCIM/.thumbnails/**", Capability.SHARED_STORAGE),
            Target(f"{SDCARD}/DCIM/.thumbnails/**", Capability.SHARED_STORAGE),
        ),
    ),
    CleanerSpec(
        name="external_app_cache",
        description="Cache aplikací ve sdíleném úložišti",
        requires=(Capability.SHARED_STORAGE,),
        risk=Risk.LOW,
        paths=(
            # Only the ``cache`` subdirectory. ``/sdcard/Android/data`` as a
            # whole is 9.7 GB here, but that is game/asset data in ``files/``
            # (Pokemon Unite alone is 7.8 GB), not disposable cache.
            Target(f"{ANDROID_MEDIA}/Android/data/*/cache/**", Capability.SHARED_STORAGE),
            Target(f"{SDCARD}/Android/data/*/cache/**", Capability.SHARED_STORAGE),
        ),
    ),
    CleanerSpec(
        name="shared_databases",
        description="Databáze ve sdíleném úložišti (VACUUM)",
        requires=(Capability.SHARED_STORAGE,),
        risk=Risk.LOW,
        kind=Kind.SQLITE,
        paths=(
            Target(f"{SDCARD}/**/*.db", Capability.SHARED_STORAGE),
        ),
    ),

    # -- ADB (Shizuku) tier ----------------------------------------------
    # Android itself knows what is cache; let it clear the cache instead of
    # rm -rf on /data/data/<pkg>/cache (safe against open files).
    CleanerSpec(
        name="other_app_cache",
        description="Mezipaměť ostatních aplikací (~300 MB)",
        requires=(Capability.OTHER_APP_DATA,),
        risk=Risk.MEDIUM,
        kind=Kind.PACKAGE,
    ),
    CleanerSpec(
        name="unused_app_cache",
        description="Mezipaměť aplikací nepoužitých 30+ dní",
        requires=(Capability.OTHER_APP_DATA,),
        risk=Risk.LOW,
        kind=Kind.PACKAGE,
        unused_days=30,
    ),
    CleanerSpec(
        name="own_databases",
        description="Databáze této aplikace (VACUUM)",
        requires=(Capability.APP_DATA,),
        risk=Risk.LOW,
        kind=Kind.SQLITE,
        paths=(
            Target(f"{APP_FILES}/*.db", Capability.APP_DATA),
            Target(f"{APP_FILES}/databases/*.db", Capability.APP_DATA),
        ),
    ),
    CleanerSpec(
        name="other_databases",
        description="Databáze ostatních aplikací (VACUUM)",
        requires=(Capability.OTHER_APP_DATA,),
        risk=Risk.MEDIUM,
        kind=Kind.SQLITE,
        paths=(
            Target(f"{ANDROID_DATA}/*/databases/*.db", Capability.OTHER_APP_DATA),
        ),
    ),

    # -- root tier -------------------------------------------------------
    CleanerSpec(
        name="logcat_dumps",
        description="Zachycené systémové logy a ANR (~10 MB)",
        requires=(Capability.SYSTEM,),
        risk=Risk.MEDIUM,
        paths=(
            Target("/data/anr/**", Capability.SYSTEM),
            Target("/data/tombstones/**", Capability.SYSTEM),
            Target("/data/system/dropbox/**", Capability.SYSTEM),
        ),
    ),
    # Measured: /data/dalvik-cache is ~850 MB on this device, by far the
    # largest reclaimable directory. ART rebuilds the .dex/.oat files on the
    # next app launch, so deleting them is safe; the engine never removes the
    # glob root directory itself, only its leaves.
    CleanerSpec(
        name="dalvik_cache",
        description="ART/Dalvik cache všech aplikací (~850 MB)",
        requires=(Capability.SYSTEM,),
        risk=Risk.MEDIUM,
        paths=(
            Target("/data/dalvik-cache/arm64/**", Capability.SYSTEM),
            Target("/data/dalvik-cache/arm/**", Capability.SYSTEM),
        ),
    ),
    # Per-app ``code_cache`` is tiny here (~70 kB total) but non-zero, and it
    # is the directory Android itself considers disposable.
    CleanerSpec(
        name="app_code_cache",
        description="Kódová cache jednotlivých aplikací",
        requires=(Capability.SYSTEM,),
        risk=Risk.LOW,
        paths=(
            Target(f"{ANDROID_DATA}/*/code_cache/**", Capability.SYSTEM),
        ),
    ),
    CleanerSpec(
        name="package_cache",
        description="Cache instalátoru balíčků (~15 MB)",
        requires=(Capability.SYSTEM,),
        risk=Risk.LOW,
        paths=(
            Target("/data/system/package_cache/**", Capability.SYSTEM),
        ),
    ),
    CleanerSpec(
        name="profile_snapshots",
        description="Profily ART/kompilace aplikací (~6 MB)",
        requires=(Capability.SYSTEM,),
        risk=Risk.LOW,
        paths=(
            Target("/data/misc/profiles/cur/**", Capability.SYSTEM),
            Target("/data/misc/profiles/ref/**", Capability.SYSTEM),
        ),
    ),
    # `/data/resource-cache` deliberately NOT included. It reads like a cache
    # and it is not: alongside regenerable `@idmap` files it holds *fabricated*
    # runtime resource overlays, and on the test device two of them were live —
    # `cmd overlay list` reported `[x] com.android.systemui:accent` and
    # `[x] com.android.systemui:neutral`, backed by
    # `/data/resource-cache/com.android.systemui-{accent,neutral}-*.frro`.
    # OverlayManager references those by path, so deleting one takes SystemUI's
    # colour theme with it. The whole directory was 41 kB (2 kB of it the live
    # overlays), which buys nothing against that risk.
    CleanerSpec(
        name="system_cache_dir",
        description="Systémová /cache",
        requires=(Capability.SYSTEM,),
        risk=Risk.MEDIUM,
        paths=(
            Target("/cache/**", Capability.SYSTEM),
        ),
    ),
    # /data/local/tmp is a shared scratch directory: 1.5 GB here, but other
    # tooling puts files there on purpose, so it stays HIGH risk and off by
    # default in the UI (only cleaners the user selects are run).
    CleanerSpec(
        name="staging_tmp",
        description="Soubory ve /data/local/tmp (~1,5 GB), adresáře zůstanou",
        requires=(Capability.SYSTEM,),
        risk=Risk.HIGH,
        paths=(
            # Recursive, but the engine only ever deletes *files* (list_sized
            # filters to -type f / -type l and never yields a directory), so the
            # directory tree itself survives. That matters here: the tools kept
            # in /data/local/tmp live in subdirectories (shz/, lib/, ad/), and
            # only the loose one-off files in them are disposable.
            Target("/data/local/tmp/**", Capability.SYSTEM),
        ),
    ),
    # NOTE: an earlier ``apk_leftovers`` cleaner globbed
    # ``<android_data>/*/lib/**`` and claimed to remove leftovers of
    # uninstalled apps. That pattern actually matches the native libraries of
    # *installed* apps, so running it would break every app shipping native
    # code. Leftovers need a real package-list comparison, not a glob, so the
    # cleaner is gone until that exists.
)


def all_cleaners() -> tuple[CleanerSpec, ...]:
    return CLEANERS


def by_name(name: str) -> CleanerSpec:
    for spec in CLEANERS:
        if spec.name == name:
            return spec
    raise KeyError(name)


__all__ = [
    "ANDROID_DATA",
    "ANDROID_MEDIA",
    "APP_CACHE",
    "APP_FILES",
    "CLEANERS",
    "EXTERNAL",
    "SDCARD",
    "all_cleaners",
    "by_name",
]