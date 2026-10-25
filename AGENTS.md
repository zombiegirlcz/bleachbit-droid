# AGENTS.md — bleachbit-droid

Projekt: **BleachBit na Android** s podporou tří úrovní oprávnění — **non-root**, **ADB (Shizuku)** a **root**.

Základní teze: **čím vyšší oprávnění, tím kvalitnější (hlubší) čistka.**

---

## 1. Cíl projektu

Pjich UID** (`u0_aXXX`), ne pod rootem, pokud jde o jejich data a jsme non-root.
10. Při práci se soubory projektu přepnout na **UID vlastníka dané složky**; nikdy neměnit ownership cizích věcí.

---

## 7. Build & test workflow

- **Vždy nejdřív build, pak deploy.** „Hotovo“ bez buildu je lež.
- **Před commitem vždy pustit testy** (hook to vyžaduje). Evidence before claims.
- Regresní testy proti **golden referenci** z fáze 0 (§4).
- Testovací matrice: `{nonroot, shizuku, root} × {dry-run, apply}` — každá kombinace musí proběhnout bez výjimky a s rozumným logem.
- Testy mazání **nikdy** na reálných systémových datech: používat fixture adresáře v app files dir s **napodobenou** strukturou.
- Před tvrzením „funguje“ doložit: příkaz, výstup, exit code.

---

## 8. Konvence kódu

- Python: `snake_case`, typové anotace u veřejných funkcí, žádné holé `except:`.
- Každý cleaner: `name`, `description`, `requires: [capabilities]`, `paths: [...]`, `risk: low|medium|high`.
- Logování: strukturované (JSON lines) s poli `backend`, `path`, `action`, `bytes`, `result`, `reason`.
- Uživatelské hlášky v **češtině**, kód a komentáře anglicky (kromě odůvodnění mappingů, tam česky).
- `postup.md` je **autoritativní zdroj aktuálního stavu projektu** — čti ho první, udržuj aktuální.

---

## 9. Vyhledávání v kódu — basemind

Používej **basemind** místo `grep`/`find`/`git log`:
- hledání v kódu → `code` mode `grep`
- symbol → `symbols` / `definition` / `references`
- tvar souboru → `outline`
- soubor podle jména → `code` mode `find`
- historie → `git` modes `recent` / `blame_symbol` / `touching`

Fallback na `grep`/`git` jen když basemind nefunguje nebo je obsah mimo index (`.md`, konfigy → `memory` mode `documents`).

---

## 10. Co NEDĚLAT

- ❌ Netvrdit, že něco funguje, bez doloženého výstupu.
- ❌ Nepředpokládat, že kontext konverzace = aktuální stav. Vždy ověřit (`git log`, `cat` souboru).
- ❌ Neobcházet `PrivilegeBackend` přímým voláním `os.remove`.
- ❌ Nepřidávat cleaner bez definovaného `requires` a `risk`.
- ❌ Nemazat „protože to je cache“ — ověřit, že to cache opravdu je a že patří nám.
- ❌ Necommitovat tajemství (tokeny, klíče) — nikdy je nevypisovat do odpovědí ani commitů.
- ❌ Nedělat novou práci v jiné branchi než `master` (uživatel pracuje jen v masteru).