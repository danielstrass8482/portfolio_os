"""
test_product_scope.py – Test für die Produkt-Scope-Trennung (2026-08-21):
PosUser.trading_bot_access / portfolio_os_access, durchgesetzt über
api.py require_portfolio_os_access (Router-Dependency auf `protected`).

Kontext: pos_users ist die gemeinsame Identitätstabelle für trading_bot UND
portfolio_os (trading_bot hat keine eigene User-Tabelle, /api/auth/ +
/api/user/ werden von der Trading-Bot-Domain per nginx auf dieses api.py
durchgereicht). Bis eben bekam JEDE registrierte Person automatisch vollen
portfolio_os-Zugriff, unabhängig davon wofür sie sich registriert hatte
(siehe Diagnose zu pos_users id=9/Dana, 2026-08-21) – dieser Test verifiziert
die Trennung mit zwei Accounts, die je nur EINE der beiden Berechtigungen
haben.

Deckt ab:
  1. Nutzer mit NUR trading_bot_access darf NICHT auf ein
     portfolio_os-spezifisches Endpoint zugreifen (403 erwartet).
  2. Derselbe Nutzer darf weiterhin ein geteiltes Endpoint (/api/user/*)
     erreichen (kein 403 nur wegen fehlendem portfolio_os_access).
  3. Nutzer mit NUR portfolio_os_access darf auf das portfolio_os-spezifische
     Endpoint zugreifen (200) – belegt, dass die beiden Flags unabhängig
     voneinander wirken (fehlendes trading_bot_access blockiert NICHT den
     portfolio_os-Zugriff).
  4. Registrierung über die portfolio_os-Origin setzt die Flags spiegelbildlich
     zur Trading-Bot-Origin (_is_portfolio_os_signup).

Kein Test für "trading-bot-spezifisches Endpoint blockiert portfolio_os-only-
Nutzer" (siehe Auftrag Teil 3 Punkt 8, "umgekehrt genauso"): innerhalb von
api.py/portfolio_os gibt es kein solches Endpoint – die eigentliche
Trading-Bot-Geschäftslogik läuft in trading_api.py (separates Repo/Service,
port 8504, live Handelskonto), das hier bewusst NICHT angefasst wurde (siehe
Bericht). trading_bot_access ist dort vorbereitet, aber noch nicht
durchgesetzt.

KEINE Produktions-DB, KEIN echter Mailversand (ALERT_EMAIL/SMTP_* bleiben
leer -> notifier.send_email loggt nur, siehe notifier.py): DATABASE_URL zeigt
auf eine eigene Wegwerf-Postgres-DB (analog test_password_reset.py):

    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER scopetest_tmp WITH PASSWORD 'scopetest_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_scopetest OWNER scopetest_tmp;"
    DATABASE_URL=postgresql://scopetest_tmp:scopetest_tmp_pw@localhost:5432/portfolio_os_scopetest \
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \
    python3 test_product_scope.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_scopetest;"
    sudo -u postgres psql -c "DROP USER scopetest_tmp;"
"""
import os
import sys
import traceback

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://scopetest_tmp:scopetest_tmp_pw@localhost:5432/portfolio_os_scopetest",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")  # bewusst leer -> notifier loggt nur, kein echter Mailversand

import api  # noqa: E402
import database  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

RESULTS = []
# base_url MUSS https sein: api.py setzt das Login-Cookie mit Secure=true
# (richtig für Produktion), httpx' Cookie-Jar verschickt Secure-Cookies aber
# nicht über http://testserver (TestClient-Default) -- jeder Request nach dem
# Login würde sonst mit 401 scheitern, obwohl der Login selbst 200 lieferte.
client = TestClient(api.app, base_url="https://testserver")
pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def make_user(email: str, *, trading_bot_access: bool, portfolio_os_access: bool,
              password: str = "TestPassword123") -> None:
    with database.get_session() as session:
        session.query(database.PosUser).filter_by(email=email).delete()
    with database.get_session() as session:
        user = database.PosUser(
            name="Scope Test", email=email, password_hash=pwd_context.hash(password),
            rolle="member", status="active",
            trading_bot_access=trading_bot_access, portfolio_os_access=portfolio_os_access,
        )
        session.add(user)


def login(email: str, password: str = "TestPassword123") -> None:
    res = client.post("/api/auth/login", data={"username": email, "password": password})
    assert res.status_code == 200, f"Login fehlgeschlagen: {res.status_code} {res.text}"


def test_trading_bot_only_blocked_from_portfolio_os_endpoint():
    api.limiter.reset()
    email = "tb-only@example.com"
    make_user(email, trading_bot_access=True, portfolio_os_access=False)
    login(email)

    res = client.get("/api/positions")
    record(
        "trading_bot_access=true/portfolio_os_access=false -> GET /api/positions liefert 403",
        res.status_code == 403, f"status={res.status_code}, body={res.text}",
    )

    res2 = client.get("/api/user/alpaca-status")
    record(
        "derselbe Nutzer erreicht weiterhin das geteilte Endpoint /api/user/alpaca-status (kein 403)",
        res2.status_code == 200, f"status={res2.status_code}, body={res2.text}",
    )


def test_portfolio_os_only_allowed_despite_no_trading_bot_access():
    api.limiter.reset()
    email = "po-only@example.com"
    make_user(email, trading_bot_access=False, portfolio_os_access=True)
    login(email)

    res = client.get("/api/positions")
    record(
        "trading_bot_access=false/portfolio_os_access=true -> GET /api/positions liefert 200 "
        "(Flags wirken unabhängig voneinander)",
        res.status_code == 200, f"status={res.status_code}, body={res.text}",
    )

    res2 = client.get("/api/user/alpaca-status")
    record(
        "derselbe Nutzer erreicht ebenfalls das geteilte Endpoint /api/user/alpaca-status",
        res2.status_code == 200, f"status={res2.status_code}, body={res2.text}",
    )


def test_registration_origin_sets_flags_symmetrically():
    api.limiter.reset()
    with database.get_session() as session:
        session.query(database.PosUser).filter_by(email="signup-tb@example.com").delete()
        session.query(database.PosUser).filter_by(email="signup-po@example.com").delete()

    # Trading-Bot-Origin (app.ai-tradingbot.de) -- der aktuell einzige echte Signup-Weg.
    res_tb = client.post(
        "/api/auth/register",
        json={"name": "TB Signup", "email": "signup-tb@example.com", "password": "TestPassword123", "reason": ""},
        headers={"Origin": "https://app.ai-tradingbot.de"},
    )
    record("Registrierung von app.ai-tradingbot.de -> 200", res_tb.status_code == 200,
           f"status={res_tb.status_code}, body={res_tb.text}")
    with database.get_session() as session:
        u = session.query(database.PosUser).filter_by(email="signup-tb@example.com").first()
        record("Trading-Bot-Origin -> trading_bot_access=true", u is not None and u.trading_bot_access is True)
        record("Trading-Bot-Origin -> portfolio_os_access=false", u is not None and u.portfolio_os_access is False)

    # portfolio_os-Origin (aktuell keine echte Signup-UI dafür, siehe Modultext -- nur die Erkennung selbst).
    res_po = client.post(
        "/api/auth/register",
        json={"name": "PO Signup", "email": "signup-po@example.com", "password": "TestPassword123", "reason": ""},
        headers={"Origin": "https://portfolio.diestraesschens.de"},
    )
    record("Registrierung von portfolio.diestraesschens.de -> 200", res_po.status_code == 200,
           f"status={res_po.status_code}, body={res_po.text}")
    with database.get_session() as session:
        u = session.query(database.PosUser).filter_by(email="signup-po@example.com").first()
        record("portfolio_os-Origin -> trading_bot_access=false", u is not None and u.trading_bot_access is False)
        record("portfolio_os-Origin -> portfolio_os_access=true", u is not None and u.portfolio_os_access is True)


def main():
    database.init_db()
    for fn in (
        test_trading_bot_only_blocked_from_portfolio_os_endpoint,
        test_portfolio_os_only_allowed_despite_no_trading_bot_access,
        test_registration_origin_sets_flags_symmetrically,
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
