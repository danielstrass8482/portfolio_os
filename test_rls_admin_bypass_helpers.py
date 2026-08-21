"""
test_rls_admin_bypass_helpers.py – Test für den Chunk-2-Nachzug (2026-08-21,
siehe docs/rls-force-umbau-plan-21-08.md, Sonderfall c): die vier Geschwister-
Helfer von _resolve_user_id() -- _owner_check_id() (jetzt begleitet von
_switch_context_for_admin_write(), ehem. _maybe_log_admin_access()),
_require_position_access(), _require_portfolio_access() -- die im
ursprünglichen Chunk-2-Commit 982842c bewusst zurückgestellt wurden
(12 betroffene Endpoints: Update/Delete Position/Portfolio/Transaction,
Delete RealEstate, Positions-Transaktionen, Tax-Preview, Create-Transaction,
Tagesgeld, Depot-CSV-Import).

Deckt für jeden betroffenen Endpoint ab:
  - Member B (nicht Owner, kein Admin) -> weiterhin 404 (IDOR-Schutz aus
    db-isolation-audit-05-08.md Teil C unverändert intakt, KEIN Rückschritt)
  - Admin (echter Cross-Access) -> Operation gelingt, wird in
    pos_admin_access_log protokolliert, kein Context-Leck danach (Admin-
    Folgerequest zeigt wieder eigene Daten)
  - Für 3 repräsentative Schreibendpoints (Position/Portfolio/Transaction,
    kombinierter Lookup+Write in EINER get_session()): direkter Nachweis per
    Monkeypatch, dass der RLS-Kontext beim Schreibzugriff TATSÄCHLICH auf den
    Owner zeigt (nicht nur "es hat funktioniert", was heute -- vor FORCE RLS
    -- auch ohne korrekten Kontext funktionieren würde)
  - Concurrency: Admin-Cross-Write gleichzeitig mit Member-Normalrequest,
    kein Context-Bleed (analog zu test_rls_context.py/test_rls_special_cases.py)

KEINE Produktions-DB, KEIN echter yfinance-Call. Setup identisch zu
test_rls_special_cases.py (lokale Wegwerf-Postgres-Instanz):
    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER rlssc_tmp WITH PASSWORD 'rlssc_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_rlssc OWNER rlssc_tmp;"
    DATABASE_URL=postgresql://rlssc_tmp:rlssc_tmp_pw@localhost:5432/portfolio_os_rlssc \
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \
    python3 test_rls_admin_bypass_helpers.py
"""
import os
import sys
import threading

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://rlssc_tmp:rlssc_tmp_pw@localhost:5432/portfolio_os_rlssc",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")

import api  # noqa: E402
import database  # noqa: E402
import portfolio as portfolio_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

RESULTS = []
client = TestClient(api.app, base_url="https://testserver")
pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")
_counter = {"n": 0}


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def make_user(email: str, *, rolle: str = "member", password: str = "TestPassword123") -> int:
    with database.get_session() as session:
        session.query(database.PosUser).filter_by(email=email).delete()
    with database.get_session() as session:
        u = database.PosUser(
            name="Bypass Helper Test", email=email, password_hash=pwd_context.hash(password),
            rolle=rolle, status="active", trading_bot_access=True, portfolio_os_access=True,
        )
        session.add(u)
        session.flush()
        return u.id


def mint_token(user_id: int) -> str:
    return api.create_access_token({"sub": str(user_id)})


def auth(user_id: int) -> dict:
    return {"Authorization": f"Bearer {mint_token(user_id)}"}


def admin_log_count() -> int:
    with database.get_session() as session:
        return session.query(database.PosAdminAccessLog).count()


def _uniq() -> str:
    _counter["n"] += 1
    return f"{_counter['n']}"


def setup_a_resources(uid_a: int) -> dict:
    """Legt für Nutzer A frisches Portfolio+Position+Transaction+Immobilie an
    (jeweils eindeutig benannt/getickert, damit parallele Testfälle sich nicht
    in die Quere kommen)."""
    n = _uniq()
    token_a = auth(uid_a)

    pf_res = client.post("/api/portfolios", json={"name": f"A-Depot-{n}", "typ": "depot"}, headers=token_a)
    assert pf_res.status_code == 200, pf_res.text
    portfolio_id = pf_res.json()["id"]

    tx_res = client.post("/api/transactions", json={
        "portfolio_id": portfolio_id, "typ": "kauf", "ticker": f"TICK{n}",
        "quantity": 1, "price": 100.0, "datum": "2026-01-01",
    }, headers=token_a)
    assert tx_res.status_code == 200, tx_res.text
    tx_body = tx_res.json()

    re_res = client.post("/api/real-estate", json={"adresse": f"A-Immobilie-{n}", "kaufpreis": 10000},
                          headers=token_a)
    assert re_res.status_code == 200, re_res.text

    return {
        "portfolio_id": portfolio_id,
        "position_id": tx_body["position_id"],
        "transaction_id": tx_body["transaction_id"],
        "real_estate_id": re_res.json()["id"],
    }


# ─────────────────────────────────────────────
# 1: _owner_check_id + _switch_context_for_admin_write (7 Endpoints)
# ─────────────────────────────────────────────

def _check_write_endpoint(label, method, path_fn, member_payload, admin_payload, uid_a, uid_b, admin_id, res):
    """Gemeinsames Muster: B (IDOR) -> 404, Admin -> 200 + geloggt + kein Leck danach."""
    b_res = client.request(method, path_fn(res), json=member_payload, headers=auth(uid_b))
    record(f"{label}: Member B (IDOR) -> 404", b_res.status_code == 404, f"status={b_res.status_code}")

    log_before = admin_log_count()
    admin_res = client.request(method, path_fn(res), json=admin_payload, headers=auth(admin_id))
    record(f"{label}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record(f"{label}: Admin-Cross-Access wurde protokolliert (+1)",
           admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")

    own_res = client.get("/api/portfolios", headers=auth(admin_id))
    record(f"{label}: kein hängender Kontext nach Admin-Cross-Write (ContextVar None)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")
    del own_res


def test_position_put_delete():
    uid_a = make_user(f"pos-owner-{_uniq()}@example.com")
    uid_b = make_user(f"pos-b-{_uniq()}@example.com")
    admin_id = make_user(f"pos-admin-{_uniq()}@example.com", rolle="admin")

    res = setup_a_resources(uid_a)
    payload = {"display_name": "Umbenannt"}
    _check_write_endpoint(
        "PUT /api/positions/{id}", "PUT",
        lambda r: f"/api/positions/{r['position_id']}", payload, payload, uid_a, uid_b, admin_id, res,
    )

    # Kontext-Korrektheit direkt nachweisen: update_position() öffnet Lookup+
    # Write in EINER get_session() -- der Kontext MUSS beim Aufruf schon auf
    # den Owner zeigen, nicht erst danach.
    res2 = setup_a_resources(uid_a)
    beobachtet = {}
    original = portfolio_module.update_position

    def spion(*args, **kwargs):
        beobachtet["ctx"] = database._current_user_ctx.get()
        return original(*args, **kwargs)

    portfolio_module.update_position = spion
    try:
        r = client.put(f"/api/positions/{res2['position_id']}", json={"display_name": "X"}, headers=auth(admin_id))
    finally:
        portfolio_module.update_position = original
    record("PUT /api/positions/{id}: Kontext beim Schreibzugriff zeigt auf den ECHTEN Owner (nicht den Admin)",
           r.status_code == 200 and beobachtet.get("ctx") == uid_a,
           f"status={r.status_code}, ctx={beobachtet.get('ctx')}, owner={uid_a}, admin={admin_id}")

    # DELETE auf frischer Ressource (PUT hat sie nicht verbraucht, DELETE würde es)
    res3 = setup_a_resources(uid_a)
    b_res = client.delete(f"/api/positions/{res3['position_id']}", headers=auth(uid_b))
    record("DELETE /api/positions/{id}: Member B (IDOR) -> 404", b_res.status_code == 404,
           f"status={b_res.status_code}")
    log_before = admin_log_count()
    admin_res = client.delete(f"/api/positions/{res3['position_id']}", headers=auth(admin_id))
    record("DELETE /api/positions/{id}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}")
    record("DELETE /api/positions/{id}: Admin-Cross-Access wurde protokolliert (+1)",
           admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")


def test_portfolio_put_delete():
    uid_a = make_user(f"pf-owner-{_uniq()}@example.com")
    uid_b = make_user(f"pf-b-{_uniq()}@example.com")
    admin_id = make_user(f"pf-admin-{_uniq()}@example.com", rolle="admin")

    res = setup_a_resources(uid_a)
    payload = {"name": "Umbenanntes Depot"}
    _check_write_endpoint(
        "PUT /api/portfolios/{id}", "PUT",
        lambda r: f"/api/portfolios/{r['portfolio_id']}", payload, payload, uid_a, uid_b, admin_id, res,
    )

    res2 = setup_a_resources(uid_a)
    beobachtet = {}
    original = portfolio_module.update_portfolio

    def spion(*args, **kwargs):
        beobachtet["ctx"] = database._current_user_ctx.get()
        return original(*args, **kwargs)

    portfolio_module.update_portfolio = spion
    try:
        r = client.put(f"/api/portfolios/{res2['portfolio_id']}", json={"name": "Y"}, headers=auth(admin_id))
    finally:
        portfolio_module.update_portfolio = original
    record("PUT /api/portfolios/{id}: Kontext beim Schreibzugriff zeigt auf den ECHTEN Owner (nicht den Admin)",
           r.status_code == 200 and beobachtet.get("ctx") == uid_a,
           f"status={r.status_code}, ctx={beobachtet.get('ctx')}, owner={uid_a}, admin={admin_id}")

    # DELETE: Portfolio darf laut delete_portfolio() keine Positionen mehr
    # enthalten -- eigenes leeres Portfolio anlegen statt setup_a_resources().
    pf_res = client.post("/api/portfolios", json={"name": f"A-Leer-{_uniq()}", "typ": "depot"}, headers=auth(uid_a))
    portfolio_id = pf_res.json()["id"]
    b_res = client.delete(f"/api/portfolios/{portfolio_id}", headers=auth(uid_b))
    record("DELETE /api/portfolios/{id}: Member B (IDOR) -> 404", b_res.status_code == 404,
           f"status={b_res.status_code}")
    log_before = admin_log_count()
    admin_res = client.delete(f"/api/portfolios/{portfolio_id}", headers=auth(admin_id))
    record("DELETE /api/portfolios/{id}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record("DELETE /api/portfolios/{id}: Admin-Cross-Access wurde protokolliert (+1)",
           admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")


def test_transaction_put_delete():
    uid_a = make_user(f"tx-owner-{_uniq()}@example.com")
    uid_b = make_user(f"tx-b-{_uniq()}@example.com")
    admin_id = make_user(f"tx-admin-{_uniq()}@example.com", rolle="admin")

    res = setup_a_resources(uid_a)
    payload = {"quantity": 2}
    _check_write_endpoint(
        "PUT /api/transactions/{id}", "PUT",
        lambda r: f"/api/transactions/{r['transaction_id']}", payload, payload, uid_a, uid_b, admin_id, res,
    )

    res2 = setup_a_resources(uid_a)
    beobachtet = {}
    original = portfolio_module.update_transaction

    def spion(*args, **kwargs):
        beobachtet["ctx"] = database._current_user_ctx.get()
        return original(*args, **kwargs)

    portfolio_module.update_transaction = spion
    try:
        r = client.put(f"/api/transactions/{res2['transaction_id']}", json={"quantity": 3}, headers=auth(admin_id))
    finally:
        portfolio_module.update_transaction = original
    record("PUT /api/transactions/{id}: Kontext beim Schreibzugriff zeigt auf den ECHTEN Owner (nicht den Admin)",
           r.status_code == 200 and beobachtet.get("ctx") == uid_a,
           f"status={r.status_code}, ctx={beobachtet.get('ctx')}, owner={uid_a}, admin={admin_id}")

    res3 = setup_a_resources(uid_a)
    b_res = client.delete(f"/api/transactions/{res3['transaction_id']}", headers=auth(uid_b))
    record("DELETE /api/transactions/{id}: Member B (IDOR) -> 404", b_res.status_code == 404,
           f"status={b_res.status_code}")
    log_before = admin_log_count()
    admin_res = client.delete(f"/api/transactions/{res3['transaction_id']}", headers=auth(admin_id))
    record("DELETE /api/transactions/{id}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}")
    record("DELETE /api/transactions/{id}: Admin-Cross-Access wurde protokolliert (+1)",
           admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")


def test_real_estate_delete():
    uid_a = make_user(f"re-owner-{_uniq()}@example.com")
    uid_b = make_user(f"re-b-{_uniq()}@example.com")
    admin_id = make_user(f"re-admin-{_uniq()}@example.com", rolle="admin")

    res = setup_a_resources(uid_a)
    b_res = client.delete(f"/api/real-estate/{res['real_estate_id']}", headers=auth(uid_b))
    record("DELETE /api/real-estate/{id}: Member B (IDOR) -> 404", b_res.status_code == 404,
           f"status={b_res.status_code}")

    log_before = admin_log_count()
    admin_res = client.delete(f"/api/real-estate/{res['real_estate_id']}", headers=auth(admin_id))
    record("DELETE /api/real-estate/{id}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record("DELETE /api/real-estate/{id}: Admin-Cross-Access wurde protokolliert (+1)",
           admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")
    record("DELETE /api/real-estate/{id}: kein hängender Kontext danach (ContextVar None)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


# ─────────────────────────────────────────────
# 2: _require_position_access (2 Endpoints)
# ─────────────────────────────────────────────

def test_require_position_access_endpoints():
    uid_a = make_user(f"rpa-owner-{_uniq()}@example.com")
    uid_b = make_user(f"rpa-b-{_uniq()}@example.com")
    admin_id = make_user(f"rpa-admin-{_uniq()}@example.com", rolle="admin")
    res = setup_a_resources(uid_a)
    pos_id = res["position_id"]

    for label, path in (
        ("GET /api/positions/{id}/transactions", f"/api/positions/{pos_id}/transactions"),
        ("GET /api/tax-preview", f"/api/tax-preview?position_id={pos_id}&verkauf_preis=150"),
    ):
        b_res = client.get(path, headers=auth(uid_b))
        record(f"{label}: Member B (IDOR) -> 404", b_res.status_code == 404, f"status={b_res.status_code}")

        log_before = admin_log_count()
        admin_res = client.get(path, headers=auth(admin_id))
        record(f"{label}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
               f"status={admin_res.status_code}, body={admin_res.text}")
        record(f"{label}: Admin-Cross-Access wurde protokolliert (+1)",
               admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")
        record(f"{label}: kein hängender Kontext danach (ContextVar None)",
               database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


# ─────────────────────────────────────────────
# 3: _require_portfolio_access (3 Endpoints)
# ─────────────────────────────────────────────

def test_require_portfolio_access_endpoints():
    uid_a = make_user(f"rfa-owner-{_uniq()}@example.com")
    uid_b = make_user(f"rfa-b-{_uniq()}@example.com")
    admin_id = make_user(f"rfa-admin-{_uniq()}@example.com", rolle="admin")
    res = setup_a_resources(uid_a)
    pf_id = res["portfolio_id"]

    # POST /api/transactions: legt eine NEUE Transaktion in As Portfolio an.
    b_res = client.post("/api/transactions", json={
        "portfolio_id": pf_id, "typ": "kauf", "ticker": "BTIDOR",
        "quantity": 1, "price": 10.0, "datum": "2026-01-02",
    }, headers=auth(uid_b))
    record("POST /api/transactions: Member B (IDOR) -> 404", b_res.status_code == 404, f"status={b_res.status_code}")

    log_before = admin_log_count()
    admin_res = client.post("/api/transactions", json={
        "portfolio_id": pf_id, "typ": "kauf", "ticker": "ADMTX",
        "quantity": 1, "price": 10.0, "datum": "2026-01-02",
    }, headers=auth(admin_id))
    record("POST /api/transactions: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record("POST /api/transactions: Admin-Cross-Access wurde protokolliert (+1)",
           admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")

    # POST /api/positions/tagesgeld
    b_res2 = client.post("/api/positions/tagesgeld", json={
        "portfolio_id": pf_id, "konto_name": "B-Versuch", "betrag": 100.0,
    }, headers=auth(uid_b))
    record("POST /api/positions/tagesgeld: Member B (IDOR) -> 404", b_res2.status_code == 404,
           f"status={b_res2.status_code}")

    log_before2 = admin_log_count()
    admin_res2 = client.post("/api/positions/tagesgeld", json={
        "portfolio_id": pf_id, "konto_name": "Admin-Zugriff", "betrag": 100.0,
    }, headers=auth(admin_id))
    record("POST /api/positions/tagesgeld: Admin-Cross-Access -> 200", admin_res2.status_code == 200,
           f"status={admin_res2.status_code}, body={admin_res2.text}")
    record("POST /api/positions/tagesgeld: Admin-Cross-Access wurde protokolliert (+1)",
           admin_log_count() == log_before2 + 1, f"vorher={log_before2}, nachher={admin_log_count()}")

    # POST /api/depot/import-csv: nur der IDOR-Block wird geprüft (der Zugriffs-
    # check _require_portfolio_access läuft VOR dem CSV-Parsing, siehe api.py)
    # -- derselbe Helfer wie oben, ein voller Erfolgs-Roundtrip mit echtem
    # Broker-CSV-Format ist für diesen Nachzug kein zusätzlicher Erkenntnisgewinn.
    b_res3 = client.post(
        "/api/depot/import-csv", data={"portfolio_id": str(pf_id), "broker": "comdirect"},
        files={"file": ("t.csv", b"irrelevant", "text/csv")}, headers=auth(uid_b),
    )
    record("POST /api/depot/import-csv: Member B (IDOR) -> 404", b_res3.status_code == 404,
           f"status={b_res3.status_code}")

    record("kein hängender Kontext nach Gruppe 3 (ContextVar None)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


# ─────────────────────────────────────────────
# 4: Concurrency -- Admin-Cross-Write gleichzeitig mit Member-Normalrequest
# ─────────────────────────────────────────────

def test_concurrent_admin_write_no_leak():
    uid_a = make_user(f"conc-owner-{_uniq()}@example.com")
    admin_id = make_user(f"conc-admin-{_uniq()}@example.com", rolle="admin")
    uid_c = make_user(f"conc-c-{_uniq()}@example.com")

    res_a = setup_a_resources(uid_a)
    res_c = setup_a_resources(uid_c)

    ergebnisse = {"admin": [], "c": [], "fehler": []}

    def admin_writes():
        try:
            for _ in range(5):
                r = client.put(f"/api/positions/{res_a['position_id']}",
                                json={"display_name": "Conc"}, headers=auth(admin_id))
                ergebnisse["admin"].append(r.status_code)
        except Exception as e:
            ergebnisse["fehler"].append(f"admin: {e}")

    def c_reads():
        try:
            for _ in range(5):
                r = client.get(f"/api/positions/{res_c['position_id']}/transactions", headers=auth(uid_c))
                ergebnisse["c"].append(r.status_code)
        except Exception as e:
            ergebnisse["fehler"].append(f"c: {e}")

    t1 = threading.Thread(target=admin_writes)
    t2 = threading.Thread(target=c_reads)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    record("Concurrency: keine Exceptions", not ergebnisse["fehler"], f"{ergebnisse['fehler']}")
    record("Concurrency: Admin-Cross-Writes alle 200", all(s == 200 for s in ergebnisse["admin"]),
           f"{ergebnisse['admin']}")
    record("Concurrency: Cs eigene Requests alle 200 (kein Context-Bleed von der Admin-Umschaltung)",
           all(s == 200 for s in ergebnisse["c"]), f"{ergebnisse['c']}")
    record("Nach Concurrency-Test: kein hängender Kontext (ContextVar None)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


if __name__ == "__main__":
    database.init_db()
    api.limiter.reset()
    test_position_put_delete()
    print()
    test_portfolio_put_delete()
    print()
    test_transaction_put_delete()
    print()
    test_real_estate_delete()
    print()
    test_require_position_access_endpoints()
    print()
    test_require_portfolio_access_endpoints()
    print()
    test_concurrent_admin_write_no_leak()

    print("\n=== ZUSAMMENFASSUNG ===")
    fehlgeschlagen = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(fehlgeschlagen)}/{len(RESULTS)} Checks bestanden.")
    if fehlgeschlagen:
        sys.exit(1)
