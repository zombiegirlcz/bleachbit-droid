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
            Target(f"{SDCARD}/Android/data/*/cache/**", Capability.SHARED_STORAGE),
        ),
    ),

    # -- ADB (Shizuku) tier ----------------------------------------------
    # Android itself knows what is cache; let it clear the cache instead of
    # rm -rf on /data/data/<pkg>/cache (safe against open files).
    CleanerSpec(
        name="other_app_cache",
        description="Mezipaměť ostatních aplikací",
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

    # -- root tier -------------------------------------------------------
    CleanerSpec(
        name="logcat_dumps",
        description="Zachycené systémové logy a ANR",
        requires=(Capability.SYSTEM,),
        risk=Risk.MEDIUM,
        paths=(
            Target("/data/anr/**", Capability.SYSTEM),
            Target("/data/tombstones/**", Capability.SYSTEM),
            Target("/data/system/dropbox/**", Capability.SYSTEM),
        ),
    ),
    CleanerSpec(
        name="dalvik_cache",
        description="ART/Dalvik cache všech aplikací",
        requires=(Capability.SYSTEM,),
        risk=Risk.MEDIUM,
        paths=(
            Target(f"{ANDROID_DATA}/*/code_cache/**", Capability.SYSTEM),
        ),
    ),
    CleanerSpec(
        name="apk_leftovers",
        description="Zbytky po odinstalovaných aplikacích",
        requires=(Capability.SYSTEM,),
        risk=Risk.HIGH,
        paths=(
            Target(f"{ANDROID_DATA}/*/lib/**", Capability.SYSTEM),
        ),
    ),

    # -- database tier (bundled glibc sqlite3) ---------------------------
    # DELETE only frees pages inside the file; VACUUM rewrites it so the bytes
    # actually leave the disk. No Android tool can do this.
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
