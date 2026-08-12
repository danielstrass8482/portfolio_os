"""
test_change_password.py – Test für POST /api/auth/change-password
(Backend, 2026-08-12): Passwort ändern für einen bereits eingeloggten Nutzer.

Deckt ab:
  1. Erfolgreicher Wechsel mit korrektem aktuellem Passwort - danach Login
     mit dem alten Passwort schlägt fehl, mit dem neuen funktioniert.
  2. Ablehnung bei falschem aktuellem Passwort (400), Passwort bleibt
     unverändert (Login mit dem echten alten Passwort funktioniert weiterhin).
  3. Zu kurzes neues Passwort wird abgelehnt (dieselbe Mindestlänge wie
     Register/Reset, siehe _require_strong_password).
  4. Fehlender/ungültiger Token -> 401 (kein Zugriff ohne Login).
  5. Rate-Limiting (5/Minute) greift bei wiederholten Versuchen.

KEINE Produktions-DB, KEIN echter Mailversand (identische Wegwerf-DB-
Konvention wie test_password_reset.py):

    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER pwchange_tmp WITH PASSWORD 'pwchange_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_pwchange_test OWNER pwchange_tmp;"
    DATABASE_URL=postgresql://pwchange_tmp:pwchange_tmp_pw@localhost:5432/portfolio_os_pwchange_test \
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \
    python3 test_change_password.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_pwchange_test;"
    sudo -u postgres psql -c "DROP USER pwchange_tmp;"
"""
import os
import sys
import traceback

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://pwchange_tmp:pwchange_tmp_pw@localhost:5432/portfolio_os_pwchange_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")  # bewusst leer -> notifier loggt nur, kein echter Mailversand

import api  # noqa: E402
import database  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

RESULTS = []
client = TestClient(api.app)
pwd_context_test = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def make_user(email: str, password: str = "OldPassword123") -> int:
    with database.get_session() as session:
        session.query(database.PosUser).filter_by(email=email).delete()
        session.commit()
    with database.get_session() as session:
        user = database.PosUser(
            name="Test Nutzer", email=email, password_hash=pwd_context_test.hash(password),
            rolle="member", status="active",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def auth_headers(user_id: int) -> dict:
    token = api.create_access_token({"sub": str(user_id)})
    return {"Authorization": f"Bearer {token}"}


def login_ok(email: str, password: str) -> bool:
    res = client.post("/api/auth/login", data={"username": email, "password": password})
    return res.status_code == 200


def test_successful_change():
    api.limiter.reset()
    email = "change-ok@example.com"
    uid = make_user(email, password="OldPassword123")

    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "OldPassword123", "new_password": "NewPassword456"},
        headers=auth_headers(uid),
    )
    record("change-password mit korrektem aktuellem Passwort -> 200",
           res.status_code == 200, f"status={res.status_code}, body={res.text}")

    record("Login mit NEUEM Passwort funktioniert danach", login_ok(email, "NewPassword456"))
    record("Login mit ALTEM Passwort funktioniert NICHT mehr", not login_ok(email, "OldPassword123"))


def test_wrong_current_password_rejected():
    api.limiter.reset()
    email = "change-wrong-current@example.com"
    uid = make_user(email, password="OldPassword123")

    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "TotallyWrongPassword", "new_password": "NewPassword456"},
        headers=auth_headers(uid),
    )
    record("change-password mit FALSCHEM aktuellem Passwort -> 400",
           res.status_code == 400, f"status={res.status_code}, body={res.text}")

    record("Passwort blieb unverändert (Login mit dem echten alten Passwort funktioniert weiterhin)",
           login_ok(email, "OldPassword123"))
    record("Login mit dem abgelehnten 'neuen' Passwort funktioniert NICHT",
           not login_ok(email, "NewPassword456"))


def test_weak_new_password_rejected():
    api.limiter.reset()
    email = "change-weak@example.com"
    uid = make_user(email, password="OldPassword123")

    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "OldPassword123", "new_password": "short"},
        headers=auth_headers(uid),
    )
    record("zu kurzes neues Passwort wird abgelehnt (400)",
           res.status_code == 400, f"status={res.status_code}, body={res.text}")
    record("Passwort blieb unverändert (altes Passwort funktioniert weiterhin)",
           login_ok(email, "OldPassword123"))


def test_unauthenticated_rejected():
    api.limiter.reset()
    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "irrelevant", "new_password": "NewPassword456"},
    )
    record("kein Token -> 401", res.status_code == 401, f"status={res.status_code}")

    res2 = client.post(
        "/api/auth/change-password",
        json={"current_password": "irrelevant", "new_password": "NewPassword456"},
        headers={"Authorization": "Bearer offensichtlich-kein-jwt"},
    )
    record("kaputter Token -> 401", res2.status_code == 401, f"status={res2.status_code}")


def test_rate_limiting():
    api.limiter.reset()
    email = "change-ratelimit@example.com"
    uid = make_user(email, password="OldPassword123")
    headers = auth_headers(uid)

    statuses = []
    for _ in range(7):
        res = client.post(
            "/api/auth/change-password",
            json={"current_password": "TotallyWrongPassword", "new_password": "NewPassword456"},
            headers=headers,
        )
        statuses.append(res.status_code)

    record("nach 5 erlaubten Anfragen/Minute greift Rate-Limiting (429) bei weiteren Versuchen",
           429 in statuses, f"statuses={statuses}")


def main():
    for fn in (
        test_successful_change,
        test_wrong_current_password_rejected,
        test_weak_new_password_rejected,
        test_unauthenticated_rejected,
        test_rate_limiting,
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
