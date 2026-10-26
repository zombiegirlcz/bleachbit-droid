# AGENTS.md — bleachbit-droid

Projekt: **BleachBit na Android** s podporou tří úrovní oprávnění — **non-root**, **ADB (Shizuku)** a **root**.

Základní teze: **čím vyšší oprávnění, tím kvalitnější (hlubší) čistka.**

---

## 1. Cíl projektu

Přenést BleachBit na Android tak, aby každá ze tří úrovní oprávnění uměla
skutečně to, co její oprávnění dovoluje — a ne víc:

- **non-root** — vlastní data aplikace + sdílené úložiště. Navíc díky
  vestavěnému glibc toolchainu umí to, co Android nativně neumí:
  `VACUUM` databází, bezpečné přemazání souborů (`shred`), GNU `find`.
- **ADB (Shizuku)** — navíc data ostatních aplikací a jejich cache
  (`pm clear --cache-only`), bez roota.
- **root** — vše: `/data/dalvik-cache`, systémové cache, staging adresáře.

---

## 2. Architektura (rozhodnuto)

Kotlin/Compose UI je tenká slupka. **Veškerá logika čištění je v Python core**
(`app/src/main/python/bleachbit_droid/`), který jde spustit i jako CLI bez
Androidu — to je hlavní vývojová a testovací cesta.

```
Compose UI  ->  MainViewModel  ->  CleanerRepository  ->  PythonBridge (Chaquopy)
                                                              |
                                                api.py (JSON-in / JSON-out)
                                                              |
                                      engine.scan / engine.apply
                                                              |
                                          PrivilegeBackend (nonroot/adb/root)
```

CLI: `python3 -m bleachbit_droid.cli {detect,cleaners,scan,clean}` —
`--mode nonroot|adb|root`, `--runner local|nh-adb|su`, `--json`, `--log`.
Slouží k ladění stejných code paths bez instalace APK.

---

## 3. Capability model

`app_data` < `shared_storage` < `other_app_data` < `system`

| backend | capabilities | packages | sqlite |
|---|---|---|---|
| `nonroot` | app_data, shared_storage | ne | ano (vestavěný `sqlite3`) |
| `adb` | + other_app_data | ano | ano |
| `root` | vše | ano | ano |

Cleaner se spustí jen když `requires` je podmnožinou `backend.capabilities`;
u `kind=package` navíc `backend.supports_packages`, u `kind=sqlite`
`backend.supports_sqlite`.

---

## 4. Tři druhy cleanerů

- **`kind=path`** — glob vzory, enumerace a mazání přes backend.
- **`kind=package`** — nemažeme soubory, ale voláme `pm clear --cache-only <pkg>`
  (Android sám ví, co je cache). Volitelně předtím `am force-stop <pkg>`.
  `unused_days=N` znamená, že seznam balíčků dodá Kotlin (`UsageStatsProvider`);
  bez seznamu se cleaner přeskočí (radši nic než mazat špatné appky).
  **Pozor:** root tier tuto cestu nepoužívá — viz §6.
- **`kind=sqlite`** — `VACUUM` přes vestavěný glibc `sqlite3`.

---

## 5. Vestavěný glibc toolchain (elf_loader)

Android neumí spustit glibc binárku (`execve` na `/lib/ld-linux-aarch64.so.1`
selže) a `sqlite3`/`shred`/GNU `find` v něm nejsou. Proto je v APK loader
z projektu `~/elf_loader`, který guest binárku own-loadne do privátního scope.

| co | kde | proč tam |
|---|---|---|
| `libelf_loader.so` | `jniLibs/arm64-v8a/` | bionic, Android ho extrahuje do `nativeLibraryDir` a dovolí `execve` |
| `usr/bin/{shred,sqlite3,find}` | `assets/rootfs/` | 6,7 MB; loader je jen `mmap`uje, `execve` se ho netýká |
| 12 glibc knihoven | `assets/rootfs/usr/lib/aarch64-linux-gnu/` | musí zůstat původní sonames (`libc.so.6`), jinak `DT_NEEDED` selže |

**Proč ne `libc.so.6` do `jniLibs`:** Android extrahuje z `lib/<abi>/` jen
soubory končící na `.so`, takže `libc.so.6` by zmizel. Přejmenovat nejde —
loader hledá závislosti podle soname. Proto assets + `filesDir`, kde `mmap`
funguje (zákaz `execve` na app datech se sem nevztahuje).

`GuestTool` (Kotlin) spouští nástroje s `ELF_ROOTFS`; streamy čte v daemon
vláknech s timeoutem. `GuestSelfTest` projede všechny tři při startu →
výsledek v logcatu.

**Ověřeno na zařízení (APK, app uid 10316):**
- `shred` 9.7 — overwrite → rename chain → removed
- `find` 4.10 — `-mtime +90`, `-empty`, `-size +100k` správně
- `sqlite3` — **VACUUM 3 088 384 → 1 032 192 B**, freelist 502 → 0,
  `integrity_check = ok`, ownership zůstal `10316:10316`

---

## 6. Bezpečnostní invarianty (držet!)

1. **Žádná mutace filesystemu mimo `PrivilegeBackend.delete()`** — engine ani
   cleaner nesmí volat `os.remove`/`shutil.rmtree` přímo.
2. **Nikdy neměnit ownership** systémových ani app dat. Když soubor musí
   existovat pod jiným uid, **vytvoř ho jako to uid** (`su <uid> -c …`), nikdy
   ne `chown`. Root-owned soubor v app datech = rozbitá appka (a bootloop,
   který se v tomto projektu už jednou stal).
3. **Root tier maže cache aplikací pod UID té aplikace** — `clear_app_cache()`
   čte uid z `/data/data/<pkg>` a maže přes
   `su <uid> -c 'find … -mindepth 1 -delete'`.
4. `NonRootBackend` má whitelist rootů; prázdný seznam = vše denied.
5. `AdbBackend` odmítá relativní cesty a `..`.
6. `_validate_package()` odmítá cokoli mimo `[A-Za-z0-9._]` a musí začínat
   písmenem → žádná shell injection přes jméno balíčku.
7. Engine nikdy nemaže kořenovou složku globu, jen její obsah.
8. **Cleaner nesmí globovat `…/*/lib/**`** a podobné vzory, které vypadají jako
   „zbytky“, ale matchují nativní knihovny nainstalovaných aplikací.
   (Přesně to dělal zrušený `apk_leftovers`.)
9. `unshare -m` → okamžitě `mount --make-rprivate /`; `$ROOTFS` není izolované
   prostředí.

---

## 7. Výkonové invarianty

- **Jeden persistentní shell na tier.** `ShellSession` drží jeden `su`/Shizuku
  proces a posílá mu příkazy na stdin, oddělené markerem `__BB_END_<n>:$?`.
  Spouštět nový proces na každý příkaz znamenalo Magisk prompt na každý
  `test -e` a zahlcenou obrazovku povolenin.
- **Bulk enumerace.** `PrivilegeBackend.list_sized()` vrátí `(path, size)` pro
  celý target jedním `find … -exec stat -c '%s<TAB>%n' {} +`.
  Per-file `stat` = 1 shell příkaz na soubor (naměřeno: 2000 souborů
  ~68 s vs **1,3 s** jedním příkazem).
- **`stat -c '%s\t%n'` musí v příkazu obsahovat skutečný TAB**, ne dva znaky
  `\` a `t`. Test to hlídá.
- **Progress je povinný.** `engine.scan()` i `engine.apply()` berou `on_event`;
  `api.scan_streaming`/`apply_clean_streaming` ho předávají do Kotlinu jako
  **samostatný Chaquopy argument**, nikdy ne v JSON payloadu.

---

## 8. Chaquopy úskalí (draze zaplacená)

- **Objekt v JSON payloadu se serializuje na string.** `JSONObject.put("cb", obj)`
  → `"cz.zombie.bleachbitdroid.ProgressSink@1a2b3c"`. Objekty (callbacky,
  runnery) se předávají jako další argument `callAttr`.
- **Chaquopy proxy je `callable()`**, ale zavolání nezasáhne `onEvent(String)`
  — vyhodí výjimku. Proto `_as_callback()` hledá **nejdřív `onEvent`** a
  `__call__` je jen fallback pro obyčejné Python callably (testy).
- **`supports_sqlite` je property odvozená z toho, zda byl runner injectnut.**
  Runner se musí registrovat **před prvním `reload()`**, jinak UI nabídne
  databázové cleanery jako „chybí: sqlite3“, i když tool funguje.
- **`RootBackend` musí dědit `_SqliteMixin`** a dostat `vacuum_runner` — jinak
  root tier databáze vůbec nenabídne.
- **`Process.waitFor(timeout, unit)` nefunguje se Shizuku remote procesem** —
  polluje `exitValue()`, které hází `IllegalArgumentException: process hasn't
  exited`. Čeká se přes `CountDownLatch` + blokující `waitFor()` na vlákně.

---

## 9. Build & test workflow

- **Vždy nejdřív build, pak deploy.** „Hotovo“ bez buildu je lež.
- **Před commitem vždy pustit testy** (`cd app && python3 -m pytest src/test/python -q`).
- Build běží **jen v CI** (GitHub Actions) — lokálně to nejde (aapt2 je
  x86-64 only, chybí Java truststore).
- Nasazení: APK do `/sdcard/Download/` (proot ho tam vidí), pak
  `nh adb install /sdcard/Download/x.apk`. **`nh adb push` nefunguje** —
  `nh adb` je server pod uid 2000 a cesty z prootu nevidí.
- Testovací matrice: `{nonroot, adb, root} × {dry-run, apply}`.
- Testy mazání **nikdy** na reálných systémových datech — fixture stromy
  v `tmp_path`, `FakeShell` pro shell backendy.
- Před tvrzením „funguje“ doložit: příkaz, výstup, exit code.

---

## 10. Ověřování na zařízení

- UI čti přes `nh dev acc --json` (accessibility dump). **Appka musí být
  v popředí** — terminál v prootu foreground krade, proto se start + tap + dump
  dělá **v jednom on-device řetězu**
  (`nh adb shell "am start …; sleep N; input tap X Y"`).
- Souřadnice tlačítek se mění podle délky seznamu cleanerů — vždy nejdřív dump.
- `uiautomator dump` dává Compose textovým uzlům nulové bounds; klikatelné
  parenty mají bounds správné.
- Logcat filtruj přes `--pid=$(pidof …)`, jinak se ztratí v systémovém šumu.
- `su <uid> -c` z `nh adb shell` funguje (kontext `u:r:magisk:s0`).
- `dd` v `nh adb shell` tiše selhává — na výrobu testovacích souborů používej
  `head -c N /dev/zero > soubor`.

---

## 11. Konvence kódu

- Python: `snake_case`, typové anotace u veřejných funkcí, žádné holé `except:`.
- Každý cleaner: `name`, `description`, `requires`, `paths`, `risk`
  (+ `kind`, u package `unused_days`).
- Logování: strukturované (JSON lines) s poli `backend`, `path`, `action`,
  `bytes`, `result`, `reason`, `cleaner`.
- Uživatelské hlášky v **češtině**, kód a komentáře anglicky (odůvodnění
  mappingů může česky).
- **Komentáře vysvětlují PROČ, ne co.** U každé opravy patří do kódu důvod,
  jaká chyba to byla a co se rozbilo — ne popis toho, co řádek dělá.
- `postup.md` je **autoritativní zdroj aktuálního stavu projektu** — čti ho
  první, udržuj aktuální.

---

## 12. Vyhledávání v kódu — basemind

Používej **basemind** místo `grep`/`find`/`git log`:
- hledání v kódu → `code` mode `grep`
- symbol → `symbols` / `definition` / `references`
- tvar souboru → `outline`
- soubor podle jména → `code` mode `find`
- historie → `git` modes `recent` / `blame_symbol` / `touching`

Fallback na `grep`/`git` jen když basemind nefunguje nebo je obsah mimo index
(`.md`, konfigy → `memory` mode `documents`).

---

## 13. Co NEDĚLAT

- ❌ Netvrdit, že něco funguje, bez doloženého výstupu.
- ❌ Nepředpokládat, že kontext konverzace = aktuální stav. Vždy ověřit
  (`git log`, `cat` souboru).
- ❌ Neobcházet `PrivilegeBackend` přímým voláním `os.remove`.
- ❌ Nepřidávat cleaner bez definovaného `requires` a `risk`.
- ❌ Nemazat „protože to je cache“ — ověřit, že to cache opravdu je a že patří nám.
- ❌ Neměnit ownership (žádný `chown` na cizí data).
- ❌ Nedávat objekt do JSON payloadu pro Chaquopy.
- ❌ Nepoužívat `pm clear --cache-only` na root tieru (smaže i bookkeeping
  package manageru; `su <uid> -c find -delete` je užší a bezpečnější).
- ❌ Nedělat novou práci v jiné branchi než `master`.
- ❌ Necommitovat tajemství (tokeny, klíče) — nikdy je nevypisovat.