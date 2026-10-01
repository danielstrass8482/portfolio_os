"""
test_update_prices_rls.py – RLS-Umbau, Sonderfall a (2026-10-01, siehe
docs/rls-force-umbau-plan-21-08.md): Kursaktualisierung unter FORCE ROW LEVEL
SECURITY.

Vorher las portfolio.update_prices() zuerst ALLE Positionen in der Session des
Aufrufers und schrieb dann pro Besitzer. Der tägliche Job (notify-daily.timer
-> run_scheduled_job.py daily -> main.daily_job()) ruft ohne Nutzerkontext
auf -- unter FORCE RLS lieferte die Leseabfrage 0 Zeilen: 0 Positionen
aktualisiert, ohne Fehler (stille Verschlechterung). Jetzt:
  - update_prices(): nur der Nutzer im AKTIVEN Kontext (Dashboard, API),
    explizit gefiltert; ohne Kontext RuntimeError statt still 0.
  - update_prices_all_users(): System-Fall, alle Zeilen aus pos_users ohne
    Filter, pro Nutzer in dessen eigenem user_context(); verweigert den
    Aufruf aus einem aktiven Nutzerkontext.
  - main.daily_job() / main.update_all_prices() nutzen update_prices_all_users().

Deckt ab (Wegwerf-Postgres, FORCE RLS + Owner-Policies, Kursabruf gestubbt):
  1. Regression: der ALTE Code (portfolio.py aus Commit 1e78c7b bzw. OLD_PORTFOLIO_REF)
     aktualisiert unter FORCE RLS ohne Kontext 0 Positionen, ohne Fehler.
  2. update_prices_all_users(): Positionen beider Nutzer aktualisiert, jeder
     Kursabruf lief im Kontext des jeweiligen Besitzers.
  3. Leitplanken: update_prices() ohne Kontext und update_prices_all_users()
     mit aktivem Kontext werfen.
  4. update_prices() im Kontext von A: nur A's Positionen.
  5. main.daily_job() Ende-zu-Ende (Mail-Versand gestubbt): beide Nutzer.
  6. POST /api/positions/refresh-prices als A: nur A's Positionen, B's
     unverändert -- unter FORCE RLS UND ohne FORCE (heutiger Prod-Zustand,
     dank expliziter Filterung).

Mehrfach gegen dieselbe DB lauffähig (RUN_ID in E-Mails/Tickern).

Setup:
    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER upr_tmp WITH PASSWORD 'upr_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_upr OWNER upr_tmp;"
    DATABASE_URL=postgresql://upr_tmp:upr_tmp_pw@localhost:5432/portfolio_os_upr \\
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \\
    python3 test_update_prices_rls.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_upr;"
    sudo -u postgres psql -c "DROP OWNED BY upr_tmp; DROP ROLE upr_tmp;"
"""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import uuid
from contextlib import redirect_stdout

os.environ.setdefault("DATABASE_URL", "postgresql://upr_tmp:upr_tmp_pw@localhost:5432/portfolio_os_upr")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")

import api  # noqa: E402
import database  # noqa: E402
import main as jobs  # noqa: E402
import notifier  # noqa: E402
import portfolio  # noqa: E402
from database import text  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
RUN_ID = uuid.uuid4().hex[:8]
client = TestClient(api.app, base_url="https://testserver")
_FORCE_TABLES = ["pos_portfolios", "pos_positions", "pos_transactions", "pos_real_estate", "pos_daily_snapshots"]
_CTX = "NULLIF(current_setting('app.current_user_id', true), '')::integer"


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + str(detail)) if detail else ''}")


# ─────────────────────────────────────────────
# Kursabruf-Stub: deterministischer Preis, merkt sich den RLS-Kontext je Abruf
# ─────────────────────────────────────────────

KURS_LOG = []  # (ticker, aktiver_kontext)


def _fake_kurs(ticker):
    KURS_LOG.append((ticker, database.current_user_context()))
    return 123.45


portfolio.get_price_in_eur = _fake_kurs
notifier.send_daily_alert = lambda user_id: None   # kein Mail-Versand im Test


# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────

def set_force(an: bool):
    ddl = []
    for t in _FORCE_TABLES:
        ddl += [f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
                f"ALTER TABLE {t} {'FORCE' if an else 'NO FORCE'} ROW LEVEL SECURITY",
                f"DROP POLICY IF EXISTS user_isolation ON {t}"]
        if t in ("pos_positions", "pos_transactions"):
            ddl.append(f"CREATE POLICY user_isolation ON {t} USING ("
                       f"portfolio_id IN (SELECT id FROM pos_portfolios WHERE user_id = {_CTX}))")
        else:
            ddl.append(f"CREATE POLICY user_isolation ON {t} USING (user_id = {_CTX})")
    with database.engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))


def make_user(tag: str) -> dict:
    with database.get_session() as session:
        u = database.PosUser(name=f"UPR-{tag}-{RUN_ID}", email=f"upr-{tag.lower()}-{RUN_ID}@example.com",
                             rolle="member", status="active", portfolio_os_access=True,
                             trading_bot_access=False, onboarding_completed=True)
        session.add(u)
        session.flush()
        uid = u.id
    tickers = [f"{tag}{i}-{RUN_ID}" for i in (1, 2)]
    with database.user_context(uid):
        with database.get_session() as session:
            pf = database.PosPortfolio(user_id=uid, name=f"UPR-{tag}-{RUN_ID}", typ="depot")
            session.add(pf)
            session.flush()
            for t in tickers:
                session.add(database.PosPosition(portfolio_id=pf.id, ticker=t, quantity=1.0, avg_buy_price=100.0))
    return {"id": uid, "tag": tag, "tickers": tickers}


def preise(user: dict) -> dict:
    with database.user_context(user["id"]):
        with database.get_session() as session:
            return {p.ticker: p.current_price for p in session.query(database.PosPosition)
                    .filter(database.PosPosition.ticker.in_(user["tickers"])).all()}


def reset(*users):
    for u in users:
        with database.user_context(u["id"]):
            with database.get_session() as session:
                for p in session.query(database.PosPosition).filter(database.PosPosition.ticker.in_(u["tickers"])):
                    p.current_price = None
                    p.last_updated = None


def alle_gesetzt(user): return all(v == 123.45 for v in preise(user).values())
def alle_leer(user): return all(v is None for v in preise(user).values())


def load_old_portfolio():
    """portfolio.py vor dem Fix aus git (OLD_PORTFOLIO_REF, Default 1e78c7b) als
    eigenes Modul laden -- für den Vorher-Beweis."""
    ref = os.environ.get("OLD_PORTFOLIO_REF", "1e78c7b")  # letzter Stand VOR dem Fix
    src = subprocess.run(["git", "show", f"{ref}:portfolio.py"], capture_output=True, text=True, check=True,
                         cwd=os.path.dirname(os.path.abspath(__file__))).stdout
    if "def update_prices_all_users" in src:
        raise SystemExit(f"OLD_PORTFOLIO_REF={ref} enthält bereits den Fix -- für den Vorher-Beweis den "
                         "Commit VOR dem Fix angeben (z.B. OLD_PORTFOLIO_REF=1e78c7b).")
    path = os.path.join(tempfile.mkdtemp(), "portfolio_old.py")
    with open(path, "w") as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location("portfolio_old", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.get_price_in_eur = _fake_kurs
    return mod, ref


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────

def test_regression_old_code(a, b):
    old, ref = load_old_portfolio()
    reset(a, b)
    try:
        n = old.update_prices()
        fehler = None
    except Exception as e:  # noqa: BLE001
        n, fehler = None, repr(e)
    record(f"VORHER ({ref}): alter update_prices() ohne Kontext unter FORCE RLS -> 0 aktualisiert, ohne Fehler "
           f"(Bug reproduziert)", n == 0 and fehler is None, f"n={n}, fehler={fehler}")
    record("VORHER: Positionen von A und B blieben unverändert (still veraltet)",
           alle_leer(a) and alle_leer(b), f"A={preise(a)}, B={preise(b)}")


def test_all_users(a, b):
    reset(a, b)
    KURS_LOG.clear()
    n = portfolio.update_prices_all_users()
    record("NACHHER: update_prices_all_users() ohne Kontext unter FORCE RLS -> > 0 aktualisiert",
           n >= 4, f"n={n}")
    record("NACHHER: alle Positionen von A UND B aktualisiert", alle_gesetzt(a) and alle_gesetzt(b),
           f"A={preise(a)}, B={preise(b)}")
    falsch = [(t, ctx) for t, ctx in KURS_LOG
              if (t in a["tickers"] and ctx != a["id"]) or (t in b["tickers"] and ctx != b["id"])]
    gesehen = {t for t, _ in KURS_LOG}
    record("NACHHER: jeder Kursabruf lief im Kontext des jeweiligen Besitzers",
           not falsch and set(a["tickers"] + b["tickers"]) <= gesehen, f"falsch={falsch}")
    record("Kontext nach update_prices_all_users() wieder None", database.current_user_context() is None)


def test_guards(a):
    try:
        portfolio.update_prices()
        r1 = "kein Fehler"
    except RuntimeError as e:
        r1 = f"RuntimeError: {str(e)[:60]}"
    record("update_prices() ohne Kontext wirft (statt still 0)", r1.startswith("RuntimeError"), r1)
    with database.user_context(a["id"]):
        try:
            portfolio.update_prices_all_users()
            r2 = "kein Fehler"
        except RuntimeError as e:
            r2 = f"RuntimeError: {str(e)[:60]}"
    record("update_prices_all_users() aus aktivem Nutzerkontext wird verweigert", r2.startswith("RuntimeError"), r2)


def test_active_context_only(a, b):
    reset(a, b)
    with database.user_context(a["id"]):
        n = portfolio.update_prices()
    record("update_prices() im Kontext A: genau A's 2 Positionen", n == 2, f"n={n}")
    record("update_prices() im Kontext A: A aktualisiert, B unverändert",
           alle_gesetzt(a) and alle_leer(b), f"A={preise(a)}, B={preise(b)}")


def test_daily_job(a, b):
    reset(a, b)
    buf = io.StringIO()
    with redirect_stdout(buf):
        jobs.daily_job()
    zeile = [l for l in buf.getvalue().splitlines() if "Position(en) aktualisiert" in l]
    record("main.daily_job() (wie notify-daily.timer) aktualisiert unter FORCE RLS A UND B",
           alle_gesetzt(a) and alle_gesetzt(b), f"log={zeile}, A={preise(a)}, B={preise(b)}")
    record("main.daily_job() meldet > 0 aktualisierte Positionen",
           bool(zeile) and not zeile[0].strip().startswith("0 "), zeile)


def test_refresh_endpoint(a, b, force_label):
    reset(a, b)
    token = api.create_access_token({"sub": str(a["id"])})
    res = client.post("/api/positions/refresh-prices", headers={"Authorization": f"Bearer {token}"})
    record(f"[{force_label}] POST /api/positions/refresh-prices als A -> 200, aktualisiert=2",
           res.status_code == 200 and res.json().get("aktualisiert") == 2, f"status={res.status_code}, body={res.text}")
    record(f"[{force_label}] refresh-prices als A: nur A's Positionen, B's unverändert",
           alle_gesetzt(a) and alle_leer(b), f"A={preise(a)}, B={preise(b)}")


if __name__ == "__main__":
    database.init_db()
    api.limiter.reset()
    set_force(True)
    print(f"RUN_ID={RUN_ID}; FORCE RLS auf {', '.join(_FORCE_TABLES)} (nur diese Wegwerf-DB)\n")
    a, b = make_user("A"), make_user("B")

    test_regression_old_code(a, b)
    print()
    test_all_users(a, b)
    print()
    test_guards(a)
    print()
    test_active_context_only(a, b)
    print()
    test_daily_job(a, b)
    print()
    test_refresh_endpoint(a, b, "FORCE RLS")
    set_force(False)
    test_refresh_endpoint(a, b, "ohne FORCE, wie Prod heute")
    set_force(True)
    record("Kein hängender Kontext am Ende", database.current_user_context() is None)

    print("\n=== ZUSAMMENFASSUNG ===")
    fehlgeschlagen = [r for r in RESULTS if not r[1]]
    for name, ok, _ in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(fehlgeschlagen)}/{len(RESULTS)} Checks bestanden.")
    if fehlgeschlagen:
        sys.exit(1)
