"""
test_dashboard_context.py – RLS-Umbau Chunk 4 (2026-10-01, siehe
docs/rls-force-umbau-plan-21-08.md): das Streamlit-Dashboard (dashboard.py)
ist ein Ein-Personen-Admin-Werkzeug und läuft fest im RLS-Kontext von
DASHBOARD_USER_ID (config.py) über database.pin_user_context().

Läuft gegen eine Wegwerf-Postgres-DB mit ECHTEM FORCE ROW LEVEL SECURITY +
Owner-Policies auf allen pos_*-Tabellen mit Nutzerbezug (simuliert den
Chunk-5/7-Zielzustand) und rendert dashboard.py headless über
streamlit.testing.v1.AppTest. Externe Netzwerkaufrufe (Alpaca/Bot-Kontowert,
EUR/USD-Kurs, yfinance-Kurse) sind gestubbt -- alle DB-/RLS-Pfade sind echt.

Deckt ab:
  1. Fail closed: ohne DASHBOARD_USER_ID / mit einem Nicht-Admin -> st.error +
     st.stop(), keine Tabs, keine Daten.
  2. Mit DASHBOARD_USER_ID: nur die Daten dieses Nutzers erscheinen (Portfolio,
     Immobilie, Buchung eines zweiten Nutzers mit eigenen Daten NICHT); kein
     Nutzerwähler, kein Familien-Modus, kein Familie-Tab, keine Nutzerverwaltung.
  3. Schreibzugriff über das Dashboard (Formular "Neues Portfolio") läuft im
     festen Kontext -- unter FORCE RLS würde ein falscher/fehlender Kontext
     die Policy verletzen.
  4. Cross-Session-/Thread-Isolation des ContextVar-Zustands (die ungeprüfte
     Annahme aus der Diagnose):
     a) jeder Script-Run läuft in einem eigenen, neuen Thread, und der
        RLS-Kontext ist VOR pin_user_context() leer -- kein Übertrag aus
        früheren Runs oder anderen Sessions;
     b) zwei AppTest-Sessions mit unterschiedlichem Dashboard-Nutzer,
        verschränkt ausgeführt, sehen jeweils nur ihre eigenen Daten;
     c) nebenläufig: ein Dashboard-Run parallel zu einem Thread, der fest auf
        einen anderen Nutzer gepinnt ist, und einem Thread ganz ohne Kontext --
        keiner sieht fremde Daten, der Thread ohne Kontext sieht unter FORCE
        RLS gar nichts;
     d) der Kontext des Test-Hauptthreads bleibt die ganze Zeit None;
     e) die Warnung in pin_user_context() bei bereits gesetztem FREMDEM
        Kontext feuert tatsächlich -- und in keinem echten Dashboard-Run.

Mehrfach gegen dieselbe DB lauffähig (RUN_ID in E-Mails/Markern).

Setup (Wegwerf-Postgres, analog test_rls_owner_lookup_functions.py; braucht
streamlit im Python-Interpreter, z.B. venv_dashboard):
    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER dashctx_tmp WITH PASSWORD 'dashctx_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_dashctx OWNER dashctx_tmp;"
    DATABASE_URL=postgresql://dashctx_tmp:dashctx_tmp_pw@localhost:5432/portfolio_os_dashctx \
    venv_dashboard/bin/python test_dashboard_context.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_dashctx;"
    sudo -u postgres psql -c "DROP OWNED BY dashctx_tmp; DROP ROLE dashctx_tmp;"
"""
import io
import itertools
import os
import sys
import threading
import uuid
from contextlib import contextmanager, redirect_stdout
from datetime import date

os.environ.setdefault(
    "DATABASE_URL", "postgresql://dashctx_tmp:dashctx_tmp_pw@localhost:5432/portfolio_os_dashctx",
)
os.environ.setdefault("ALERT_EMAIL", "")
os.environ.pop("DASHBOARD_USER_ID", None)

import config  # noqa: E402
import database  # noqa: E402
import portfolio  # noqa: E402
import trading_bot_connector  # noqa: E402
from database import text  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

RESULTS = []
RUN_ID = uuid.uuid4().hex[:8]
DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")
TIMEOUT = 120


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + str(detail)) if detail else ''}")


# ─────────────────────────────────────────────
# Stubs für Netzwerkzugriffe (keine DB-/RLS-Pfade)
# ─────────────────────────────────────────────

def _stub_network():
    trading_bot_connector.get_bot_account_value_eur = lambda: {
        "positionen_usd": 0.0, "cash_usd": 0.0, "total_usd": 0.0, "positionen_eur": 0.0,
        "cash_eur": 0.0, "total_eur": 0.0, "positionen_detail": [],
    }
    trading_bot_connector.get_bot_positions_detail = lambda: []
    trading_bot_connector._eurusd_rate = lambda: 1.0

    def _kein_kurs(ticker):
        raise RuntimeError("Kursabruf im Test deaktiviert")
    portfolio.get_price_in_eur = _kein_kurs


# ─────────────────────────────────────────────
# Instrumentierung: RLS-Kontext je Thread VOR pin_user_context() und bei jeder
# get_session() aus dashboard.py heraus mitschreiben
# ─────────────────────────────────────────────

# Threads werden über eine eigene Seriennummer PRO THREAD-OBJEKT unterschieden,
# nicht über threading.get_ident(): Python vergibt Idents nach Thread-Ende neu
# (erster Testlauf: 4 Runs, 4 verschiedene Thread-Objekte, aber 1 Ident).
PIN_LOG = []       # (thread_serial, ctx_vor_pin, gepinnte_user_id)
SESSION_LOG = []   # (thread_serial, ctx_bei_get_session)
_serial = itertools.count(1)


def _thread_serial() -> int:
    t = threading.current_thread()
    if not hasattr(t, "_dashctx_serial"):
        t._dashctx_serial = next(_serial)
    return t._dashctx_serial
_orig_pin = database.pin_user_context
_orig_get_session = database.get_session


def _pin_recording(user_id):
    PIN_LOG.append((_thread_serial(), database._current_user_ctx.get(), user_id))
    _orig_pin(user_id)


@contextmanager
def _get_session_recording():
    SESSION_LOG.append((_thread_serial(), database._current_user_ctx.get()))
    with _orig_get_session() as session:
        yield session


database.pin_user_context = _pin_recording
database.get_session = _get_session_recording


# ─────────────────────────────────────────────
# Setup: FORCE RLS + Testnutzer mit eigenen Daten
# ─────────────────────────────────────────────

_DIREKT = ["pos_portfolios", "pos_target_weights", "pos_goals", "pos_investment_preferences",
           "pos_tax_config", "pos_tax_events", "pos_real_estate", "pos_rebalancing_proposals",
           "pos_daily_snapshots", "pos_buchungen", "pos_kategorisierungsregeln"]
_UEBER_PORTFOLIO = ["pos_positions", "pos_transactions"]
_CTX = "NULLIF(current_setting('app.current_user_id', true), '')::integer"


def enable_force_rls():
    ddl = []
    for t in _DIREKT + _UEBER_PORTFOLIO:
        ddl += [f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY", f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
                f"DROP POLICY IF EXISTS user_isolation ON {t}"]
        if t in _DIREKT:
            ddl.append(f"CREATE POLICY user_isolation ON {t} USING (user_id = {_CTX})")
        else:
            ddl.append(f"CREATE POLICY user_isolation ON {t} USING ("
                       f"portfolio_id IN (SELECT id FROM pos_portfolios WHERE user_id = {_CTX}))")
    with database.engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))


def make_user(tag: str, rolle: str, access: bool) -> dict:
    with _orig_get_session() as session:
        u = database.PosUser(name=f"{tag}-{RUN_ID}", email=f"dash-{tag.lower()}-{RUN_ID}@example.com",
                             rolle=rolle, status="active", portfolio_os_access=access,
                             trading_bot_access=False, onboarding_completed=True)
        session.add(u)
        session.flush()
        uid = u.id
    marker = {"pf": f"PF-{tag}-{RUN_ID}", "immo": f"IMMO-{tag}-{RUN_ID}", "empf": f"EMPF-{tag}-{RUN_ID}"}
    with database.user_context(uid):
        with _orig_get_session() as session:
            session.add(database.PosPortfolio(user_id=uid, name=marker["pf"], typ="depot"))
            session.add(database.PosRealEstate(user_id=uid, adresse=marker["immo"], kaufpreis=100000.0))
        database.save_buchungen(uid, [{"datum": date.today().replace(day=1).isoformat(), "betrag": 12.5,
                                       "empfaenger": marker["empf"], "typ": "ausgabe",
                                       # mit Kategorie: ohne crasht der Haushaltsbuch-Chart (px.bar mit
                                       # leerer Kategorie-Summe) -- vorbestehend, nicht Teil von Chunk 4
                                       "kategorie": "Sonstiges"}])
    return {"id": uid, "tag": tag, **marker}


def owner_of_portfolio(name: str):
    """Als Superuser-ähnlicher Blick ohne RLS: über psql als postgres."""
    import subprocess
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-At", "-d", database.engine.url.database,
                        "-c", f"SELECT user_id FROM pos_portfolios WHERE name = '{name}'"],
                       capture_output=True, text=True)
    return r.stdout.strip()


# ─────────────────────────────────────────────
# Helfer für AppTest
# ─────────────────────────────────────────────

def run_dashboard(user_id, at: AppTest | None = None) -> AppTest:
    config.DASHBOARD_USER_ID = user_id
    at = at or AppTest.from_file(DASHBOARD, default_timeout=TIMEOUT)
    at.run()
    return at


def rendered_text(at: AppTest) -> str:
    teile = []
    for kind in ("title", "header", "subheader", "markdown", "caption", "info", "warning", "error",
                 "success", "text", "code"):
        teile += [str(e.value) for e in getattr(at, kind)]
    for m in at.metric:
        teile += [str(m.label), str(m.value)]
    for sb in at.selectbox:
        teile += [str(sb.label)] + [str(o) for o in sb.options]
    for df in at.dataframe:
        teile.append(df.value.to_string())
    teile += [str(e.label) for e in at.expander]
    teile += [str(t.label) for t in at.tabs]
    return "\n".join(teile)


def sees(at: AppTest, user: dict) -> dict:
    t = rendered_text(at)
    return {k: user[k] in t for k in ("pf", "immo", "empf")}


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

def test_fail_closed(member: dict):
    at = run_dashboard(None)
    record("Ohne DASHBOARD_USER_ID: st.error mit Hinweis",
           any("DASHBOARD_USER_ID" in str(e.value) for e in at.error), [str(e.value)[:80] for e in at.error])
    record("Ohne DASHBOARD_USER_ID: st.stop() -- keine Tabs, keine Exception",
           len(at.tabs) == 0 and not at.exception, f"tabs={len(at.tabs)}, exc={len(at.exception)}")

    at = run_dashboard(member["id"])
    record("Nicht-Admin als DASHBOARD_USER_ID: abgelehnt (st.error, keine Tabs, keine Daten)",
           any("kein aktiver Admin" in str(e.value) for e in at.error) and len(at.tabs) == 0
           and not any(sees(at, member).values()), [str(e.value)[:90] for e in at.error])

    at = run_dashboard(999999999)
    record("Nicht existierende DASHBOARD_USER_ID: abgelehnt",
           any("existiert nicht" in str(e.value) for e in at.error) and len(at.tabs) == 0)


def test_only_own_data_and_no_selector(daniel: dict, other: dict, admin2: dict):
    at = run_dashboard(daniel["id"])
    record("Dashboard rendert ohne Exception unter FORCE RLS", not at.exception,
           [str(e.value)[:200] for e in at.exception])
    labels = [t.label for t in at.tabs]
    record("8 Tabs, kein Familie-Tab", len(labels) == 8 and not any("Familie" in l for l in labels), labels)
    eigen = sees(at, daniel)
    record("Eigene Daten sichtbar (Portfolio, Immobilie, Buchung)", all(eigen.values()), eigen)
    for fremd in (other, admin2):
        f = sees(at, fremd)
        record(f"Daten von {fremd['tag']} NICHT sichtbar", not any(f.values()), f)
    record("Kein Nutzerwähler ('Portfolio von') und kein Familien-Schalter",
           not any(sb.label == "Portfolio von" for sb in at.selectbox) and len(at.toggle) == 0
           and "Familien-Portfolio" not in rendered_text(at),
           f"selectboxes={[sb.label for sb in at.selectbox]}, toggles={len(at.toggle)}")
    record("Keine Nutzerverwaltung (kein 'Nutzer verwalten', kein Rollen-Wähler)",
           not any(s.value == "Nutzer verwalten" for s in at.subheader)
           and not any(sb.label == "Rolle" for sb in at.selectbox))
    return at


def test_write_in_fixed_context(at: AppTest, daniel: dict):
    neu = f"PF-NEU-{RUN_ID}"
    at.text_input(key="pf_name").set_value(neu)
    knopf = [b for b in at.button if b.label == "Anlegen" and b.proto.form_id == "neues_portfolio"]
    record("Formular 'Neues Portfolio' gefunden", len(knopf) == 1, [(b.label, b.proto.form_id) for b in at.button][:8])
    if not knopf:
        return
    knopf[0].click()
    config.DASHBOARD_USER_ID = daniel["id"]
    at.run()
    record("Portfolio über Dashboard angelegt, ohne RLS-Fehler", not at.exception,
           [str(e.value)[:200] for e in at.exception])
    record("Neues Portfolio gehört dem Dashboard-Nutzer (festgelegter Kontext)",
           owner_of_portfolio(neu) == str(daniel["id"]), f"user_id={owner_of_portfolio(neu)!r}")


def test_isolation_sequential(daniel: dict, admin2: dict):
    PIN_LOG.clear()
    SESSION_LOG.clear()
    main = _thread_serial()
    ausgabe = io.StringIO()
    with redirect_stdout(ausgabe):
        a = run_dashboard(daniel["id"])
        b = run_dashboard(admin2["id"])
        a = run_dashboard(daniel["id"], a)   # Rerun derselben Session A
        b = run_dashboard(admin2["id"], b)   # Rerun derselben Session B
    record("Keine 'fremder Kontext'-Warnung von pin_user_context() in den 4 Runs",
           "fremden Kontext" not in ausgabe.getvalue())
    record("4 Runs (2 Sessions × 2) -> 4 pin_user_context()-Aufrufe", len(PIN_LOG) == 4, len(PIN_LOG))
    threads = [t for t, _, _ in PIN_LOG]
    record("Jeder Run in einem eigenen, neuen Thread (nicht der Test-Hauptthread)",
           len(set(threads)) == 4 and main not in threads, f"{len(set(threads))} verschiedene Threads")
    record("RLS-Kontext VOR pin_user_context() in jedem Run leer (kein Übertrag aus früheren Runs/Sessions)",
           all(vor is None for _, vor, _ in PIN_LOG), [vor for _, vor, _ in PIN_LOG])
    record("Gepinnte IDs entsprechen der jeweiligen Session (A, B, A, B)",
           [u for _, _, u in PIN_LOG] == [daniel["id"], admin2["id"], daniel["id"], admin2["id"]],
           [u for _, _, u in PIN_LOG])
    gepinnt = {t: u for t, _, u in PIN_LOG}
    falsch = [(t, c) for t, c in SESSION_LOG if t in gepinnt and c not in (None, gepinnt[t])]
    nach_pin = [(t, c) for t, c in SESSION_LOG if t in gepinnt and c == gepinnt[t]]
    record("Jede get_session() aus dashboard.py lief mit dem Kontext IHRES Threads (nie dem einer anderen Session)",
           not falsch and len(nach_pin) > 0, f"falsch={falsch[:3]}, korrekt={len(nach_pin)}")
    ohne = [(t, c) for t, c in SESSION_LOG if t in gepinnt and c is None]
    record("Einzige kontextlose get_session() je Run ist die Nutzerprüfung vor dem Pin (pos_users, ohne RLS)",
           len(ohne) == 4, f"kontextlos={len(ohne)}")
    for name, at, eigen, fremd in (("A", a, daniel, admin2), ("B", b, admin2, daniel)):
        e, f = sees(at, eigen), sees(at, fremd)
        record(f"Session {name} (Rerun) sieht nur eigene Daten", all(e.values()) and not any(f.values()),
               f"eigen={e}, fremd={f}")
    record("Kontext des Test-Hauptthreads nach den Runs weiterhin None", database._current_user_ctx.get() is None)


def test_pin_warning(daniel: dict, admin2: dict):
    """Die Warnung selbst, in frischen Threads (wie ein Script-Run)."""
    ergebnis = {}

    def fall(name, vorher):
        buf = io.StringIO()
        with redirect_stdout(buf):
            if vorher is not None:
                database._current_user_ctx.set(vorher)
            _orig_pin(daniel["id"])
        ergebnis[name] = ("fremden Kontext" in buf.getvalue(), database._current_user_ctx.get())

    for name, vorher in (("leer", None), ("gleich", daniel["id"]), ("fremd", admin2["id"])):
        t = threading.Thread(target=fall, args=(name, vorher))
        t.start()
        t.join()
    record("pin_user_context(): keine Warnung bei leerem Kontext", ergebnis["leer"] == (False, daniel["id"]),
           ergebnis["leer"])
    record("pin_user_context(): keine Warnung bei gleichem Kontext", ergebnis["gleich"] == (False, daniel["id"]),
           ergebnis["gleich"])
    record("pin_user_context(): Warnung bei FREMDEM Kontext (und setzt trotzdem den neuen)",
           ergebnis["fremd"] == (True, daniel["id"]), ergebnis["fremd"])


def test_isolation_concurrent(daniel: dict, admin2: dict):
    """Ein Dashboard-Run (Kontext daniel) läuft gleichzeitig mit einem Thread,
    der fest auf admin2 gepinnt ist, und einem Thread ganz ohne Kontext."""
    stop = threading.Event()
    befunde = {"x_fremd": 0, "x_ok": 0, "leer_sah_etwas": 0, "leer_ok": 0, "fehler": []}

    def gepinnt_auf_admin2():
        try:
            _orig_pin(admin2["id"])
            while not stop.is_set():
                with _orig_get_session() as session:
                    namen = {n for (n,) in session.execute(text("SELECT name FROM pos_portfolios"))}
                if daniel["pf"] in namen:
                    befunde["x_fremd"] += 1
                elif admin2["pf"] in namen:
                    befunde["x_ok"] += 1
        except Exception as e:  # noqa: BLE001
            befunde["fehler"].append(repr(e))

    def ohne_kontext():
        try:
            while not stop.is_set():
                assert database._current_user_ctx.get() is None
                with _orig_get_session() as session:
                    n = session.execute(text("SELECT count(*) FROM pos_portfolios")).scalar()
                befunde["leer_sah_etwas" if n else "leer_ok"] += 1
        except Exception as e:  # noqa: BLE001
            befunde["fehler"].append(repr(e))

    threads = [threading.Thread(target=gepinnt_auf_admin2), threading.Thread(target=ohne_kontext)]
    for t in threads:
        t.start()
    try:
        ergebnisse = [run_dashboard(daniel["id"]) for _ in range(3)]
    finally:
        stop.set()
        for t in threads:
            t.join()
    record("Nebenläufig: 3 Dashboard-Runs (Kontext A) ohne Exception, nur eigene Daten",
           all(not at.exception and all(sees(at, daniel).values()) and not any(sees(at, admin2).values())
               for at in ergebnisse))
    record("Nebenläufig: auf B gepinnter Thread sah nie A's Daten",
           befunde["x_fremd"] == 0 and befunde["x_ok"] > 0, {k: befunde[k] for k in ("x_ok", "x_fremd")})
    record("Nebenläufig: Thread ohne Kontext erbte nichts und sah unter FORCE RLS 0 Zeilen",
           befunde["leer_sah_etwas"] == 0 and befunde["leer_ok"] > 0,
           {k: befunde[k] for k in ("leer_ok", "leer_sah_etwas")})
    record("Nebenläufig: keine Fehler in den Hintergrund-Threads", not befunde["fehler"], befunde["fehler"][:2])
    record("Kontext des Test-Hauptthreads weiterhin None", database._current_user_ctx.get() is None)


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, streamlit {__import__('streamlit').__version__}, RUN_ID={RUN_ID}\n")
    database.init_db()
    enable_force_rls()
    _stub_network()

    daniel = make_user("DANIEL", "admin", True)
    other = make_user("OTHER", "member", True)
    admin2 = make_user("ADMIN2", "admin", True)

    test_fail_closed(other)
    print()
    at = test_only_own_data_and_no_selector(daniel, other, admin2)
    print()
    test_write_in_fixed_context(at, daniel)
    print()
    test_isolation_sequential(daniel, admin2)
    print()
    test_pin_warning(daniel, admin2)
    print()
    test_isolation_concurrent(daniel, admin2)

    print("\n=== ZUSAMMENFASSUNG ===")
    fehlgeschlagen = [r for r in RESULTS if not r[1]]
    for name, ok, _ in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(fehlgeschlagen)}/{len(RESULTS)} Checks bestanden.")
    if fehlgeschlagen:
        sys.exit(1)
