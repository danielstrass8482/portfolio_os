# Portfolio-OS Status-Audit — 2026-08-21

Reines Diagnose-Dokument, keine Code-Änderungen. Repo war ca. eine Woche inaktiv
(letzter Commit 2026-08-12). Erstellt vor Wiederaufnahme der Weiterentwicklung.

## 1. Live-Status (VPS 185.117.250.246)

Alle drei zugehörigen systemd-Services laufen, keine Fehlermeldungen in den
letzten 7 Tagen (`journalctl -p err` leer):

| Service | Port/Bind | Status | Läuft seit |
|---|---|---|---|
| `portfolio-api.service` | `0.0.0.0:8503` (uvicorn) | active | 2026-08-12 13:50 UTC |
| `portfolio-dashboard.service` | `127.0.0.1:8502` (Streamlit) | active | 2026-08-11 02:02 UTC |
| `portfolio-react.service` | `*:3000` (Next.js) | active | 2026-08-13 15:26 UTC |

**Korrektur zur Auftragsannahme:** Das Streamlit-Dashboard läuft auf Port
**8502**, nicht 8501 — Port 8501 gehört zum `trading_bot`-Dashboard (eigener,
separater Prozess). Alle drei Ports sind namentlich über `systemctl` verifiziert,
nicht geraten.

Kein Absturz, kein manueller Stop, kein Server-Neustart in der Inaktivitäts-
woche erkennbar — die Prozess-Startzeiten korrelieren mit den letzten Deploys
vom 11.–13.08, danach durchgehend stabil.

Nginx-Routing (`/etc/nginx/sites-available/portfolio`, Domain
`portfolio.diestraesschens.de`): `/` → `localhost:8080` (Preview-Reverse-Proxy
→ Next.js `:3000`), `/api/` → `:8503`. Entspricht dem in
[[portfolio-nginx-routing]] dokumentierten Präfix-Strip-Verhalten.

## 2. Repo-Diff-Stand

Beide Repos sind **sauber und vollständig deployed** — kein Drift:

| Repo | Lokaler HEAD = origin/main | Deployed auf VPS | Diff |
|---|---|---|---|
| `portfolio_os` | `5ba3ccf` (2026-08-12) | `5ba3ccf` | keiner |
| `portfolio_react` | `1dda3c8` (2026-08-12) | `1dda3c8` | keiner |

Kein ungepushter, kein gepushter-aber-nicht-deployter Stand. Letzte Aktivität
beider Repos: 2026-08-12, danach Funkstille (passt zur "eine Woche inaktiv"-
Beobachtung).

## 3. Funktionsbestand

### Backend-API (`api.py`, 1849 Zeilen)

75 Endpoints unter `/api/*` (11 `@app` + 64 `@protected`/Auth-geschützt),
grob gruppiert: Auth (Login/Register/Approval/Reset/Change-Password),
Positionen/Transaktionen/Depot-Import, Chart, Tax, Rebalancing, Trading-Bot-
Statusspiegel, Scan-Log, Entry-Time-Slots, Real-Estate, Family, Haushaltsbuch,
KI-Analyse, Portfolios, Target-Weights, Kontoauszug/Screenshot-Import.

### Kernfunktionen — fertig/teilweise/fehlt

| Bereich | Status | Kurzbefund |
|---|---|---|
| Auth (Register/Approval, Login, Passwort ändern/vergessen) | **fertig** | Argon2id, HttpOnly-JWT-Cookies, Rate-Limiting (slowapi), eigene Tests vorhanden (s. Abschnitt 6) |
| Positionen/Transaktionen/Depot-Import (CSV, Comdirect) | **fertig** | CRUD + CSV-Import produktiv |
| Chart/Kursdaten (yfinance) | **fertig** | |
| Rebalancing (Ist/Soll-Abweichung, Vorschläge, Bestätigungs-Flow) | **fertig** | inkl. `get_sparrate_empfehlung` — echte Lenkungslogik, keine Attrappe |
| Trading-Bot-Integration (Statusspiegel) | **fertig, bewusst read-only** | s. Abschnitt 4 |
| Haushaltsbuch (Kontoauszug-Kategorisierung, KI-Vorfilter) | **fertig** | inkl. Vision-Fallback für gescannte PDFs |
| KI-Analyse (Klumpenrisiko, Quartalsbericht, Chat, Portfolio-Screenshot-Import) | **fertig** | `llm_analyst.py`, Anthropic-API-gestützt |

### Die 5 Projekt-5-Differenzierungsfeatures — Ist-Stand

| Feature | Status | Befund |
|---|---|---|
| **Steuer-Intelligence** | **teilweise, mehr als erwartet** | `tax_engine.py` (320 Zeilen): Abgeltungssteuer-Berechnung, Verlusttopf, Freistellungsauftrag-Verwaltung, **echtes Tax-Loss-Harvesting** (`find_tax_loss_harvesting`, `get_optimal_sell_order`), Jahresübersicht. Kein Platzhalter — funktionale Kernlogik vorhanden. Fehlt: keine automatisierte Steuererklärungs-Anbindung, keine Mehrjahres-Optimierung über den aktuellen Freibetrag hinaus. |
| **Sparplan-Optimierung** | **teilweise** | `rebalancing.py::get_sparrate_empfehlung` verteilt eine gegebene Sparrate nach Ist/Soll-Abweichung auf Positionen. Reine Lenkung der nächsten Einzahlung — kein automatisiertes wiederkehrendes Sparplan-Management (keine Anbindung an echte Bank-/Broker-Sparpläne, keine Frequenz-/Betrags-Optimierung über Zeit). |
| **Immobilien-Integration** | **teilweise, mehr als erwartet** | `/api/real-estate` CRUD, `Immobilie.tsx`-Tab, AfA-Berechnung (Grundstücksanteil), KI-gestützte Wertschätzung (`llm_analyst.estimate_real_estate_value`) und **Kreditvertrag-Analyse per Vision-API** (`analyze_kredit_vertrag`). Substanzieller Funktionsumfang, aber nur Einzelobjekt-Verwaltung, keine Marktdaten-Anbindung (Preisvergleich, Mietspiegel etc.). |
| **Familien-/Haushaltssicht** | **teilweise, strukturell schwächer als der Name suggeriert** | Es gibt **kein "Familie"-Feld** in `pos_users` — `/api/family` aggregiert schlicht ALLE registrierten Nutzer und ist seit 2026-08-05 admin-only gesperrt (vorher ungeschützt, s. Abschnitt 4). Kein Konzept für mehrere unabhängige Haushalte mit gegenseitiger, eingeschränkter Sichtbarkeit — nur "Admin sieht alles" vs. "Nutzer sieht nur sich selbst". Haushaltsbuch selbst ist pro Einzelnutzer, nicht haushaltsübergreifend. |
| **Smart Notifications** | **gebaut, aber NICHT aktiv deployed** | `notifier.py` (Tages-/Wochen-/Monats-/Quartals-/Jahres-Report per E-Mail) + `main.py` (APScheduler-Orchestrierung dieser Jobs) sind vollständig implementiert — **aber `main.py` läuft auf dem VPS in keinem systemd-Service und keinem Cronjob**. Verifiziert: `systemctl list-units` zeigt nur `portfolio-api`/`-dashboard`/`-react`; `ps aux` zeigt keinen `main.py`-Prozess für `portfolio_os` (die einzigen laufenden `main.py`-Prozesse gehören zu `trading_bot`/`trading_bot_saxo`); der einzige Portfolio-bezogene Cronjob ist ein tägliches DB-Backup um 03:00 Uhr. Nutzer bekommen aktuell **keine** automatischen Reports oder Schwellwert-Alerts, obwohl der Code dafür fertig ist. Zudem rein digest-/cron-basiert, kein "smart" im Sinne adaptiver Trigger. |

**Freemium-Tiers**: nicht implementiert. Keine Spur von Subscription-/Tier-
Logik im Code (Grep auf `freemium`/`tier`/`subscription`/`Abo` liefert keine
Treffer außerhalb von Kommentar-Rauschen). Weder Datenmodell noch API noch UI
dafür vorhanden.

## 4. Offene Punkte aus früheren Audits — Gegencheck

| Punkt | Damaliger Stand | Jetzt |
|---|---|---|
| `TradingBot.tsx` auf Read-only reduziert | Reduziert (Audit Chunk 4/5, 2026-08-05) | **Noch aktuell.** 98 Zeilen, kein `useMutation` im File, Kommentar im Code bestätigt bewusste Stilllegung zugunsten von `trading_react` als alleiniger Schreib-Oberfläche. |
| RLS strukturell wirkungslos bei <5 Nutzern | Bekanntes Risiko | **Noch offen — und strukturell schwerwiegender als "nur bei wenigen Nutzern".** 8 von 17 `pos_*`-Tabellen haben `ROW LEVEL SECURITY` aktiviert, aber `FORCE ROW LEVEL SECURITY` ist auf **keiner** davon gesetzt. Die App verbindet sich als `trading_bot_user` — der **Owner** dieser Tabellen. Postgres lässt Tabellen-Owner RLS-Policies grundsätzlich umgehen, sofern nicht `FORCE` gesetzt ist. Damit ist RLS aktuell für die App-Verbindung komplett wirkungslos, unabhängig von der Nutzerzahl — Isolation läuft ausschließlich über applikationsseitige `user_id`-Filterung in den Queries. |
| ADMIN-SCOPE-TODO | Offen | **Noch offen**, im Code selbst dokumentiert (`api.py:656`, `:662`, `database.py:447`): "Admin" = voller Zugriff auf ALLE `pos_users`, nicht nur Familienmitglieder — bewusst so belassen für den aktuellen Beta-Testerkreis, vor echtem Onboarding weiterer Kunden nochmal zu entscheiden. |
| `portfolio-api` fehlte `PYTHONUNBUFFERED=1` | Bekannter Fix ausstehend | **Erledigt.** `Environment=PYTHONUNBUFFERED=1` im systemd-Unit gesetzt, letzter Commit dazu (`5ba3ccf`, 12.08.) ist der aktuelle HEAD und deployed. |
| `/login` Cache-Control-Anomalie | Beobachtet | **Noch vorhanden.** `/login` liefert `Cache-Control: s-maxage=31536000` (1 Jahr) plus `x-nextjs-cache: HIT`, `x-nextjs-prerender: 1` — Next.js prerendert die Login-Seite statisch mit sehr langer CDN-Cache-Vorgabe. Funktional vermutlich unkritisch (keine nutzerspezifischen Serverdaten im HTML), aber weiterhin eine Anomalie wert, geprüft zu werden, falls sich der Seiteninhalt je nutzerabhängig ändert. |
| `use_container_width` (Streamlit) deprecated | Bekannt | **Erledigt.** Commit `a3ec26f` (2026-08-05) hat alle Vorkommen im eigenen Code durch `width="stretch"` ersetzt — Grep auf `use_container_width` in `portfolio_os` (ohne venvs) liefert **null** Treffer im eigenen Code. |
| Geteiltes venv zwischen `portfolio-api`/`-dashboard` | Bekanntes Risiko | **Erledigt.** Commit `6e4e668` (2026-08-05) hat auf getrennte venvs aufgeteilt — auf dem VPS bestätigt: `venv_api/` und `venv_dashboard/` sind getrennte Verzeichnisse, in den systemd-Units auch getrennt referenziert. Das alte gemeinsame `venv/` liegt noch ungenutzt daneben (Aufräumkandidat, keine Funktionsgefahr). |

## 5. Architektur-Kurzüberblick

**Backend** (`portfolio_os`): FastAPI (`api.py`, 1849 Zeilen) + separates
Streamlit-Dashboard (`dashboard.py`, 2310 Zeilen, 7 Tabs) + APScheduler-
Orchestrierung (`main.py`, aktuell **nicht deployed**, s. Abschnitt 3) +
SQLAlchemy 2.0 (`database.py`, 811 Zeilen) ohne Alembic — Schema-Änderungen
laufen über `Base.metadata.create_all()` (legt nur fehlende Tabellen an,
migriert keine Spalten an Bestandstabellen; laut Code-Kommentar ist das
bewusst dokumentiert, nicht übersehen). Claude/Anthropic-API für KI-Analyse,
Immobilienschätzung, Kreditvertrags- und Screenshot-Auswertung.

**Frontend** (`portfolio_react`): Next.js 16.2.11 (App Router), React 19.2.4,
TypeScript, Tailwind CSS 4, TanStack Query. 11 Tab-Komponenten
(Übersicht, Positionen, Rebalancing, Steuer, Immobilie, Familie,
Haushaltsbuch, KI-Analyse, Trading-Bot, Verwaltung, PositionChart).

**Auth**: JWT in HttpOnly-Cookies (kein localStorage), Argon2id-Hashing
(64MB/3 Iterationen/4 Threads, mit Legacy-bcrypt-Fallback-Erkennung),
Rate-Limiting via `slowapi` (120/min Default, striktere Limits auf
Auth-Endpoints).

**Datenbank**: **Kein eigener Datenbank-Server** — `portfolio_os` teilt sich
die Postgres-Instanz **und dieselbe Datenbank** (`trading_bot`) mit dem
Trading-Bot, lediglich mit eigenem Tabellenpräfix `pos_*` (17 Tabellen:
Users, Positions, Transactions, Portfolios, Goals, Real-Estate, Buchungen,
Target-Weights, Tax-Config/-Events, Rebalancing-Proposals, Daily-Snapshots,
Asset-Classes, Investment-Preferences, Kategorisierungsregeln,
Admin-Access-Log, Family-Goals). DB-User `trading_bot_user` ist zugleich
Owner der `pos_*`-Tabellen — relevant für den RLS-Befund oben.

**Multi-Tenant-Status**: Analog zum in `trading_bot` etablierten Muster —
`pos_users` mit Registrierung + Admin-Approval-Flow, Alpaca-Connect pro
Nutzer, `user_id`-Spalten zur Isolation. **Kein** DB-natives RLS-Enforcement
(s. Abschnitt 4), Isolation ausschließlich über Query-seitige
`_resolve_user_id()`-Logik in `api.py`. Admin-Zugriffe auf fremde `user_id`
werden protokolliert (`pos_admin_access_log`), aber "Admin" ist aktuell
gleichbedeutend mit Vollzugriff auf alle Nutzer (ADMIN-SCOPE-TODO).

## 6. Tests

Zwei projekteigene Testdateien vorhanden: `test_change_password.py`,
`test_password_reset.py` (beide 2026-08-12, decken die jeweiligen Auth-Flows
inkl. Rate-Limiting und Timing-Safety ab). Beide sind laut eigenem
Docstring so konzipiert, dass sie eine **separate Wegwerf-Postgres-DB**
(eigener DB-User, eigene DB, danach `DROP`) benötigen. Im Rahmen dieses rein
lesenden Audits **nicht ausgeführt**, um keine DB-Objekte anzulegen — Status
daher unbekannt (weder als "grün" noch als "rot" verifiziert). Empfehlung:
vor der nächsten Auth-Änderung einmal laufen lassen.

Die zwei vorbestehenden Testfehler aus `trading_bot/test_saxo_connection.py`
und `trading_bot_saxo/test_benchmark_endpoint.py` sind wie im Auftrag
festgehalten **nicht** Teil dieses Audits.

## 7. Gesamteinschätzung

Der aktuelle Stand ist ein solides, breites **Portfolio-Verwaltungs-Tool**
mit echter Tiefe in Einzelbereichen (Steuer-Engine mit Tax-Loss-Harvesting,
KI-gestützte Immobilien- und Kreditvertragsanalyse, funktionierendes
Rebalancing) — das ist mehr Substanz, als eine reine Bestandsaufnahme
erwarten ließe, gerade bei Steuer- und Immobilien-Logik.

Gegenüber der Projekt-5-Vision fehlen aber die **produktdefinierenden
Differenzierungsmerkmale** noch fast vollständig:

- **Kein Freemium/Tiering** — überhaupt keine Code-Basis dafür, nicht mal ein
  Datenmodell-Stub.
- **Keine echte Familien-/Haushaltssicht** — strukturell nicht vorgesehen
  (kein "Familie"-Konzept in `pos_users`), nur binäres Admin-sieht-alles vs.
  Nutzer-sieht-sich-selbst.
- **Smart Notifications ist der auffälligste Rückschritt**: fertig
  entwickelt, aber seit dem letzten Deploy **nicht aktiv** — Nutzer erhalten
  aktuell keine automatischen Reports, obwohl der Code dafür bereitsteht.
  Das ist vermutlich der schnellste Hebel, um sichtbaren Produktwert zu
  heben (ein systemd-Service für `main.py` fehlt schlicht).
- Steuer-Intelligence und Immobilien-Integration sind am weitesten von den
  fünf Merkmalen, aber beide noch auf Einzelfunktions-Ebene, nicht als
  durchgängige "Intelligence"-Erzählung (z. B. keine automatisierte
  Mehrjahres-Steueroptimierung, keine Marktdaten-Anbindung bei Immobilien).

Sicherheitsseitig ist das System für den aktuellen kleinen Beta-Kreis
funktional abgesichert (Argon2id, Rate-Limiting, admin-only Family-Endpoint,
Access-Logging), aber der RLS-Befund zeigt, dass die DB-Isolation komplett
auf korrekter Anwendungslogik beruht, nicht auf einer zweiten,
datenbankseitigen Verteidigungslinie — das wird relevanter, je mehr Nutzer
und je mehr Endpoints dazukommen.

**Realistische nächste Schritte** (Priorität aus technischer Sicht, nicht
Produkt-Priorität):

1. `main.py`/Notifications als systemd-Service deployen — kleinster Aufwand,
   größter sofortiger Nutzen (Feature ist fertig, liegt nur brach).
2. RLS-Befund entscheiden: entweder `FORCE ROW LEVEL SECURITY` setzen (und
   dafür einen non-Owner-DB-User für die App-Verbindung einrichten) oder
   RLS als reine Dokumentation/Zukunftsvorbereitung markieren und aktiv auf
   die bestehende Query-seitige Isolation vertrauen — der aktuelle
   Zwischenzustand (aktiviert, aber wirkungslos) ist am ehesten irreführend.
3. ADMIN-SCOPE-TODO und Familien-Datenmodell zusammen angehen, sobald
   Familien-/Haushaltssicht tatsächlich priorisiert wird — beide hängen
   strukturell zusammen (ein echtes "Familie"-Feld in `pos_users` würde auch
   den Admin-Scope entschärfen).
4. Freemium/Tiering ist von allen fünf Merkmalen am weitesten entfernt vom
   Ist-Stand und am ehesten ein eigenständiger, größerer Entwurf (Billing,
   Feature-Gates) statt einer Erweiterung bestehender Endpoints.
