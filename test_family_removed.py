"""
test_family_removed.py – RLS-Umbau, Risiko R2 vor Chunk 7 (2026-10-02): die
Familien-Aggregation ist entfernt.

  1. GET /api/family existiert nicht mehr (404) -- auch nicht für Admins.
  2. GET /api/overview?family=true liefert keine Aggregation mehr: der Parameter
     wird ignoriert, die Antwort ist identisch zur normalen Übersicht des
     Aufrufers (eigene Daten).
  3. Die normale Übersicht inkl. protokolliertem Admin-Cross-View
     (?user_id=<fremd>) funktioniert weiter.

Setup (Wegwerf-Postgres):
    sudo -u postgres psql -c "CREATE USER famrm_tmp WITH PASSWORD 'famrm_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_famrm OWNER famrm_tmp;"
    DATABASE_URL=postgresql://famrm_tmp:famrm_tmp_pw@localhost:5432/portfolio_os_famrm \\
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only python3 test_family_removed.py
"""
import os
import subprocess
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://famrm_tmp:famrm_tmp_pw@localhost:5432/portfolio_os_famrm")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")

import api  # noqa: E402
import database  # noqa: E402
import trading_bot_connector  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
RUN_ID = uuid.uuid4().hex[:8]
client = TestClient(api.app, base_url="https://testserver")
# keine Netzwerkaufrufe (Bot-Kontowert, VIX, Kurse)
trading_bot_connector.get_bot_account_value_eur = lambda: {
    "positionen_usd": 0.0, "cash_usd": 0.0, "total_usd": 0.0, "positionen_eur": 0.0,
    "cash_eur": 0.0, "total_eur": 0.0, "positionen_detail": []}
api._vix_status = lambda: None


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + str(detail)) if detail else ''}")


def make_user(tag, rolle):
    with database.get_session() as s:
        u = database.PosUser(name=f"FAM-{tag}-{RUN_ID}", email=f"fam-{tag.lower()}-{RUN_ID}@example.com", rolle=rolle,
                             status="active", portfolio_os_access=True, onboarding_completed=True)
        s.add(u)
        s.flush()
        uid = u.id
    with database.user_context(uid):
        with database.get_session() as s:
            s.add(database.PosRealEstate(user_id=uid, adresse=f"IMMO-{tag}-{RUN_ID}", kaufpreis=100000.0,
                                         letzter_schaetzwert=250000.0 if tag == "B" else 150000.0))
    return uid


def auth(uid):
    return {"Authorization": f"Bearer {api.create_access_token({'sub': str(uid)})}"}


def audit_count():
    return int(subprocess.run(["sudo", "-u", "postgres", "psql", "-d", database.engine.url.database, "-At", "-c",
                               "SELECT count(*) FROM pos_admin_access_log"], capture_output=True, text=True).stdout)


if __name__ == "__main__":
    database.init_db()
    api.limiter.reset()
    admin, b = make_user("ADMIN", "admin"), make_user("B", "member")

    for who, uid in (("Admin", admin), ("Member B", b)):
        r = client.get("/api/family", headers=auth(uid))
        record(f"GET /api/family als {who} -> 404 (Endpoint entfernt)", r.status_code == 404, f"status={r.status_code}")

    normal = client.get("/api/overview", headers=auth(admin))
    fam = client.get("/api/overview?family=true", headers=auth(admin))
    record("GET /api/overview als Admin -> 200", normal.status_code == 200, f"status={normal.status_code}")
    record("GET /api/overview?family=true als Admin -> keine Aggregation, identisch zur eigenen Übersicht",
           fam.status_code == 200 and fam.json() == normal.json(),
           f"gesamtvermoegen normal={normal.json().get('gesamtvermoegen')}, family={fam.json().get('gesamtvermoegen')}")

    vorher = audit_count()
    cross = client.get(f"/api/overview?user_id={b}", headers=auth(admin))
    own_b = client.get("/api/overview", headers=auth(b))
    record("Admin-Cross-View ?user_id=B liefert B's Übersicht (wie B selbst)",
           cross.status_code == 200 and cross.json() == own_b.json(),
           f"admin->B={cross.json().get('gesamtvermoegen')}, B selbst={own_b.json().get('gesamtvermoegen')}")
    record("Admin-Cross-View weiterhin protokolliert (+1)", audit_count() == vorher + 1, f"{vorher} -> {audit_count()}")
    record("Member B kann mit ?user_id=Admin nicht fremd lesen (bekommt eigene Daten)",
           client.get(f"/api/overview?user_id={admin}", headers=auth(b)).json() == own_b.json())

    print("\n=== ZUSAMMENFASSUNG ===")
    fehl = [r for r in RESULTS if not r[1]]
    for name, ok, _ in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(fehl)}/{len(RESULTS)} Checks bestanden.")
    sys.exit(1 if fehl else 0)
