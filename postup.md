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

Runnery: `local` (`sh -c`), `nh-adb` (`nh adb shell`), `su` (`su 0 -c`),
**`ashell`** (`ashell -c 'su -c …'` — root tier přímo z prootu; `su` v prootu
nefunguje, magisk daemon tam není vidět). `--no-shared-storage` simuluje
non-root tier bez all-files oprávnění.
Pro VACUUM: `--loader` + `--rootfs` (vestavěný toolchain), `--sqlite3` (systémový),
`--no-sqlite` (negativní test).

### Capability model

`app_data` < `shared_storage` < `other_app_data` < `system`

| backend | capabilities | `supports_packages` | VACUUM (`vacuum_capabilities`) |
|---|---|---|---|
| `nonroot` | app_data (+ shared_storage **jen s all-files**) | ne | app_data, shared_storage |
| `adb` | + other_app_data | ano | app_data, shared_storage |
| `root` | vše | ano | app_data, shared_storage |

Cleaner se spustí jen když `requires ⊆ backend.capabilities`
(a u `kind=package` navíc `supports_packages`, u `kind=sqlite`
`backend.can_vacuum(requires)`).

`shared_storage` na non-root tieru se odvozuje z `all_files` v payloadu
(`Environment.isExternalStorageManager()`), protože bez toho oprávnění proces
`/sdcard` vyjmenuje, ale nic v něm nesmaže.

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
| `other_databases` | sqlite | other_app_data | medium | **nenabízí se** (viz níže) |
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

**Aktuální stav: 128 passed** (Python) + 7 JVM testů `ShellSessionTest`
(`gradle :app:testDebugUnitTest`, běží v CI — lokálně gradle nejde).

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

## Opravy z analýzy funkčnosti (2026-10-27)

Všechno níže má regresní test; co šlo, je ověřené na zařízení přes
`--runner ashell`.

| co bylo rozbité | oprava | doklad |
|---|---|---|
| non-root tier nesmazal **nic** ve sdíleném úložišti (scan našel, apply spadl „outside allowed roots“ na každém souboru) | `shared_storage` se odvozuje z `all_files`; s oprávněním jsou `/sdcard` a `/data/media/0` ve whitelistu | `test_nonroot_*_shared_storage`; CLI: 2 soubory smazány, 0 failed |
| apply = 4 shell příkazy na soubor | `delete_many()`, dávky po 200 | 20 souborů **82 → 3** příkazy; na zařízení 41 souborů = **1** `rm` |
| `ShellSession.close()` nevratné → první timeout zabil tier do restartu | latch `closed` zrušen | `ShellSessionTest` (JVM) |
| `other_databases` se nabízel, ale VACUUM cizí db nemůže uspět | `vacuum_capabilities`, cleaner vypnut s důvodem | `test_foreign_databases_are_not_offered_on_any_tier` |
| nulové soubory se ohlásily a nikdy nesmazaly | `size == 0` guard pryč | `test_zero_length_files_are_scanned_and_deleted` |
| „Nalezeno N (0 B)“ a „Uvolněno 0 B“ | `ProgressSink` čte `bytes`/`freed_bytes`/`unknown` | `test_done_event_carries_what_was_freed` |
| `cleaner_done` hlásil kumulativní součet jako podíl cleaneru | `bytes` = podíl cleaneru | `test_cleaner_done_reports_only_its_own_share` |
| apply nehlásil průběh během scanu (nejdelší fáze) | `on_event` se předává do `scan()` | `test_apply_reports_the_scan_it_runs` |
| ADB tier hlásil `0 B` po úspěšném `pm clear` | `Stats.size_known`, `CleanReport.unknown`, „+ N položek neznámé velikosti“ | `test_package_clear_without_a_measurable_size_is_reported_as_unknown` |
| `unused_app_cache` prakticky nikdy nic nevrátil (dotaz jen do okna N dní) | všechny retained intervaly od 0, chybějící záznam = nepoužito, jen spustitelné appky | — (vyžaduje zařízení s usage access) |
| `RootBackend` obaloval každý příkaz `su -c` uvnitř root shellu | runner je root, příkazy jdou nezabalené | `test_root_backend_sends_plain_commands_to_its_runner`; na zařízení ověřeno |
| VACUUM obcházel whitelist non-root tieru | `NonRootBackend.vacuum_db` guarduje | `test_nonroot_vacuum_respects_the_whitelist` |
| `missing` se uživateli zobrazoval jako `other_app_data, shell` | `missing_labels` v češtině | CLI: „chybí: Data jiných aplikací, přístup k shellu“ |
| „Vyčistit“ mazalo bez potvrzení | `AlertDialog` s počtem čističů, nálezem a HIGH-risk varováním | — |
| `detectModes()`+`reload()` běžely 2× při startu a pozdní reload přepsal výběr | jednou; `reload(resetSelection)` | — |
| manifest žádal FGS / notifikace / wake lock / boot, které nic nepoužívá | oprávnění pryč, vrátí se se službou | manifest |
| `.gitignore` měl slepenou řádku `ktlintapkmanifest.xml`, `apkmanifest.xml` byl commitnutý | řádka opravena, soubor odtrackován | `git check-ignore` |

### Ověřeno na zařízení (root tier, `--runner ashell`, 2026-10-27)

- `detect` → `uid=0(root) … context=u:r:magisk:s0`
- scan `dalvik_cache,package_cache,profile_snapshots,logcat_dumps` →
  **901,1 MB v 2031 položkách** za 5,3 s (`du -sb /data/dalvik-cache`
  = 871 419 576 B, souhlasí)
- dry-run `package_cache,profile_snapshots` → 17,6 MB
- reálné mazání na vlastním stromu `/data/local/tmp/bb-selftest`: 41 souborů
  (včetně nulového) **jedním** `rm`, 0 selhání, adresář i vlastník zůstali
- VACUUM přes engine: 3,3 MB → 8,2 kB, `integrity_check = ok`, freelist 0
- toybox 0.8.6: `du -b`, `stat -c %u`, `stat -c '%s<TAB>%n'` s `-exec +`,
  `find -path`, `find -mindepth 1 -delete` — vše funguje
- `/system/bin/sqlite3` na zařízení **není** (ani v xbin, ani v apexu)

## Známé limity / TODO

1. APK se staví jen v CI; artefakt `bleachbit-droid-debug`.
2. `local.properties` a `apkmanifest.xml` jsou v `.gitignore` (řádka opravena,
   `apkmanifest.xml` odtrackován).
3. **Foreground service pro apply stále neexistuje** — a manifest ho už ani
   nepředstírá (oprávnění odstraněna). Dlouhá čistka na pozadí může být zabita
   (MIUI hibernace to reálně dělá). Služba + notifikace = další krok.
4. Usage stats vyžadují ruční povolení; bez něj se `unused_app_cache` přeskočí.
   Nová logika (všechny intervaly) není ověřená na zařízení s povoleným
   usage access.
5. All-files-access vyžaduje ruční povolení — bez něj non-root tier nabízí
   jen `app_cache` a `own_databases`, a říká proč.
6. Shizuku manager na testovacím zařízení není nainstalovaný; běží jen
   `shizuku_server` (compat server). ADB tier funguje, `pm clear` ověřen zvlášť.
7. `staging_tmp` je HIGH risk, ve výchozím výběru není a potvrzovací dialog ho
   vypíše zvlášť.
8. Chybí: notifikace o průběhu, golden reference z fáze 0, skutečné zbytky
   po odinstalovaných appkách (chce porovnání se seznamem balíčků),
   ikona aplikace (`bb.png` není v `res/`), VACUUM cizích databází (chtělo by
   `sqlite3` spustitelný pod uid vlastníka).
9. Kotlin kód se lokálně nedá zkompilovat (aapt2 x86-64, chybí truststore) —
   změny v Kotlinu ověří až CI.

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