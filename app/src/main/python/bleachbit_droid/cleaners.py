"""Cleaner registry.

Every cleaner must declare ``name``, ``description``, ``requires``, ``paths``
and ``risk`` (AGENTS.md §8). Paths use ``<placeholder>`` tokens that are
expanded by the engine with values supplied from Kotlin:

    <app_files>     -> Context.getFilesDir()
    <app_cache>     -> Context.getCacheDir()
    <external>      -> Context.getExternalFilesDir(null)
    <sdcard>        -> /sdcard
    <android_data>  -> /data/data
    <android_media> -> /data/media/0
"""

from __future__ import annotations

from .models import Capability, CleanerSpec, Risk, Target

APP_FILES = "<app_files>"
APP_CACHE = "<app_cache>"
EXTERNAL = "<external>"
SDCARD = "<sdcard>"
ANDROID_DATA = "<android_data>"
ANDROID_MEDIA = "<android_media>"


CLEANERS: tuple[CleanerSpec, ...] = (
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
        name="logcat_dumps",
        description="Zachycené systemové logy",
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
        description="Dalvik/ART cache jiných aplikací",
        requires=(Capability.OTHER_APP_DATA, Capability.APP_DATA),
        risk=Risk.MEDIUM,
        paths=(
            Target(f"{ANDROID_DATA}/*/cache/**", Capability.OTHER_APP_DATA),
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