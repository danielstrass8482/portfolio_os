# FORCE ROW LEVEL SECURITY — Umbau-Plan (2026-08-21)

Reines Plan-Dokument, **keine Code-Änderung**. Grundlage für eine informierte
Entscheidung vor der eigentlichen Umsetzung. Umfang laut Vorgabe: 14 Tabellen
(7 bereits policied + `pos_transactions` + 5 bisher ungeschützte +
`pos_admin_access_log`); `pos_users`/`pos_asset_classes`/`pos_family_goals`
bleiben außen vor.

## Status (2026-09-07)

Chunk 1 (Commit `c16b405`), Chunk 2 inkl. Nachzug (Commits `982842c`,
`8e1d89c`) sind umgesetzt. **Vorbereitung für Chunk 5** (SECURITY DEFINER
Owner-Lookup, Option B) ist umgesetzt: das im Chunk-2-Nachzug dokumentierte
Restrisiko (Sonderfall c, s.u. – die 4 Admin-Bypass-Owner-Lookups liefen
noch über ein normales `session.get()` unter dem Admin-eigenen RLS-Kontext,
wären also unter FORCE RLS mit reinen Owner-Policies selbst RLS-gefiltert
gewesen) ist geschlossen:

- 4 SQL-Funktionen `pos_position_owner_id()`/`pos_portfolio_owner_id()`/
  `pos_transaction_owner_id()`/`pos_real_estate_owner_id()` (SECURITY
  DEFINER, geben ausschließlich die owner_id zurück) — angelegt via
  `database.py::_migrate_owner_lookup_functions()` (läuft automatisch in
  `init_db()`, sicher auf jeder Umgebung).
- Die 4 Python-Helfer in `api.py` (`_position_owner_id`/`_portfolio_owner_id`/
  `_transaction_owner_id`/`_real_estate_owner_id`) rufen jetzt diese
  Funktionen statt eines normalen `session.get()` auf.
- **Einmaliger manueller Schritt vor Chunk 7 noch offen** (braucht Postgres-
  Superuser, bewusst NICHT automatisiert): `docs/rls-owner-lookup-bypass-
  role-setup.sql` legt die dedizierte `BYPASSRLS`-Rolle
  `pos_owner_lookup_bypass` an und schaltet die 4 Funktionen auf deren
  Ownership um — erst danach bypassen sie tatsächlich FORCE RLS (SECURITY
  DEFINER allein reicht nicht, siehe Kommentar in `_migrate_owner_lookup_functions`).
  **Auf Produktion noch NICHT ausgeführt.**
- Verifiziert gegen eine lokale Wegwerf-Postgres-Instanz mit ECHTEM FORCE ROW
  LEVEL SECURITY + Owner-Policies auf `pos_positions`/`pos_portfolios`/
  `pos_transactions`/`pos_real_estate` (simuliert den künftigen Chunk-5/7-
  Zustand, siehe `test_rls_owner_lookup_functions.py`, 49/49) — inkl.
  2-Konten-Cross-Access mit expliziten, getrennten Accounts über alle 12
  betroffenen Endpoints, PUBLIC-Execute-Sperre, NULL-Fall. Bestehende Suiten
  weiterhin grün (`test_rls_context.py` 20/20, `test_rls_admin_bypass_helpers.py`
  48/48, `test_rls_special_cases.py` 17/17, `test_product_scope.py` 10/10) —
  keine Regression.

Chunk 3 (`main.py`/`notifier.py`, `update_prices()` via Pro-Nutzer-Iteration)
ist ebenfalls bereits umgesetzt (Commit `982842c`). Chunk 4 (`dashboard.py`/
`onboarding.py`) bleibt blockiert auf die offene Frage aus Abschnitt 7. Chunk
5 selbst (die eigentlichen `CREATE POLICY`-Statements aus Abschnitt 4/5/6)
sowie Chunk 6/7 sind noch NICHT umgesetzt.

## 0. Kernproblem zur Erinnerung

`get_session_for_user()` (setzt `app.current_user_id`) existiert seit
Security-Schritt-2, wird aber **nirgends aufgerufen** — alle 112 echten
`with get_session() as session:`-Aufrufstellen laufen ohne User-Kontext.
`FORCE ROW LEVEL SECURITY` würde diese Stellen sofort auf 0 sichtbare Zeilen
setzen, sobald sie eine der 14 Tabellen berühren.

## 1. Call-Site-Inventar (112 Stellen, gruppiert)

| Datei | Stellen | Charakter |
|---|---|---|
| `api.py` | 35 | FastAPI-Endpoints, größtenteils "meine eigenen Daten" (current_user.id), 22 davon nutzen bereits `_resolve_user_id()` für optionalen Admin-Cross-View (Positionen, Tax, Rebalancing, Real-Estate, Family, Haushaltsbuch, KI-Analyse, Portfolios, Target-Weights, Import) |
| `dashboard.py` | 25 | Streamlit — **kein eigenes Auth-/User-Konzept** (kein `session_state`-Login, kein "current_user" irgendwo im File). Berührt 10 der 14 Tabellen. Siehe Risiko-Abschnitt Punkt 7 — offene Frage, kein Vorschlag von mir. |
| `onboarding.py` | 15 | Läuft innerhalb von `dashboard.py`'s Prozess (Streamlit-Onboarding-Flow) — dieselbe offene Frage wie oben |
| `portfolio.py` | 14 | Geschäftslogik-Funktionen, alle nehmen `user_id`-Parameter entgegen (von Aufrufer übergeben) — EXCEPT `update_prices()` (s. Sonderfall 1) |
| `tax_engine.py` | 8 | Alle mit `user_id`-Parameter, folgen demselben Muster wie `portfolio.py` |
| `database.py` | 8 | Helper-Funktionen (`save_real_estate`, `save_buchungen`, `log_admin_access` u.a.) — nehmen `user_id`/`admin_user_id`+`target_user_id` als Parameter entgegen |
| `rebalancing.py` | 3 | Alle mit `user_id`-Parameter |
| `notifier.py` | 2 | Bereits pro Nutzer aufgerufen (Fix vom 21.08.), passt ins Loop-Muster von Sonderfall 2 |
| `main.py` | 2 | `_alle_user_ids()` (kein RLS-Bezug) + der Job-Loop selbst (s. Sonderfall 2) |

**Wichtige strukturelle Beobachtung:** Fast alle Funktionen in
`portfolio.py`/`tax_engine.py`/`rebalancing.py`/`database.py` (Ausnahmen s.u.)
bekommen `user_id` bereits als Parameter von ihrem Aufrufer übergeben — das
ist exakt der Wert, den `app.current_user_id` bräuchte. Das spricht für einen
**zentralen statt 112 verstreute Eingriffe**: siehe Empfehlung unten.

## 2. Empfohlener Mechanismus (statt 112 Einzeländerungen)

`get_session()` selbst kontextsensitiv machen via `contextvars.ContextVar`,
statt jede Aufrufstelle einzeln umzuschreiben:

```python
# database.py
import contextvars
_current_user_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_user_id", default=None
)

@contextmanager
def user_context(user_id: int | None):
    """Setzt den RLS-Kontext für den aktuellen Task/Request. None = kein
    Kontext (Systemjobs, s. Sonderfall 1) -- absichtlich EXPLIZIT, kein
    stiller Default."""
    token = _current_user_ctx.set(user_id)
    try:
        yield
    finally:
        _current_user_ctx.reset(token)

def get_session():
    session = SessionLocal()
    try:
        uid = _current_user_ctx.get()
        if uid is not None:
            session.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                             {"uid": str(uid)})
        yield session
        session.commit()
    ...
```

Damit brauchen **portfolio.py/tax_engine.py/rebalancing.py/database.py
(48 Stellen) keine einzige Änderung** — sie laufen weiter mit
`get_session()`, der Kontext kommt von außen. Nur die **Einstiegspunkte**
müssen `user_context(...)` setzen:

- **api.py:** eine Zeile pro Endpoint (`with database.user_context(user_id):`
  um den bestehenden Funktionskörper, `user_id` ist an allen 35 Stellen
  bereits als lokale Variable vorhanden — entweder `current_user.id` direkt
  oder das Ergebnis von `_resolve_user_id(...)`). Realistisch sogar auf EINEN
  Ort reduzierbar: `require_portfolio_os_access`/`get_current_user` als
  Dependency könnte den Kontext auf `current_user.id` vorbelegen, die 22
  `_resolve_user_id`-Stellen überschreiben ihn danach explizit auf die
  Ziel-`user_id`.
- **main.py/notifier.py:** `user_context(user_id)` um jede Job-Iteration
  (s. Sonderfall 2).
- **dashboard.py/onboarding.py:** offene Frage, s. Risiko-Abschnitt.

Das reduziert den Diff drastisch (von "112 Stellen anfassen" auf "~25-35
Einstiegspunkte + 1 zentraler Mechanismus"), senkt aber auch das Risiko,
eine Stelle zu vergessen — die 48 Business-Logic-Funktionen bleiben
unverändert und funktionieren automatisch korrekt, sobald der Aufrufer den
Kontext richtig setzt.

## 3. Die drei Sonderfälle

### a) `portfolio.py::update_prices()` — nutzerübergreifendes Preis-Update

Ist-Zustand: `session.query(PosPosition).all()` — bewusst über ALLE Nutzer
hinweg in einem Rutsch (Performance: ein Lauf pro 15-Min-Intervall statt N
Läufe). Das lässt sich **nicht** in einen Einzelnutzer-RLS-Kontext pressen,
ohne die Funktion komplett umzubauen.

**Vorschlag:** `user_context(None)` (= kein Kontext gesetzt) reicht hier
NICHT aus, sobald `pos_positions` FORCE-RLS hat — Owner-Bypass ist ja genau
das, was FORCE abschaltet. Zwei echte Optionen:

1. **Eigene DB-Rolle mit `BYPASSRLS`** ausschließlich für diesen einen
   Systemjob (z.B. `portfolio_os_system`), die App verbindet für
   `update_prices()` (und nur dafür) über eine zweite Engine/Connection mit
   dieser Rolle. Sauberste Lösung, aber zusätzliche DB-Rolle + zweite
   Connection-Verwaltung.
2. **Pro-Portfolio-Iteration mit User-Kontext**: `update_prices()` gruppiert
   Positionen nach `portfolio_id`→`user_id` und setzt den Kontext pro
   Nutzer neu, bevor dessen Positionen aktualisiert werden. Kein neuer
   DB-User nötig, aber N Kontext-Wechsel pro Lauf (bei aktuell 1
   aktivem Portfolio-OS-Nutzer irrelevant, bei mehreren ein Mehraufwand,
   der mit der Nutzerzahl linear wächst — nicht das teuerste Element im Lauf,
   da der yfinance-Call ohnehin dominiert).

**Meine Einschätzung, keine Entscheidung:** Solange nur Daniel
`portfolio_os_access=true` hat, ist Option 2 der pragmatischere erste
Schritt (kein neuer DB-User, kein zweiter Connection-Pool) — Option 1 lohnt
sich, sobald mehrere Nutzer aktiv Portfolio-OS nutzen und der Kontext-Wechsel-
Overhead spürbar wird.

### b) Notify-Jobs (main.py, loopen pro Nutzer)

Einfacher als (a): `main.py`'s Jobs iterieren bereits explizit über
`_alle_user_ids()` und rufen `notifier.send_*(user_id)` einzeln auf. Jede
dieser Aufrufe bekommt `with database.user_context(user_id):` um den
Funktionskörper (bzw. `notifier.py`'s `send_*`-Funktionen setzen ihn selbst
zu Beginn, analog zum bereits bestehenden `_get_user_email(user_id)`-Muster
vom 21.08.). Kein Sonderfall im eigentlichen Sinne — folgt demselben Muster
wie ein normaler Endpoint, nur dass der "Request" hier ein Scheduler-Tick ist.

### c) Admin-Cross-User-View (`_resolve_user_id`)

Bereits das sauberste der drei Probleme, weil der Code die Unterscheidung
"eigene Daten" vs. "bewusster Cross-User-Zugriff" schon exakt trifft und
sogar schon loggt (`log_admin_access`). Vorschlag: `_resolve_user_id()`
setzt den Kontext selbst, direkt an der Stelle, wo es heute schon
`log_admin_access(...)` aufruft:

```python
def _resolve_user_id(current_user, requested_user_id, endpoint, method="GET") -> int:
    if current_user.rolle != "admin" or requested_user_id is None:
        database.user_context_set(current_user.id)  # oder Rückgabewert + with-Block beim Aufrufer
        return current_user.id
    if requested_user_id != current_user.id:
        log_admin_access(current_user.id, requested_user_id, endpoint, method)
    database.user_context_set(requested_user_id)
    return requested_user_id
```

Das ist exakt die "explizite, geloggte Kontext-Umschaltung nur für den einen
Request" — kein allgemeiner RLS-Bypass, weil der Kontext danach IMMER auf
irgendeine konkrete `user_id` zeigt (nie None/leer), nur eben auf die des
Ziels statt des Admins. `_owner_check_id()`/`_maybe_log_admin_access()`
(die anderen Admin-Bypass-Helfer in api.py, für
update/delete-Position/Transaction/Portfolio/RealEstate) brauchen dieselbe
Behandlung — sie geben aktuell `None` zurück, um eine App-seitige
Ownership-Prüfung zu überspringen; unter FORCE RLS reicht das nicht, hier
muss der tatsächliche Owner (nicht der Admin) als Kontext gesetzt werden,
sonst blockt die DB, wo die App-Prüfung bewusst durchlässt.

## 4. Policy-Vorschlag `pos_transactions`

Kein direktes `user_id`, aber `portfolio_id → pos_portfolios.user_id` —
exakt dasselbe Muster wie die bestehende `pos_positions`-Policy:

```sql
CREATE POLICY user_isolation ON pos_transactions
  USING (
    portfolio_id IN (
      SELECT id FROM pos_portfolios
      WHERE user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
    )
  );
```

Kein offener Punkt — Schema ist eindeutig, keine Rückfrage nötig.

## 5. Policy-Vorschlag `pos_admin_access_log`

Kein einfaches `user_id`, sondern zwei Rollen (`admin_user_id`,
`target_user_id`). Zwei Varianten, **offene Design-Frage, keine
Vorentscheidung von mir**:

**Variante A — nur der Admin sieht seine eigenen Zugriffe:**
```sql
CREATE POLICY admin_sees_own_accesses ON pos_admin_access_log
  FOR SELECT USING (admin_user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);
CREATE POLICY admin_writes_own_accesses ON pos_admin_access_log
  FOR INSERT WITH CHECK (admin_user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);
```
Konsequenz: `log_admin_access()` muss beim Schreiben den Kontext auf den
**Admin** setzen (nicht das Ziel) — Konflikt mit Sonderfall (c) oben, wo der
Kontext direkt danach auf das Ziel wechselt. Reihenfolge in
`_resolve_user_id()` müsste daher sein: erst loggen (Kontext=Admin), dann
Kontext auf Ziel umschalten.

**Variante B — betroffener Nutzer sieht auch, wer auf seine Daten
zugegriffen hat (Transparenz):**
```sql
CREATE POLICY admin_or_target_sees_entry ON pos_admin_access_log
  FOR SELECT USING (
    admin_user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
    OR target_user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
  );
```
Gäbe z.B. Dana (falls sie je `portfolio_os_access` bekommt) die Möglichkeit
zu sehen, dass/wann ein Admin auf ihre Daten zugegriffen hat — heute gibt es
dafür in der UI ohnehin keinen Endpoint, wäre also erstmal nur DB-seitig
vorbereitet, nicht nutzbar.

**Meine Einschätzung:** Variante A ist der sicherere Default (Audit-Logs
sollten primär für Admins/Auditoren sichtbar sein, nicht für die betroffene
Person selbst editierbar-nah in Reichweite) — aber das ist eine bewusste
Produktentscheidung, keine technische, daher explizit offen gelassen.

## 6. Policies für die 5 ungeschützten Tabellen

Alle vier mit direktem `user_id` folgen 1:1 dem bestehenden Muster der 7
bereits (wirkungslos) geschützten Tabellen:

```sql
CREATE POLICY user_isolation ON pos_daily_snapshots
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

CREATE POLICY user_isolation ON pos_investment_preferences
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

CREATE POLICY user_isolation ON pos_kategorisierungsregeln
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

CREATE POLICY user_isolation ON pos_rebalancing_proposals
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

CREATE POLICY user_isolation ON pos_tax_events
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);
```

Keine offenen Fragen — Schema ist bei allen fünf eindeutig.

## 7. Risiko-Einschätzung: Stellen, die absichtlich ohne User-Filter arbeiten

| Stelle | Warum kein Filter | Risiko bei zu aggressiver Umstellung |
|---|---|---|
| `portfolio.py::update_prices()` | Systemweites Preis-Update über alle Positionen | Bricht komplett, wenn naiv unter FORCE gestellt (s. Sonderfall a) |
| `api.py::family()` / `overview(family=true)` | Admin-Aggregation über ALLE `pos_users` (bewusst, `_require_admin`-gated, ADMIN-SCOPE-TODO) | Muss wie Sonderfall (c) behandelt werden — mehrere Kontext-Wechsel INNERHALB eines einzigen Requests (einmal pro aggregiertem Nutzer), nicht nur einer |
| `api.py::list_users/get_pending_users/admin_approve_user/...` | Admin-Verwaltung aller `pos_users` | `pos_users` ist ohnehin außerhalb des Scopes dieser Runde — kein Konflikt, aber falls `pos_users` später auch RLS bekommt, bräuchten diese Endpoints eine eigene Bypass-Logik |
| **`dashboard.py`/`onboarding.py` (40 Stellen zusammen)** | **Kein Auth-/User-Konzept im gesamten Streamlit-Code** — kein Login, kein `session_state`-User, keine erkennbare Stelle, die "wer bin ich" beantwortet | **Größtes offenes Risiko dieser ganzen Umsetzung.** Ich habe keine Stelle gefunden, die dashboard.py verrät, für welchen `user_id`-Kontext es aktuell rendert (weder Konstante noch Query-Parameter noch Session-State). Bevor hier irgendein `user_context(...)` gesetzt werden kann, muss erst geklärt werden: Ist `dashboard.py` faktisch ein Single-Operator-Tool nur für Daniel (dann reicht ein hartcodierter Kontext beim Programmstart), oder gibt es eine mir nicht aufgefallene Nutzerauswahl? **Das kläre ich nicht selbst, sondern frage nach**, bevor Chunk 2/3 (unten) dashboard.py anfasst. |
| `database.py::log_admin_access` | Schreibt bewusst BEIDE Rollen (admin+target) in einer Zeile | Policy-Design bereits in Abschnitt 5 behandelt |

## 8. Chunk-Einteilung für die Umsetzung

| Chunk | Inhalt | Größe/Risiko |
|---|---|---|
| **1** | `user_context()`-Mechanismus in `database.py` (ContextVar + Helper), OHNE ihn irgendwo scharf zu nutzen — reiner Infrastruktur-Chunk, testbar isoliert | **Klein, risikoarm.** Keine Verhaltensänderung, da noch nirgends aufgerufen. |
| **2** | `api.py` verdrahten: `get_current_user`/`require_portfolio_os_access` setzt Default-Kontext (`current_user.id`), `_resolve_user_id()` überschreibt bei Admin-Cross-View (Sonderfall c), `_owner_check_id`/`_maybe_log_admin_access`-Pfade nachziehen. Test: alle 35 Endpoints einmal manuell/automatisiert durchspielen (eigene Daten + Admin-Cross-View). | **Mittel.** Ein zentraler Eingriffspunkt, aber 22 Cross-View-Stellen einzeln zu verifizieren braucht Sorgfalt. |
| **3** | `main.py`/`notifier.py` verdrahten (Sonderfall b) + `update_prices()`-Entscheidung (Sonderfall a, Option 1 oder 2) treffen und umsetzen. | **Mittel, eine echte Architekturentscheidung nötig** (DB-Rolle vs. Loop) — sollte VOR Umsetzung mit Dir abgestimmt sein. |
| **4** | `dashboard.py`/`onboarding.py` — **blockiert auf die offene Frage aus Abschnitt 7**, kann erst starten, wenn geklärt ist, wie dashboard.py "seinen" Nutzer bestimmt. | **Unklare Größe, da Voraussetzung fehlt.** Potenziell der aufwändigste Chunk (40 Stellen), aber vielleicht auch trivial (1 Zeile), falls es tatsächlich fest für Daniel läuft. |
| **5** | Fehlende Policies anlegen (`pos_transactions`, `pos_admin_access_log` gemäß gewählter Variante, 5 Gap-Tabellen) — reine SQL, kein Python-Code. | **Klein.** SQL aus Abschnitt 4/5/6 dieses Dokuments, einsatzbereit. |
| **6** | Test gegen isolierte Kopie (Teil 2 des ursprünglichen Auftrags: 2 Nutzer, Lese-/Schreibtest, Cross-Access-Sicherheitstest) — erst NACH Chunk 1-5. | Wie ursprünglich beauftragt. |
| **7** | `FORCE ROW LEVEL SECURITY`-SQL vorbereiten (Teil 3) — Freigabe für Live-Lauf separat einholen. | Wie ursprünglich beauftragt. |

**Empfohlene Reihenfolge:** 1 → 2 → 5 (kann parallel zu 2 laufen, ist reines
SQL) → 3 → 4 (nach Klärung) → 6 → 7. Chunk 4 ist der einzige mit echter
Unsicherheit in der Aufwandsschätzung — dort sollte vor dem Start eine kurze
Klärung stehen, nicht während der Umsetzung.
