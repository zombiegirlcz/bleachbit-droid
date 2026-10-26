# postup.md — aktuální stav projektu

Autoritativní zdroj stavu. Aktualizuj po každé změně.

## Co projekt je

BleachBit pro Android se třemi úrovněmi oprávnění: **non-root**, **ADB (Shizuku)**, **root**.
Teze: čím vyšší oprávnění, tím hlubší čistka (capability gating).

## Stack (rozhodnuto)

- Kotlin + Jetpack Compose (UI) — `app/src/main/java/cz/zombie/bleachbitdroid/`
- **Vestavěný glibc toolchain** (elf_loader) — non-root tier umí `shred`, `sqlite3`, `find`
- Python core přes **Chaquopy 17.1.0**, Python 3.13 — `app/src/main/python/bleachbit_droid/`
- Gradle 8.14.3, AGP 8.13.2, Kotlin 2.2.21, JDK 21, **compileSdk 36**, minSdk 26
- Shizuku API 13.1.5 (`dev.rikka.shizuku:api`, `:provider`)
- Build běží na GitHub Actions (`.github/workflows/build.yml`) — lokální build na zařízení
  je problematický (aapt2 z Google je x86-64 only, chybí Java truststore)

## Struktura Python core

| modul | obsah |
|---|---|
| `models.py` | `Capability`, `Risk`, `Kind{PATH,PACKAGE,SQLITE}`, `Target`, `CleanerSpec`, `Entry`, `LogRecord`, `ScanReport`, `CleanReport` |
| `capabilities.py` | pořadí capability, české labely, `missing()` |
| `backends.py` | `PrivilegeBackend` (ABC), `NonRootBackend`, `AdbBackend`, `RootBackend`, `_ShellMixin`, `_SqliteMixin`, `Stats`, `_find_base_and_glob`, `shell_quote`, `_validate_package` |
| `cleaners.py` | registry cleanerů (path / package / sqlite) |
| `engine.py` | expanze `<placeholder>`, `scan()`, `apply()`, `describe()`, `on_event` progress |
| `api.py` | JSON-in/JSON-out pro Kotlin + `_streaming` (callback jako samostatný argument) |
| `cli.py` | **CLI nástroj** — `detect`, `cleaners`, `scan`, `clean`; `--mode`, `--runner`, `--json`, `--log` |

### CLI — hlavní vývojová cesta

```bash
cd app/src/main/python
python3 -m bleachbit_droid.cli cleaners --mode nonroot
python3 -m bleachbit_droid.cli scan   --mode adb --runner nh-adb --cleaners app_cache
python3 -m bleachbit_droid.cli clean  --mode root --runner su --dry-run
```

Runnery: `local` (`sh -c`), `nh-adb` (`nh adb shell`), `su` (`su 0 -c`).
Pro VACUUM: `--loader` + `--rootfs` (vestavěný toolchain), `--sqlite3` (systémový),
`--no-sqlite` (negativní test).

### Capability model

`app_data` < `shared_storage` < `other_app_data` < `system`

| backend | capabilities | `supports_packages` | `supports_sqlite` |
|---|---|---|---|
| `nonroot` | app_data, shared_storage | ne | ano (když je injectnut runner) |
| `adb` | + other_app_data | ano | ano |
| `root` | vše | ano | ano |

Cleaner se spustí jen když `requires ⊆ backend.capabilities`
(a u `kind=package` navíc `supports_packages`, u `kind=sqlite` `supports_sqlite`).

### Tři druhy cleanerů

- **`kind=path`** — glob vzory; enumerace a mazání přes backend
  (`expand`, `leaves`, `list_sized`, `is_dir`, `size`, `delete`, `measure`).
- **`kind=package`** — `pm clear --cache-only <pkg>` (+ `am force-stop`).
  `unused_days=N` → seznam balíčků dodá `UsageStatsProvider`; bez seznamu skip.
  **Root tier tuto cestu nepoužívá** (viz níže).
- **`kind=sqlite`** — `VACUUM` přes vestavěný glibc `sqlite3`.

### Enumerace cest patří backendu, ne enginu

`engine` **nesmí** volat `glob.glob`/`os.walk` — na ADB a root tieru tenhle proces
cesty nevidí a scan by vždy hlásil 0 B. Backend odpovídá ve svém namespace:

- `PrivilegeBackend.expand/leaves/is_dir/is_file/size` — lokální syscally
- `_ShellMixin` — přes `find` / `stat` / `test` ve shellu
- `list_sized(pattern)` — **jeden** `find … -exec stat -c '%s<TAB>%n' {} +`
  pro celý target (naměřeno 2000 souborů: 1,3 s vs ~68 s per-file)

### Vestavěný glibc toolchain (elf_loader)

| co | kde | proč tam |
|---|---|---|
| `libelf_loader.so` | `jniLibs/arm64-v8a/` | bionic; Android ho extrahuje do `nativeLibraryDir` a dovolí `execve` |
| `usr/bin/{shred,sqlite3,find}` | `assets/rootfs/` | 6,7 MB; loader je jen `mmap`uje |
| 12 glibc knihoven | `assets/rootfs/usr/lib/aarch64-linux-gnu/` | musí zůstat původní sonames (`libc.so.6`) |

`GuestTool` spouští nástroje s `ELF_ROOTFS`; `GuestSelfTest` při startu ověří
všechny tři → logcat. `VacuumRunner` dává Pythonu `VACUUM` callback.

**Ověřeno na zařízení (APK, app uid 10316, 2026-10-26):**
- `GuestSelfTest: shred rc=0 / sqlite rc=0 / find rc=0 / result=OK`
- **VACUUM: 3 088 384 → 1 032 192 B**, freelist 502 → 0, `integrity_check = ok`,
  ownership zůstal `10316:10316`
- `detect -> {"available": ["nonroot", "root", "adb"]}`

### Persistentní shell (výkon)

`ShellSession` (Kotlin) drží **jeden** `su`/Shizuku proces na tier a posílá mu
příkazy na stdin, oddělené markerem:

```
{ <command>
} 2>&1; echo "__BB_END_<n>:$?"
```

Dřív se na každý příkaz spouštěl nový `su` → Magisk prompt na každý `test -e`
a zahlcená obrazovka povolenin. Protokol ověřen proti reálnému `sh`
(exit kódy 0/1/2, stdout+stderr sloučené, taby zachované).

`ShizukuRunner` používá `CountDownLatch` + blokující `waitFor()`:
`Process.waitFor(timeout, unit)` polluje `exitValue()`, a
`ShizukuRemoteProcess.exitValue()` hází `IllegalArgumentException:
process hasn't exited` — tím dřív selhával každý Shizuku příkaz.

### Progress

`engine.scan()` i `engine.apply()` berou `on_event: Callable[[dict], None]`.
Události: `scan_start`, `cleaner_start`, `found`, `cleaner_done`, `scan_done`;
u apply `start`, `scanned`, `would_delete`, `deleted`, `skip`, `failed`, `done`.

`api.scan_streaming` / `apply_clean_streaming` berou callback jako **druhý
argument** (ne v JSON payloadu) a `_as_callback()` hledá **nejdřív `onEvent`**,
protože Chaquopy proxy je sice `callable()`, ale zavolání `onEvent` nezasáhne.

UI: `ProgressSink` (Kotlin) → `MainViewModel.pushProgress()` → `ProgressPanel`
v `CleanerScreen` (monospace, auto-scroll na konec, max 200 řádků).

### Cleanery (stav po rozšíření z reálného měření)

| name | kind | requires | risk | naměřeno |
|---|---|---|---|---|
| `app_cache` | path | app_data | low | — |
| `thumbnails` | path | shared_storage | low | — |
| `external_app_cache` | path | shared_storage | low | ~270 MB |
| `shared_databases` | sqlite | shared_storage | low | — |
| `other_app_cache` | package | other_app_data | medium | ~300 MB |
| `unused_app_cache` | package (30 dní) | other_app_data | low | — |
| `own_databases` | sqlite | app_data | low | — |
| `other_databases` | sqlite | other_app_data | medium | ~14 MB |
| `logcat_dumps` | path | system | medium | ~10 MB |
| `dalvik_cache` | path (`/data/dalvik-cache/{arm64,arm}`) | system | medium | **851 MB** |
| `app_code_cache` | path (`*/code_cache`) | system | low | ~70 kB |
| `package_cache` | path | system | low | 15 MB |
| `profile_snapshots` | path | system | low | ~13 MB |
| `system_cache_dir` | path (`/cache`, `resource-cache`) | system | medium | ~6 MB |
| `staging_tmp` | path (`/data/local/tmp`) | system | **high** | **1,5 GB** |

**Zrušeno `apk_leftovers`** — globoval `<android_data>/*/lib/**`, což jsou
nativní knihovny *nainstalovaných* aplikací. Spuštění by rozbilo každou appku
s nativním kódem. Skutečné zbytky potřebují porovnání se seznamem balíčků.

### Root tier maže cache pod UID aplikace

`RootBackend.clear_app_cache()`:
1. přečte uid z `stat -c %u /data/data/<pkg>`
2. změří `du -sk .../cache`
3. `am force-stop <pkg>` (selhání není fatální)
4. `su <uid> -c 'find <cache> -mindepth 1 -delete'`

Nikdy `chown`, nikdy `pm clear` na tomto tieru (to smaže i bookkeeping
package manageru). Soubor vytvořený rootem v app datech = rozbitá appka.

## UI

- `MainActivity` — Compose, Shizuku permission při startu, `onResume` → `refreshPermissions()`
- `CleanerScreen` — chips s úrovněmi, tlačítka all-files/usage access, seznam
  cleanerů, **progress panel**, Prohledat / Zkusmo / Vyčistit
- `MainViewModel` — `UiState` (phase, mode, cleaners, selected, scan, clean,
  permissions, **progress**, **progressTitle**), `pushProgress()`
- `CleanerRepository` — parsuje JSON, předává `onProgress`
- `PythonBridge` — Chaquopy; `call(name, payload, extra)` pro callback
- `ProgressSink` — `onEvent(json)` → `ProgressEvent.render()` (česky)
- `ShellSession` — persistentní shell, marker protokol
- `ShizukuRunner` / `RootRunner` — oba nad `ShellSession`
- `RootfsInstaller`, `GuestTool`, `GuestSelfTest`, `VacuumRunner`
- `UsageStatsProvider` — `AppOpsManager` + `queryUsageStats`

**Pořadí v `init`:** `registerVacuumRunner()` **před** prvním `reload()` —
`supports_sqlite` je odvozený z injectnutého runneru, jinak UI hlásí
„chybí: sqlite3“.

## Testy

```bash
cd app && python3 -m pytest src/test/python -q
```

Soubory: `test_core.py`, `test_enumeration.py`, `test_progress.py`.

**Aktuální stav: 107 passed, 1 skipped.**

Pokrývají mimo jiné:
- metadata cleanerů, capability gating, guardy backendů, package-name validace
- scan/apply na fixture stromu, dry-run vs apply, JSON log lines
- `_find_base_and_glob`, shellovou enumeraci (`FakeShell`), **TAB ve `stat` formátu**
- bulk `list_sized` = 1 příkaz na target
- `_as_callback` s proxy, která **je** `callable()` a přesto má `onEvent`
- callback jako druhý argument, rostoucí `total_bytes`
- root `clear_app_cache` běží pod uid appky a nikdy nechownuje
- `RootBackend` dědí `_SqliteMixin` a `api.py` mu předává vacuum runner

## Ověřování na zařízení (postup)

- ADB bridge: `nh adb start` (nebo `adb connect <host>:<port>`), pak `nh adb shell id`
- Nasazení: `cp app-debug.apk /sdcard/Download/` → `nh adb install /sdcard/Download/x.apk`
  (`nh adb push` nefunguje — server pod uid 2000 cesty z prootu nevidí)
- Start + tap + dump **v jednom on-device řetězu**, jinak terminál v prootu
  ukradne foreground a dump je prázdný
- UI: `nh dev acc --json | python3 /root/acc.py`
- Logcat: `nh adb logcat -d --pid=$(pidof cz.zombie.bleachbitdroid)`
- `uiautomator dump` dává Compose textům nulové bounds → používat klikatelné parenty
- `dd` v `nh adb shell` tiše selhává → `head -c N /dev/zero > soubor`

## Známé limity / TODO

1. APK se staví jen v CI; artefakt `bleachbit-droid-debug`.
2. `local.properties` a `apkmanifest.xml` jsou v `.gitignore`.
3. **Foreground service pro apply neexistuje** — manifest ho deklaruje, ale
   služba není. Dlouhá čistka na pozadí může být zabita (MIUI hibernace to
   reálně dělá).
4. Usage stats vyžadují ruční povolení; bez něj se `unused_app_cache` přeskočí.
5. All-files-access vyžaduje ruční povolení.
6. Shizuku manager na testovacím zařízení není nainstalovaný; běží jen
   `shizuku_server` (compat server). ADB tier funguje, `pm clear` ověřen zvlášť.
7. `staging_tmp` je HIGH risk a ve výchozím výběru být nemá.
8. Chybí: notifikace o průběhu, golden reference z fáze 0, skutečné zbytky
   po odinstalovaných appkách (chce porovnání se seznamem balíčků).

## Historie

- `ca487c8` init (jen AGENTS.md)
- `fa25977` Android skeleton: Chaquopy core, backendy, Compose UI, Shizuku, CI
- `a54ba12` postup.md
- `92f44fe` CI: pryč ze setup-android action
- `0402e8e` compileSdk 36, core-ktx 1.17.0
- `ee53ca1` oprava Kotlin chyb (JSONObject→String, reflexe Shizuku, signatury)
- `aee08c1` manifest: storage, FGS, notifikace, usage stats
- `4cfbab4` vestavěný glibc toolchain (loader + shred/sqlite3/find)
- `8d6e462` postup.md: toolchain
- `b08a66b` sqlite cleanery (VACUUM)
- `ea955ee` fix: Chaquopy proxy není callable (vacuum runner)
- `f8de82f` CLI + enumerace cest přes backendy; `nh adb shell` už sám wrappuje shell
- `31881fa` fix: Shizuku `waitFor` (exitValue hází), VACUUM runner registrován včas
- `76caafb` fix: root tier dostal `_SqliteMixin` + vacuum runner
- `42e6826` perf: persistentní shell, bulk scan, live progress
- `0ab5072` fix: skutečný TAB ve `stat` formátu
- `57fa77a` fix: callback jako samostatný Chaquopy argument
- `100b93d` feat: reálné cíle čištění, root maže cache pod uid appky
- `a66b2d6` fix: `onEvent` má přednost před `__call__` (progress konečně funguje)