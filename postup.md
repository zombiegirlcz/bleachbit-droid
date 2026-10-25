# postup.md — aktuální stav projektu

Autoritativní zdroj stavu. Aktualizuj po každé změně.

## Co projekt je

BleachBit pro Android se třemi úrovněmi oprávnění: **non-root**, **ADB (Shizuku)**, **root**.
Teze: čím vyšší oprávnění, tím hlubší čistka (capability gating).

## Stack (rozhodnuto)

- Kotlin + Jetpack Compose (UI) — `app/src/main/java/cz/zombie/bleachbitdroid/`
- Python core přes **Chaquopy 17.1.0**, Python 3.13 — `app/src/main/python/bleachbit_droid/`
- Gradle 8.14.3, AGP 8.13.2, Kotlin 2.2.21, JDK 21, **compileSdk 36**, minSdk 26
- Shizuku API 13.1.5 (`dev.rikka.shizuku:api`, `:provider`)
- Build běží na GitHub Actions (`.github/workflows/build.yml`) — lokální build na zařízení
  je problematický (aapt2 z Google je x86-64 only, chybí Java truststore)

## Struktura Python core

| modul | obsah |
|---|---|
| `models.py` | `Capability`, `Risk`, `Kind`, `Target`, `CleanerSpec`, `Entry`, `LogRecord`, `ScanReport`, `CleanReport` |
| `capabilities.py` | pořadí capability, české labely, `missing()` |
| `backends.py` | `PrivilegeBackend` (ABC), `NonRootBackend`, `AdbBackend`, `RootBackend`, `_ShellMixin`, `shell_quote`, `_validate_package` |
| `cleaners.py` | registry cleanerů s `name/description/requires/risk` + `kind` (`path`/`package`) |
| `engine.py` | expanze `<placeholder>` tokenů, `scan()`, `apply()`, `describe()`, package cleanery |
| `api.py` | JSON-in/JSON-out entry pointy pro Kotlin (`detect`, `list_cleaners`, `scan`, `apply_clean`, `set_adb_runner`, `set_root_runner`) |

### Capability model

`app_data` < `shared_storage` < `other_app_data` < `system`

- non-root backend: `app_data`, `shared_storage` (guard: jen povolené rooty), `supports_packages=False`
- adb (Shizuku): + `other_app_data`, `supports_packages=True` (přes `sh`)
- root: vše, `supports_packages=True` (přes `su -c`)

Cleaner se spustí jen když `requires ⊆ backend.capabilities`
(a u `kind=package` navíc `backend.supports_packages`).

### Dva druhy cleanerů

- **`kind=path`** — glob vzory, měření a mazání přes `backend.measure()/delete()`
- **`kind=package`** — nemazeme soubory, ale voláme `pm clear --cache-only <pkg>`
  (Android sám ví, co je cache; nedojde k rozbití otevřených souborů).
  Volitelně předtím `am force-stop <pkg>`.
  `unused_days=N` znamená, že seznam balíčků dodá Kotlin (`UsageStatsProvider`);
  bez seznamu se cleaner přeskočí (radši nic než mazat špatné appky).

### Bezpečnostní invarianty (držet!)

- Žádná mutace filesystemu mimo `PrivilegeBackend.delete()`.
- `NonRootBackend` má whitelist rootů (`filesDir`, `cacheDir`, `externalFilesDir`);
  prázdný seznam = vše denied.
- `AdbBackend` odmítá relativní cesty a `..`.
- `_validate_package()` odmítá cokoli mimo `[A-Za-z0-9._]` a musí začínat písmenem
  → žádná shell injection přes jméno balíčku.
- `RootBackend` bez `su` runneru funguje jen když `os.geteuid() == 0`.
- Engine nikdy nemaže kořenovou složku globu (např. `cache/`), jen její obsah.
- Overlapping globy: každá cesta se reportuje jen jednou (`seen` set).

## Cleanery

| name | kind | requires | risk |
|---|---|---|---|
| `app_cache` | path | app_data | low |
| `thumbnails` | path | shared_storage | low |
| `other_app_cache` | package | other_app_data | medium |
| `unused_app_cache` | package (30 dní) | other_app_data | low |
| `logcat_dumps` | path | system | medium |
| `dalvik_cache` | path (`code_cache`) | system | medium |
| `apk_leftovers` | path | system | high |

## Android manifest (oprávnění)

Deklarováno: `QUERY_ALL_PACKAGES`, `moe.shizuku.manager.permission.API_V23`,
`MANAGE_EXTERNAL_STORAGE` (+ legacy READ/WRITE do API 32), `FOREGROUND_SERVICE`,
`FOREGROUND_SERVICE_DATA_SYNC`, `POST_NOTIFICATIONS`, `WAKE_LOCK`,
`RECEIVE_BOOT_COMPLETED`, `PACKAGE_USAGE_STATS`. Provider `ShizukuProvider`.

Co Shizuku (uid 2000) reálně umí, i když to appka deklarovat nemůže:
`FORCE_STOP_PACKAGES`, `KILL_UID`, `CHANGE_APP_IDLE_STATE`, `CLEAR_APP_CACHE`
(přes privapp allowlist), `OBSERVE_APP_USAGE`, `GET_APP_OPS_STATS`,
`MANAGE_ACTIVITY_TASKS`, `READ_LOGS`, `DUMP`, `MANAGE_DEBUGGING`,
`ACCESS_CONTENT_PROVIDERS_EXTERNALLY`, `READ_CLIPBOARD_IN_BACKGROUND`.
Root-only navíc: `WRITE_MEDIA_STORAGE`, `MOUNT_*_FILESYSTEMS`.

## UI

- `MainActivity` — Compose, Shizuku permission při startu, `onResume` → `refreshPermissions()`
- `CleanerScreen` — chips s úrovněmi, tlačítka pro all-files-access a usage-access,
  seznam cleanerů, Prohledat / Zkusmo / Vyčistit, hláška o neznámé velikosti package cache
- `MainViewModel` — `UiState` (phase, mode, cleaners, selected, scan, clean, permissions),
  `unusedPackagesFor()` skládá seznam z `UsageStatsProvider`
- `CleanerRepository` — parsuje JSON z Pythonu
- `PythonBridge` — Chaquopy, `registerAdbRunner()` / `registerRootRunner()`
- `ShizukuRunner` — `Shizuku.newProcess` přes reflexi (v API 13.1.5 je `@RestrictTo`)
- `RootRunner` — `su` přes `Runtime.exec`, `isAvailable()` ověří `uid=0`
- `UsageStatsProvider` — `AppOpsManager` kontrola + `queryUsageStats`

## Testy

`app/src/test/python/test_core.py`, běží `cd app && python3 -m pytest src/test/python -q`.

Pokrývá: metadata cleanerů (včetně kind), capability gating, guardy backendů,
package-name validaci proti injection, `pm clear`/`force-stop` pořadí a chybové stavy,
`su` wrapping u root backendu, scan/apply na fixture stromu, dry-run vs apply,
JSON log lines, package cleanery přes engine, API boundary.

**Aktuální stav: 56 passed, 1 skipped.**

## Známé limity / TODO

1. APK se staví jen v CI; artefakt `bleachbit-droid-debug`.
2. `local.properties` je v `.gitignore`; `apkmanifest.xml` (dump framework manifestu) taky.
3. Foreground service pro apply zatím **není** — manifest ho deklaruje, ale služba neexistuje.
   Dlouhá čistka na pozadí může být zabita.
4. Usage stats vyžadují ruční povolení (Settings → Usage access); bez něj se
   `unused_app_cache` přeskočí.
5. All-files-access vyžaduje ruční povolení (Settings → All files access).
6. Chybí: reálné testy na zařízení, golden reference z fáze 0, notifikace o průběhu.

## Historie

- `ca487c8` init (jen AGENTS.md)
- `fa25977` Android skeleton: Chaquopy core, backendy, Compose UI, Shizuku, CI
- `a54ba12` postup.md
- `92f44fe` CI: pryč ze setup-android action
- `0402e8e` compileSdk 36, core-ktx 1.17.0
- `ee53ca1` oprava Kotlin chyb (JSONObject→String, reflexe Shizuku, signatury)
- `aee08c1` manifest: storage, FGS, notifikace, usage stats
- `916c192` hluboká čistka: package cleanery, force-stop, root přes su, usage stats