# postup.md — aktuální stav projektu

Autoritativní zdroj stavu. Aktualizuj po každé změně.

## Co projekt je

BleachBit pro Android se třemi úrovněmi oprávnění: **non-root**, **ADB (Shizuku)**, **root**.
Teze: čím vyšší oprávnění, tím hlubší čistka (capability gating).

## Stack (rozhodnuto)

- Kotlin + Jetpack Compose (UI) — `app/src/main/java/cz/zombie/bleachbitdroid/`
- Python core přes **Chaquopy 17.1.0**, Python 3.13 — `app/src/main/python/bleachbit_droid/`
- Gradle 8.14.3, AGP 8.13.2, Kotlin 2.2.21, JDK 21
- Shizuku API 13.1.5 (`dev.rikka.shizuku:api`, `:provider`)
- Build běží na GitHub Actions (`.github/workflows/build.yml`) — lokální build na zařízení
  je problematický (aapt2 z Google je x86-64 only, chybí Java truststore)

## Struktura Python core

| modul | obsah |
|---|---|
| `models.py` | `Capability`, `Risk`, `Target`, `CleanerSpec`, `Entry`, `LogRecord`, `ScanReport`, `CleanReport` |
| `capabilities.py` | pořadí capability, české labely, `missing()` |
| `backends.py` | `PrivilegeBackend` (ABC), `NonRootBackend`, `AdbBackend`, `RootBackend`, `shell_quote` |
| `cleaners.py` | registry cleanerů s `name/description/requires/paths/risk` |
| `engine.py` | expanze `<placeholder>` tokenů, `scan()`, `apply()`, `describe()` |
| `api.py` | JSON-in/JSON-out entry pointy pro Kotlin (`detect`, `list_cleaners`, `scan`, `apply_clean`, `set_adb_runner`) |

### Capability model

`app_data` < `shared_storage` < `other_app_data` < `system`

- non-root backend: `app_data`, `shared_storage` (guard: jen povolené rooty)
- adb (Shizuku): + `other_app_data`
- root: vše

Cleaner se spustí jen když `requires ⊆ backend.capabilities`.

### Bezpečnostní invarianty (držet!)

- Žádná mutace filesystemu mimo `PrivilegeBackend.delete()`.
- `NonRootBackend` má whitelist rootů (`filesDir`, `cacheDir`, `externalFilesDir`);
  prázdný seznam = vše denied.
- `AdbBackend` odmítá relativní cesty a `..`.
- Engine nikdy nemaže kořenovou složku globu (např. `cache/`), jen její obsah —
  aby appky neztratily očekávané adresáře.
- Overlapping globy: každá cesta se reportuje jen jednou (`seen` set).

## Cleanery (fáze 1)

| name | requires | risk |
|---|---|---|
| `app_cache` | app_data | low |
| `thumbnails` | shared_storage | low |
| `dalvik_cache` | other_app_data, app_data | medium |
| `logcat_dumps` | system | medium |
| `apk_leftovers` | system | high |

## UI

- `MainActivity` — Compose, request Shizuku permission při startu
- `CleanerScreen` — chips s úrovněmi oprávnění, seznam cleanerů se switchem,
  tlačítka Prohledat / Zkusmo (dry-run) / Vyčistit
- `MainViewModel` — `UiState` (phase, mode, cleaners, selected, scan, clean)
- `CleanerRepository` — parsuje JSON z Pythonu
- `PythonBridge` — Chaquopy volání, `registerAdbRunner(ShellRunner)`
- `ShizukuRunner` — `Shizuku.newProcess(["sh","-c",cmd])`, vrací JSON `{code,stdout,stderr}`

## Testy

`app/src/test/python/test_core.py` — 29 testů, běží `cd app && python3 -m pytest src/test/python -q`.
Pokrývá: metadata cleanerů, capability gating, guardy backendů, scan/apply na fixture
stromu, JSON log lines, API boundary (nikdy nevyhodí), dry-run vs apply.

**Aktuální stav: 29 passed.**

## Známé limity / TODO

1. APK se zatím staví jen v CI (GitHub Actions). Artefakt `bleachbit-droid-debug`.
2. `app/src/main/AndroidManifest.xml` obsahuje Shizuku permission
   `moe.shizuku.manager.permission.API_V23` + `ShizukuProvider`.
3. Root backend se v Pythonu aktivuje jen když `os.geteuid() == 0` — v Chaquopy to
   nikdy nebude true, je potřeba root shell runner (stejný princip jako Shizuku,
   ale `su -c`). Zatím neimplementováno → root mode v UI nebude dostupný.
4. Chybí: reálné testy na zařízení, golden reference z fáze 0, notifikace o průběhu.
5. `local.properties` je v `.gitignore` (sdk.dir se v CI nastaví samo).

## Historie

- `ca487c8` init (jen AGENTS.md)
- `fa25977` Android skeleton: Chaquopy core, backendy, Compose UI, Shizuku, CI