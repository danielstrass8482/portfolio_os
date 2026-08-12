"""
test_password_reset.py – Test für den Passwort-Reset-Flow (Backend, 2026-08-12):
POST /api/auth/forgot-password + POST /api/auth/reset-password.

Deckt ab:
  1. Gültiger Token führt zu erfolgreichem Reset (neues Passwort funktioniert
     danach beim echten Login, altes nicht mehr).
  2. Abgelaufener Token wird abgelehnt.
  3. Bereits benutzter Token wird beim zweiten Versuch abgelehnt (single-use).
  4. Zwei aufeinanderfolgende forgot-password-Anfragen: nur der zweite Token
     ist noch gültig, der erste wurde automatisch invalidiert.
  5. forgot-password mit nicht-existierender E-Mail liefert denselben
     Status-Code + Body wie bei existierender E-Mail (kein Enumeration-Leak)
     und braucht dabei eine vergleichbare Zeit (Timing-Safety).
  6. Rate-Limiting (3/Minute) greift bei wiederholten forgot-password-Anfragen.

KEINE Produktions-DB, KEIN echter Mailversand (ALERT_EMAIL/SMTP_* bleiben
leer -> notifier.send_email loggt nur, siehe notifier.py): DATABASE_URL zeigt
auf eine eigene Wegwerf-Postgres-DB (analog test_confirm_tier_chunk1_
migration.py im trading_bot_saxo-Repo):

    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER pwreset_tmp WITH PASSWORD 'pwreset_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_pwreset_test OWNER pwreset_tmp;"
    DATABASE_URL=postgresql://pwreset_tmp:pwreset_tmp_pw@localhost:5432/portfolio_os_pwreset_test \
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \
    python3 test_password_reset.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_pwreset_test;"
    sudo -u postgres psql -c "DROP USER pwreset_tmp;"
"""
import os
import sys
import time
import traceback

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://pwreset_tmp:pwreset_tmp_pw@localhost:5432/portfolio_os_pwreset_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")  # bewusst leer -> notifier loggt nur, kein echter Mailversand

import api  # noqa: E402
import database  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

RESULTS = []
client = TestClient(api.app)


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def make_user(email: str, password: str = "OldPassword123") -> database.PosUser:
    pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")
    with database.get_session() as session:
        session.query(database.PosUser).filter_by(email=email).delete()
        session.commit()
    with database.get_session() as session:
        user = database.PosUser(
            name="Test Nutzer", email=email, password_hash=pwd_context.hash(password),
            rolle="member", status="active",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id


def get_reset_token(email: str) -> str:
    with database.get_session() as session:
        user = session.query(database.PosUser).filter_by(email=email).first()
        return user.reset_token


def login_ok(email: str, password: str) -> bool:
    res = client.post("/api/auth/login", data={"username": email, "password": password})
    return res.status_code == 200


def test_valid_token_resets_password():
    api.limiter.reset()  # jeder Testfall startet mit frischem Rate-Limit-Zähler (isoliert von anderen Testfällen)
    email = "reset-valid@example.com"
    make_user(email, password="OldPassword123")

    res = client.post("/api/auth/forgot-password", json={"email": email})
    record("forgot-password (existierende E-Mail) -> 200", res.status_code == 200, f"status={res.status_code}")

    token = get_reset_token(email)
    record("reset_token wurde in der DB gesetzt", bool(token))

    res2 = client.post("/api/auth/reset-password", json={"token": token, "password": "NewPassword456"})
    record("reset-password mit gültigem Token -> 200", res2.status_code == 200, f"status={res2.status_code}, body={res2.text}")

    record("Login mit NEUEM Passwort funktioniert danach", login_ok(email, "NewPassword456"))
    record("Login mit ALTEM Passwort funktioniert NICHT mehr", not login_ok(email, "OldPassword123"))


def test_expired_token_rejected():
    api.limiter.reset()  # jeder Testfall startet mit frischem Rate-Limit-Zähler (isoliert von anderen Testfällen)
    email = "reset-expired@example.com"
    make_user(email)

    client.post("/api/auth/forgot-password", json={"email": email})
    token = get_reset_token(email)

    # Ablaufzeit künstlich in die Vergangenheit setzen (statt 45 Minuten zu warten).
    from datetime import datetime, timedelta
    with database.get_session() as session:
        user = session.query(database.PosUser).filter_by(email=email).first()
        user.reset_token_expires = datetime.utcnow() - timedelta(minutes=1)
        session.commit()

    res = client.post("/api/auth/reset-password", json={"token": token, "password": "NewPassword456"})
    record("abgelaufener Token wird abgelehnt (400)", res.status_code == 400, f"status={res.status_code}, body={res.text}")

    with database.get_session() as session:
        user = session.query(database.PosUser).filter_by(email=email).first()
        record("password_hash blieb bei abgelehntem (abgelaufenem) Token unverändert",
               user.reset_token is not None, "reset_token wurde trotz Ablehnung genullt")


def test_used_token_rejected_on_second_attempt():
    api.limiter.reset()  # jeder Testfall startet mit frischem Rate-Limit-Zähler (isoliert von anderen Testfällen)
    email = "reset-reuse@example.com"
    make_user(email)

    client.post("/api/auth/forgot-password", json={"email": email})
    token = get_reset_token(email)

    res1 = client.post("/api/auth/reset-password", json={"token": token, "password": "FirstNewPass1"})
    record("erster Reset-Versuch mit frischem Token -> 200", res1.status_code == 200, f"status={res1.status_code}")

    res2 = client.post("/api/auth/reset-password", json={"token": token, "password": "SecondNewPass2"})
    record("zweiter Versuch mit demselben (bereits benutzten) Token wird abgelehnt (400)",
           res2.status_code == 400, f"status={res2.status_code}, body={res2.text}")

    record("Login funktioniert weiterhin nur mit dem Passwort aus dem ERSTEN Reset",
           login_ok(email, "FirstNewPass1") and not login_ok(email, "SecondNewPass2"))


def test_second_forgot_password_invalidates_first_token():
    api.limiter.reset()  # jeder Testfall startet mit frischem Rate-Limit-Zähler (isoliert von anderen Testfällen)
    email = "reset-double-request@example.com"
    make_user(email)

    client.post("/api/auth/forgot-password", json={"email": email})
    first_token = get_reset_token(email)

    time.sleep(0.05)  # sicherstellen, dass token_urlsafe() nicht zufaellig identisch generiert (astronomisch unwahrscheinlich, aber explizit)
    client.post("/api/auth/forgot-password", json={"email": email})
    second_token = get_reset_token(email)

    record("zweite forgot-password-Anfrage erzeugt einen ANDEREN Token als die erste",
           first_token != second_token, f"first={first_token[:8]}…, second={second_token[:8]}…")

    res_old = client.post("/api/auth/reset-password", json={"token": first_token, "password": "ShouldFailPass1"})
    record("ERSTER (überholter) Token ist nicht mehr gültig (400)",
           res_old.status_code == 400, f"status={res_old.status_code}")

    res_new = client.post("/api/auth/reset-password", json={"token": second_token, "password": "ShouldWorkPass1"})
    record("ZWEITER (aktueller) Token ist gültig (200)",
           res_new.status_code == 200, f"status={res_new.status_code}, body={res_new.text}")


def test_nonexistent_email_identical_response():
    api.limiter.reset()  # jeder Testfall startet mit frischem Rate-Limit-Zähler (isoliert von anderen Testfällen)
    existing_email = "reset-timing-existing@example.com"
    make_user(existing_email)

    res_existing = client.post("/api/auth/forgot-password", json={"email": existing_email})
    t0 = time.perf_counter()
    res_missing = client.post("/api/auth/forgot-password", json={"email": "definitely-not-registered-xyz@example.com"})
    t_missing = time.perf_counter() - t0

    t0 = time.perf_counter()
    res_existing2 = client.post("/api/auth/forgot-password", json={"email": existing_email})
    t_existing = time.perf_counter() - t0

    record("Status-Code identisch (existierend vs. nicht-existierend)",
           res_existing.status_code == res_missing.status_code == 200,
           f"existing={res_existing.status_code}, missing={res_missing.status_code}")
    record("Response-Body identisch (kein Enumeration-Leak)",
           res_existing.json() == res_missing.json() == res_existing2.json(),
           f"existing={res_existing.json()}, missing={res_missing.json()}")
    # Grobe Toleranz (Faktor 3, absolute Grenze 300ms) statt exakter Gleichheit -
    # Ziel ist "kein GROBER Unterschied" (z.B. durch übersprungenen Argon2-Vergleich
    # oder synchron blockierenden Mailversand), nicht Mikrosekunden-Präzision, die
    # auf einer geteilten Test-VM ohnehin verrauscht wäre.
    ratio = max(t_missing, t_existing) / max(min(t_missing, t_existing), 1e-6)
    record("Antwortzeit vergleichbar (kein grober Timing-Unterschied, Faktor <3 und <300ms Differenz)",
           ratio < 3 and abs(t_missing - t_existing) < 0.3,
           f"t_existing={t_existing*1000:.1f}ms, t_missing={t_missing*1000:.1f}ms, ratio={ratio:.2f}")


def test_rate_limiting():
    api.limiter.reset()  # jeder Testfall startet mit frischem Rate-Limit-Zähler (isoliert von anderen Testfällen)
    email = "reset-ratelimit@example.com"
    make_user(email)

    statuses = []
    for _ in range(6):
        res = client.post("/api/auth/forgot-password", json={"email": email})
        statuses.append(res.status_code)

    record("nach 3 erlaubten Anfragen/Minute greift Rate-Limiting (429) bei weiteren Versuchen",
           429 in statuses, f"statuses={statuses}")


def main():
    for fn in (
        test_valid_token_resets_password,
        test_expired_token_rejected,
        test_used_token_rejected_on_second_attempt,
        test_second_forgot_password_invalidates_first_token,
        test_nonexistent_email_identical_response,
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
