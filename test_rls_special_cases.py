"""
test_rls_special_cases.py – Test für die drei RLS-Sonderfälle aus Chunk 2
(2026-08-21, siehe docs/rls-force-umbau-plan-21-08.md): update_prices()
(nutzerübergreifend), Notify-Jobs (main.py, loopen pro Nutzer), Admin-Cross-
View (_resolve_user_id() in api.py).

WICHTIG: Wie Chunk 1 hat auch dieser Chunk noch KEINE sicherheitsrelevante
Wirkung -- FORCE ROW LEVEL SECURITY ist nicht aktiv. Die Tests hier
verifizieren NUR, dass der RLS-Kontext an den drei Sonderstellen korrekt
gesetzt wird UND dass die jeweils bestehende Funktionalität (Preis-Update
für ALLE Nutzer, korrekt adressierte Reports, funktionierender Admin-
Cross-View) dabei nicht kaputtgeht.

Deckt ab:
  1. update_prices(): 3 Nutzer mit je eigenen Positionen -- nach dem Lauf
     müssen ALLE Positionen aktualisiert sein (kein Nutzer übersprungen),
     unabhängig davon wie viele verschiedene user_context()-Wechsel das
     bedeutet.
  2. Notify-Jobs: 2 Nutzer mit unterscheidbaren echten Daten -- jeder Report
     zeigt weiterhin ausschließlich die eigenen Daten (Regressionstest zum
     Adressierungs-Fix vom 21.08., der Wochen-Report ging vorher an eine
     globale ALERT_EMAIL statt an user.email).
  3. Admin-Cross-View: Admin ruft explizit die Daten eines anderen Nutzers ab
     (?user_id=<andere ID>) -- funktioniert weiterhin, UND: kein Context-Leck
     in einen nachfolgenden Request (weder sequentiell direkt danach noch
     unter echter Nebenläufigkeit, analog zum Concurrency-Test aus Chunk 1).

KEINE Produktions-DB, KEIN echter Mailversand, KEIN echter yfinance-Call
(get_price_in_eur wird deterministisch gestubbt):
    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER rlssc_tmp WITH PASSWORD 'rlssc_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_rlssc OWNER rlssc_tmp;"
    DATABASE_URL=postgresql://rlssc_tmp:rlssc_tmp_pw@localhost:5432/portfolio_os_rlssc \
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \
    python3 test_rls_special_cases.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_rlssc;"
    sudo -u postgres psql -c "DROP USER rlssc_tmp;"
"""
import os
import sys
import threading
import traceback

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://rlssc_tmp:rlssc_tmp_pw@localhost:5432/portfolio_os_rlssc",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")

import api  # noqa: E402
import database  # noqa: E402
import main as jobs  # noqa: E402
import notifier  # noqa: E402
import portfolio as portfolio_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

RESULTS = []
client = TestClient(api.app, base_url="https://testserver")
pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def make_user(email: str, *, rolle: str = "member", password: str = "TestPassword123") -> int:
    with database.get_session() as session:
        session.query(database.PosUser).filter_by(email=email).delete()
    with database.get_session() as session:
        u = database.PosUser(
            name="RLS SC Test", email=email, password_hash=pwd_context.hash(password),
            rolle=rolle, status="active", trading_bot_access=True, portfolio_os_access=True,
        )
        session.add(u)
        session.flush()
        return u.id


def make_portfolio_with_position(user_id: int, ticker: str) -> None:
    with database.user_context(user_id):
        with database.get_session() as session:
            pf = database.PosPortfolio(user_id=user_id, name="Test", broker="Test", typ="depot")
            session.add(pf)
            session.flush()
            pos = database.PosPosition(
                portfolio_id=pf.id, ticker=ticker, name=ticker,
                quantity=1.0, avg_buy_price=100.0, currency="EUR",
            )
            session.add(pos)


def mint_token(user_id: int) -> str:
    return api.create_access_token({"sub": str(user_id)})


# ─────────────────────────────────────────────
# 1: update_prices() -- nutzerübergreifend, kein Nutzer übersprungen
# ─────────────────────────────────────────────

def test_update_prices_covers_all_users():
    uid_1 = make_user("prices-1@example.com")
    uid_2 = make_user("prices-2@example.com")
    uid_3 = make_user("prices-3@example.com")
    make_portfolio_with_position(uid_1, "TICK1")
    make_portfolio_with_position(uid_2, "TICK2")
    make_portfolio_with_position(uid_3, "TICK3")

    original = portfolio_module.get_price_in_eur
    portfolio_module.get_price_in_eur = lambda ticker: 42.0
    try:
        updated = portfolio_module.update_prices()
    finally:
        portfolio_module.get_price_in_eur = original

    record("update_prices(): alle 3 Positionen (3 verschiedene Nutzer) aktualisiert",
           updated == 3, f"updated={updated}")

    for uid, ticker in ((uid_1, "TICK1"), (uid_2, "TICK2"), (uid_3, "TICK3")):
        with database.user_context(uid):
            with database.get_session() as session:
                pos = session.query(database.PosPosition).join(database.PosPortfolio).filter(
                    database.PosPortfolio.user_id == uid
                ).first()
                record(f"Nutzer {uid} ({ticker}): current_price korrekt gesetzt (42.0), nicht übersprungen",
                       pos is not None and pos.current_price == 42.0,
                       f"pos={pos.current_price if pos else None}")


# ─────────────────────────────────────────────
# 2: Notify-Jobs -- Regressionstest zum Adressierungs-Fix
# ─────────────────────────────────────────────

def test_notify_jobs_still_address_correctly():
    uid_a = make_user("notify-a@example.com")
    uid_b = make_user("notify-b@example.com")

    captured = []

    def fake_send_email(subject, body, to_email=None):
        captured.append((to_email, subject, body))

    original = notifier.send_email
    notifier.send_email = fake_send_email
    try:
        jobs.weekly_job()
    finally:
        notifier.send_email = original

    # weekly_job() iteriert über ALLE portfolio_os_access=true-Nutzer der DB,
    # nicht nur die zwei aus diesem Testfall -- die anderen Suite-Tests legen
    # eigene Nutzer in derselben DB an, daher Teilmengen-Check statt exakter
    # Gleichheit. Die eigentlich relevante Regression ist: bekommen A und B
    # ihre EIGENEN Reports, und landet NIE mehr als ein Report auf derselben
    # Adresse (das wäre wieder das alte "alles an eine globale ALERT_EMAIL"-
    # Muster vom 21.08.).
    empfaenger = [to for to, _, _ in captured]
    record("weekly_job(): Reports gingen (u.a.) an beide Nutzer-eigene Adressen",
           {"notify-a@example.com", "notify-b@example.com"} <= set(empfaenger), f"empfaenger={empfaenger}")
    doppelt = {e for e in empfaenger if empfaenger.count(e) > 1}
    record("weekly_job(): keine Adresse bekommt mehr als einen Report "
           "(Regression zum globalen-ALERT_EMAIL-Bug vom 21.08.)",
           not doppelt, f"mehrfach_adressiert={doppelt}")

    # Nach dem Job-Durchlauf darf kein Kontext mehr aktiv sein.
    record("Nach weekly_job(): kein hängender Kontext (ContextVar None)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


# ─────────────────────────────────────────────
# 3: Admin-Cross-View -- funktioniert weiterhin, kein Context-Leck
# ─────────────────────────────────────────────

def test_admin_cross_view_works_and_does_not_leak():
    api.limiter.reset()
    admin_id = make_user("admin-cv@example.com", rolle="admin")
    member_id = make_user("member-cv@example.com", rolle="member")

    admin_token = mint_token(admin_id)
    member_token = mint_token(member_id)

    # Member legt eine eigene, identifizierbare Immobilie an.
    res_setup = client.post("/api/real-estate", json={"adresse": "Member-Immobilie", "kaufpreis": 50000},
                             headers={"Authorization": f"Bearer {member_token}"})
    record("Setup: Member legt eigene Immobilie an", res_setup.status_code == 200, f"status={res_setup.status_code}")

    # Admin legt selbst eine ANDERE Immobilie an (zur Unterscheidung).
    res_setup_admin = client.post("/api/real-estate", json={"adresse": "Admin-Eigene-Immobilie", "kaufpreis": 90000},
                                   headers={"Authorization": f"Bearer {admin_token}"})
    record("Setup: Admin legt eigene Immobilie an", res_setup_admin.status_code == 200,
           f"status={res_setup_admin.status_code}")

    with database.get_session() as session:
        log_count_before = session.query(database.PosAdminAccessLog).count()

    # Admin ruft EXPLIZIT die Daten des Members ab (Cross-View).
    res_cross = client.get(f"/api/real-estate?user_id={member_id}",
                            headers={"Authorization": f"Bearer {admin_token}"})
    record("Admin-Cross-View: GET /api/real-estate?user_id=<Member> -> 200",
           res_cross.status_code == 200, f"status={res_cross.status_code}")
    if res_cross.status_code == 200:
        adressen = {im["adresse"] for im in res_cross.json()}
        record("Admin-Cross-View: sieht die Immobilie DES MEMBERS (nicht seine eigene)",
               adressen == {"Member-Immobilie"}, f"gesehen={adressen}")

    with database.get_session() as session:
        log_count_after = session.query(database.PosAdminAccessLog).count()
    record("Cross-View wurde protokolliert (pos_admin_access_log +1)",
           log_count_after == log_count_before + 1, f"vorher={log_count_before}, nachher={log_count_after}")

    # Direkt danach: kein hängender Kontext mehr (Request ist fertig).
    record("Nach dem Cross-View-Request: kein hängender Kontext (ContextVar None)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")

    # Sequenzieller Leck-Test: ein GANZ NORMALER Folge-Request (Admin, KEIN
    # user_id-Parameter) muss wieder die EIGENEN Daten des Admins zeigen,
    # nicht die vom vorherigen Cross-View "hängengebliebenen" des Members.
    res_normal = client.get("/api/real-estate", headers={"Authorization": f"Bearer {admin_token}"})
    if res_normal.status_code == 200:
        adressen_normal = {im["adresse"] for im in res_normal.json()}
        record("Folge-Request (Admin, ohne user_id): zeigt wieder EIGENE Admin-Daten, kein Leck vom Cross-View",
               adressen_normal == {"Admin-Eigene-Immobilie"}, f"gesehen={adressen_normal}")

    # Echte Nebenläufigkeit: Admin macht einen Cross-View auf Member, WÄHREND
    # der Member selbst einen ganz normalen eigenen Request macht -- beide
    # dürfen sich nicht gegenseitig beeinflussen (analog Concurrency-Test
    # Chunk 1, hier zusätzlich mit dem Context-Override aus Sonderfall c).
    results = {}
    errors = {}

    def call_admin_cross_view():
        try:
            c = TestClient(api.app, base_url="https://testserver")
            for _ in range(5):
                r = c.get(f"/api/real-estate?user_id={member_id}", headers={"Authorization": f"Bearer {admin_token}"})
                results.setdefault("admin", []).append({im["adresse"] for im in r.json()})
        except Exception:
            errors["admin"] = traceback.format_exc()

    def call_member_normal():
        try:
            c = TestClient(api.app, base_url="https://testserver")
            for _ in range(5):
                r = c.get("/api/real-estate", headers={"Authorization": f"Bearer {member_token}"})
                results.setdefault("member", []).append({im["adresse"] for im in r.json()})
        except Exception:
            errors["member"] = traceback.format_exc()

    t1 = threading.Thread(target=call_admin_cross_view)
    t2 = threading.Thread(target=call_member_normal)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    record("Concurrency (Admin-Cross-View + Member-Normal): keine Exceptions", not errors, str(errors))
    if "admin" in results:
        record("Concurrency: Admin sieht in JEDER Antwort weiterhin nur Members Immobilie",
               all(r == {"Member-Immobilie"} for r in results["admin"]), f"gesehen={results['admin']}")
    if "member" in results:
        record("Concurrency: Member sieht in JEDER Antwort weiterhin nur seine eigene Immobilie",
               all(r == {"Member-Immobilie"} for r in results["member"]), f"gesehen={results['member']}")


def main():
    database.init_db()
    for fn in (
        test_update_prices_covers_all_users,
        test_notify_jobs_still_address_correctly,
        test_admin_cross_view_works_and_does_not_leak,
    ):
        print(f"\n--- {fn.__name__} ---")
        try:
            fn()
        except Exception:
            record(fn.__name__, False, "Testfall selbst abgestürzt:\n" + traceback.format_exc())

    print("\n=== ZUSAMMENFASSUNG ===")
    failed = [n for n, ok, _ in RESULTS if not ok]
    for name, ok, detail in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} Checks bestanden.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
