"""
test_rls_owner_lookup_functions.py – Test für die SECURITY DEFINER Owner-
Lookup-Funktionen (2026-09-07, Option B, siehe docs/rls-force-umbau-plan-21-08.md,
Sonderfall c / Restrisiko aus dem Chunk-2-Nachzug-Commit 8e1d89c): die 4
Admin-Bypass-Helfer (_position_owner_id/_portfolio_owner_id/
_transaction_owner_id/_real_estate_owner_id in api.py) laufen jetzt über
pos_position_owner_id()/pos_portfolio_owner_id()/pos_transaction_owner_id()/
pos_real_estate_owner_id() (siehe database.py::_migrate_owner_lookup_functions)
statt über ein normales session.get() -- der RLS-Kontext beim Lookup zeigt
noch auf den Admin selbst, nicht auf den Ziel-Owner (Umschaltung passiert
erst NACH dem Lookup, siehe _switch_context_for_admin_write()).

UNTERSCHIED zu test_rls_admin_bypass_helpers.py: jene Suite lief OHNE FORCE
ROW LEVEL SECURITY (Chunk 5/7 noch nicht aktiv) -- der alte session.get()-
Lookup hätte damals GENAUSO funktioniert, weil Postgres den Tabellenbesitzer
ohne FORCE ohnehin nicht einschränkt. Diese Suite hier schaltet FORCE ROW
LEVEL SECURITY + Owner-Only-Policies für die 4 betroffenen Tabellen SELBST
scharf (rein lokal auf der Wegwerf-DB, simuliert den künftigen Chunk-5/7-
Zustand) -- NUR so lässt sich der eigentliche Bug überhaupt reproduzieren
und der Fix beweisen: ohne die SECURITY DEFINER Funktionen (d.h. mit dem
alten session.get()) würde jeder der 12 betroffenen Endpoints für Admins
unter FORCE RLS 404 werfen, weil der Lookup selbst schon RLS-gefiltert wäre.

Deckt ab:
  1. Sanity-Check: FORCE RLS + Policies blocken tatsächlich einen naiven
     Lookup unter Admin-eigenem Kontext (beweist, dass der Testaufbau den
     Bug reproduzieren würde, wäre der Fix nicht da).
  2. Die 4 SECURITY DEFINER Funktionen liefern trotzdem korrekt die owner_id
     zurück, aufgerufen während NOCH der Admin-Kontext aktiv ist (exakt der
     Zeitpunkt, an dem die Python-Helfer sie aufrufen).
  3. NULL für nicht existierende Ressourcen-IDs (kein Leck, kein Crash).
  4. PUBLIC kann die Funktionen NICHT ausführen (REVOKE/GRANT greift) --
     eine dritte, unprivilegierte Rolle bekommt "permission denied".
  5. Voller HTTP-Roundtrip aller 12 betroffenen Endpoints MIT echtem FORCE
     RLS (Obermenge von test_rls_admin_bypass_helpers.py, dort ohne FORCE):
     2-Konten-Cross-Access mit expliziten, getrennten Accounts (uid_a
     Owner, uid_b NICHT Owner/NICHT Admin) -- Member B -> 404 (IDOR-Schutz
     unter FORCE RLS weiterhin intakt), Admin-Cross-Access -> 200 + geloggt
     (nur dank der SECURITY DEFINER Funktionen möglich).

VORAUSSETZUNG (zusätzlich zum Setup unten): die 4 Funktionen müssen bereits
der BYPASSRLS-Rolle pos_owner_lookup_bypass gehören (docs/rls-owner-lookup-
bypass-role-setup.sql) -- ohne diesen manuellen Schritt schlagen die
Sanity-Checks in Gruppe 2 (und in Gruppe 5 die Admin-Cross-Access-Checks)
erwartungsgemäß fehl, weil die Funktionen dann selbst noch RLS-gefiltert
wären (SECURITY DEFINER allein bypassed nichts, siehe database.py-Kommentar).

KEINE Produktions-DB, KEIN echter yfinance-Call. Setup (Wegwerf-Postgres,
analog test_rls_admin_bypass_helpers.py, PLUS die BYPASSRLS-Rolle):
    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER ownerlookup_tmp WITH PASSWORD 'ownerlookup_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_ownerlookup OWNER ownerlookup_tmp;"
    DATABASE_URL=postgresql://ownerlookup_tmp:ownerlookup_tmp_pw@localhost:5432/portfolio_os_ownerlookup \
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \
    python3 -c "import database; database.init_db()"
    sudo -u postgres psql -d portfolio_os_ownerlookup <<'SQL'
      CREATE ROLE pos_owner_lookup_bypass NOLOGIN BYPASSRLS;
      GRANT pos_owner_lookup_bypass TO ownerlookup_tmp;
      ALTER FUNCTION pos_position_owner_id(integer)    OWNER TO pos_owner_lookup_bypass;
      ALTER FUNCTION pos_portfolio_owner_id(integer)   OWNER TO pos_owner_lookup_bypass;
      ALTER FUNCTION pos_transaction_owner_id(integer) OWNER TO pos_owner_lookup_bypass;
      ALTER FUNCTION pos_real_estate_owner_id(integer) OWNER TO pos_owner_lookup_bypass;
      GRANT EXECUTE ON FUNCTION pos_position_owner_id(integer)    TO ownerlookup_tmp;
      GRANT EXECUTE ON FUNCTION pos_portfolio_owner_id(integer)   TO ownerlookup_tmp;
      GRANT EXECUTE ON FUNCTION pos_transaction_owner_id(integer) TO ownerlookup_tmp;
      GRANT EXECUTE ON FUNCTION pos_real_estate_owner_id(integer) TO ownerlookup_tmp;
SQL
    DATABASE_URL=postgresql://ownerlookup_tmp:ownerlookup_tmp_pw@localhost:5432/portfolio_os_ownerlookup \
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \
    python3 test_rls_owner_lookup_functions.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_ownerlookup;"
    sudo -u postgres psql -c "DROP OWNED BY ownerlookup_tmp; DROP ROLE ownerlookup_tmp;"
    sudo -u postgres psql -c "DROP ROLE pos_owner_lookup_bypass;"
"""
import os
import subprocess
import sys

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://ownerlookup_tmp:ownerlookup_tmp_pw@localhost:5432/portfolio_os_ownerlookup",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")

import api  # noqa: E402
import database  # noqa: E402
from database import text  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

RESULTS = []
client = TestClient(api.app, base_url="https://testserver")
pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")
_counter = {"n": 0}

# Test-DB-Name für psql-Aufrufe außerhalb der App-Engine (Gruppe 4: dritte,
# unprivilegierte Rolle -- braucht eine eigene Connection, kein SQLAlchemy nötig).
_TEST_DB = database.engine.url.database


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def _uniq() -> str:
    _counter["n"] += 1
    return f"{_counter['n']}"


def make_user(email: str, *, rolle: str = "member", password: str = "TestPassword123") -> int:
    with database.get_session() as session:
        session.query(database.PosUser).filter_by(email=email).delete()
    with database.get_session() as session:
        u = database.PosUser(
            name="Owner Lookup Test", email=email, password_hash=pwd_context.hash(password),
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


def setup_a_resources(uid_a: int) -> dict:
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
# 0: FORCE ROW LEVEL SECURITY + Owner-Policies scharf schalten (simuliert
#    Chunk 5/7 -- rein lokal auf dieser Wegwerf-DB, kein Produktionscode)
# ─────────────────────────────────────────────

_POLICY_SQL = {
    "pos_positions": """
        SELECT id FROM pos_portfolios
        WHERE user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
    """,
}


def enable_force_rls():
    ddl = [
        # pos_portfolios / pos_real_estate: direktes user_id.
        "ALTER TABLE pos_portfolios ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE pos_portfolios FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS user_isolation ON pos_portfolios",
        """CREATE POLICY user_isolation ON pos_portfolios USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
        )""",
        "ALTER TABLE pos_real_estate ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE pos_real_estate FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS user_isolation ON pos_real_estate",
        """CREATE POLICY user_isolation ON pos_real_estate USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
        )""",
        # pos_positions / pos_transactions: user_id nur über portfolio_id -> pos_portfolios
        # erreichbar (siehe Plan-Dokument Abschnitt 4, exakt dasselbe Muster für beide).
        "ALTER TABLE pos_positions ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE pos_positions FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS user_isolation ON pos_positions",
        f"""CREATE POLICY user_isolation ON pos_positions USING (
            portfolio_id IN ({_POLICY_SQL['pos_positions']})
        )""",
        "ALTER TABLE pos_transactions ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE pos_transactions FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS user_isolation ON pos_transactions",
        f"""CREATE POLICY user_isolation ON pos_transactions USING (
            portfolio_id IN ({_POLICY_SQL['pos_positions']})
        )""",
    ]
    with database.engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))


# ─────────────────────────────────────────────
# 1+2: Sanity-Check (Bug reproduzierbar) + SECURITY DEFINER Funktionen liefern
#      trotzdem korrekt die owner_id
# ─────────────────────────────────────────────

def test_force_rls_blocks_naive_lookup_but_not_the_function():
    uid_a = make_user(f"sd-owner-{_uniq()}@example.com")
    admin_id = make_user(f"sd-admin-{_uniq()}@example.com", rolle="admin")
    res = setup_a_resources(uid_a)

    with database.user_context(admin_id):
        with database.get_session() as session:
            naive = session.execute(
                text("SELECT id FROM pos_positions WHERE id = :id"), {"id": res["position_id"]}
            ).fetchone()
        record(
            "Sanity: naiver Lookup unter Admin-Kontext sieht die fremde Position NICHT "
            "(beweist, dass FORCE RLS hier tatsächlich greift -- ohne SECURITY DEFINER "
            "Funktion wäre GENAU DAS der Bug)",
            naive is None, f"naive={naive!r}",
        )

        for label, fn, id_key in (
            ("pos_position_owner_id", "pos_position_owner_id", "position_id"),
            ("pos_portfolio_owner_id", "pos_portfolio_owner_id", "portfolio_id"),
            ("pos_transaction_owner_id", "pos_transaction_owner_id", "transaction_id"),
            ("pos_real_estate_owner_id", "pos_real_estate_owner_id", "real_estate_id"),
        ):
            with database.get_session() as session:
                owner = session.execute(text(f"SELECT {fn}(:id)"), {"id": res[id_key]}).scalar()
            record(
                f"{label}(): liefert trotz Admin-eigenem RLS-Kontext den ECHTEN Owner",
                owner == uid_a, f"got={owner}, expected={uid_a}",
            )

    # Die 4 api.py-Helfer selbst (nicht nur die rohe SQL-Funktion) -- exakt der
    # Aufrufpfad aus _switch_context_for_admin_write()/_require_*_access().
    with database.user_context(admin_id):
        record("api._position_owner_id() liefert echten Owner",
               api._position_owner_id(res["position_id"]) == uid_a)
        record("api._portfolio_owner_id() liefert echten Owner",
               api._portfolio_owner_id(res["portfolio_id"]) == uid_a)
        record("api._transaction_owner_id() liefert echten Owner",
               api._transaction_owner_id(res["transaction_id"]) == uid_a)
        record("api._real_estate_owner_id() liefert echten Owner",
               api._real_estate_owner_id(res["real_estate_id"]) == uid_a)


# ─────────────────────────────────────────────
# 3: NULL für nicht existierende IDs
# ─────────────────────────────────────────────

def test_nonexistent_id_returns_null():
    admin_id = make_user(f"sd-null-admin-{_uniq()}@example.com", rolle="admin")
    with database.user_context(admin_id):
        record("api._position_owner_id(999999999) -> None",
               api._position_owner_id(999999999) is None)
        record("api._portfolio_owner_id(999999999) -> None",
               api._portfolio_owner_id(999999999) is None)
        record("api._transaction_owner_id(999999999) -> None",
               api._transaction_owner_id(999999999) is None)
        record("api._real_estate_owner_id(999999999) -> None",
               api._real_estate_owner_id(999999999) is None)


# ─────────────────────────────────────────────
# 4: PUBLIC kann die Funktionen NICHT ausführen
# ─────────────────────────────────────────────

def test_public_cannot_execute():
    role = f"ownerlookup_unpriv_{_uniq()}"
    try:
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-c",
             f"CREATE ROLE {role} LOGIN PASSWORD 'unpriv_pw';"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", _TEST_DB, "-c",
             f"GRANT CONNECT ON DATABASE {_TEST_DB} TO {role};"],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["psql", f"postgresql://{role}:unpriv_pw@localhost:5432/{_TEST_DB}",
             "-c", "SELECT pos_position_owner_id(1);"],
            capture_output=True, text=True,
        )
        record(
            "PUBLIC/unprivilegierte Rolle bekommt 'permission denied' bei pos_position_owner_id()",
            result.returncode != 0 and "permission denied" in (result.stderr or "").lower(),
            f"returncode={result.returncode}, stderr={result.stderr.strip()!r}",
        )
    finally:
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-c", f"DROP ROLE IF EXISTS {role};"],
            capture_output=True, text=True,
        )


# ─────────────────────────────────────────────
# 5: Voller HTTP-Roundtrip aller 12 Endpoints MIT echtem FORCE RLS --
#    2-Konten-Cross-Access mit expliziten, getrennten Accounts
# ─────────────────────────────────────────────

def _check_write_endpoint(label, method, path_fn, payload, uid_a, uid_b, admin_id, res):
    b_res = client.request(method, path_fn(res), json=payload, headers=auth(uid_b))
    record(f"{label}: Member B (spoofed, NICHT Owner/NICHT Admin) -> 404 (IDOR unter FORCE RLS)",
           b_res.status_code == 404, f"status={b_res.status_code}")

    log_before = admin_log_count()
    admin_res = client.request(method, path_fn(res), json=payload, headers=auth(admin_id))
    record(f"{label}: Admin-Cross-Access (spoofed target={uid_a}) -> 200 unter FORCE RLS",
           admin_res.status_code == 200, f"status={admin_res.status_code}, body={admin_res.text}")
    record(f"{label}: Admin-Cross-Access protokolliert (+1)",
           admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")


def test_full_endpoint_suite_under_force_rls():
    uid_a = make_user(f"e2e-owner-{_uniq()}@example.com")
    uid_b = make_user(f"e2e-b-{_uniq()}@example.com")
    admin_id = make_user(f"e2e-admin-{_uniq()}@example.com", rolle="admin")
    assert len({uid_a, uid_b, admin_id}) == 3, "explizite, getrennte Accounts (kein ID-Zufall)"

    res = setup_a_resources(uid_a)

    _check_write_endpoint("PUT /api/positions/{id}", "PUT",
                           lambda r: f"/api/positions/{r['position_id']}",
                           {"display_name": "Umbenannt (FORCE RLS)"}, uid_a, uid_b, admin_id, res)

    res2 = setup_a_resources(uid_a)
    b_res = client.delete(f"/api/positions/{res2['position_id']}", headers=auth(uid_b))
    record("DELETE /api/positions/{id}: Member B -> 404", b_res.status_code == 404, f"status={b_res.status_code}")
    log_before = admin_log_count()
    admin_res = client.delete(f"/api/positions/{res2['position_id']}", headers=auth(admin_id))
    record("DELETE /api/positions/{id}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record("DELETE /api/positions/{id}: protokolliert (+1)", admin_log_count() == log_before + 1)

    res3 = setup_a_resources(uid_a)
    _check_write_endpoint("PUT /api/portfolios/{id}", "PUT",
                           lambda r: f"/api/portfolios/{r['portfolio_id']}",
                           {"name": "Umbenanntes Depot (FORCE RLS)"}, uid_a, uid_b, admin_id, res3)

    pf_res = client.post("/api/portfolios", json={"name": f"A-Leer-{_uniq()}", "typ": "depot"}, headers=auth(uid_a))
    pf_id = pf_res.json()["id"]
    b_res = client.delete(f"/api/portfolios/{pf_id}", headers=auth(uid_b))
    record("DELETE /api/portfolios/{id}: Member B -> 404", b_res.status_code == 404, f"status={b_res.status_code}")
    log_before = admin_log_count()
    admin_res = client.delete(f"/api/portfolios/{pf_id}", headers=auth(admin_id))
    record("DELETE /api/portfolios/{id}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record("DELETE /api/portfolios/{id}: protokolliert (+1)", admin_log_count() == log_before + 1)

    res4 = setup_a_resources(uid_a)
    _check_write_endpoint("PUT /api/transactions/{id}", "PUT",
                           lambda r: f"/api/transactions/{r['transaction_id']}",
                           {"quantity": 2}, uid_a, uid_b, admin_id, res4)

    res5 = setup_a_resources(uid_a)
    b_res = client.delete(f"/api/transactions/{res5['transaction_id']}", headers=auth(uid_b))
    record("DELETE /api/transactions/{id}: Member B -> 404", b_res.status_code == 404, f"status={b_res.status_code}")
    log_before = admin_log_count()
    admin_res = client.delete(f"/api/transactions/{res5['transaction_id']}", headers=auth(admin_id))
    record("DELETE /api/transactions/{id}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record("DELETE /api/transactions/{id}: protokolliert (+1)", admin_log_count() == log_before + 1)

    res6 = setup_a_resources(uid_a)
    b_res = client.delete(f"/api/real-estate/{res6['real_estate_id']}", headers=auth(uid_b))
    record("DELETE /api/real-estate/{id}: Member B -> 404", b_res.status_code == 404, f"status={b_res.status_code}")
    log_before = admin_log_count()
    admin_res = client.delete(f"/api/real-estate/{res6['real_estate_id']}", headers=auth(admin_id))
    record("DELETE /api/real-estate/{id}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record("DELETE /api/real-estate/{id}: protokolliert (+1)", admin_log_count() == log_before + 1)

    res7 = setup_a_resources(uid_a)
    pos_id = res7["position_id"]
    pf_id7 = res7["portfolio_id"]
    for label, path in (
        ("GET /api/positions/{id}/transactions", f"/api/positions/{pos_id}/transactions"),
        ("GET /api/tax-preview", f"/api/tax-preview?position_id={pos_id}&verkauf_preis=150"),
    ):
        b_res = client.get(path, headers=auth(uid_b))
        record(f"{label}: Member B -> 404", b_res.status_code == 404, f"status={b_res.status_code}")
        log_before = admin_log_count()
        admin_res = client.get(path, headers=auth(admin_id))
        record(f"{label}: Admin-Cross-Access -> 200", admin_res.status_code == 200,
               f"status={admin_res.status_code}, body={admin_res.text}")
        record(f"{label}: protokolliert (+1)", admin_log_count() == log_before + 1)

    b_res = client.post("/api/transactions", json={
        "portfolio_id": pf_id7, "typ": "kauf", "ticker": "BFRC", "quantity": 1, "price": 10.0, "datum": "2026-01-02",
    }, headers=auth(uid_b))
    record("POST /api/transactions: Member B -> 404", b_res.status_code == 404, f"status={b_res.status_code}")
    log_before = admin_log_count()
    admin_res = client.post("/api/transactions", json={
        "portfolio_id": pf_id7, "typ": "kauf", "ticker": "AFRC", "quantity": 1, "price": 10.0, "datum": "2026-01-02",
    }, headers=auth(admin_id))
    record("POST /api/transactions: Admin-Cross-Access -> 200", admin_res.status_code == 200,
           f"status={admin_res.status_code}, body={admin_res.text}")
    record("POST /api/transactions: protokolliert (+1)", admin_log_count() == log_before + 1)

    b_res2 = client.post("/api/positions/tagesgeld", json={
        "portfolio_id": pf_id7, "konto_name": "B-Versuch", "betrag": 100.0,
    }, headers=auth(uid_b))
    record("POST /api/positions/tagesgeld: Member B -> 404", b_res2.status_code == 404, f"status={b_res2.status_code}")
    log_before2 = admin_log_count()
    admin_res2 = client.post("/api/positions/tagesgeld", json={
        "portfolio_id": pf_id7, "konto_name": "Admin-Zugriff (FORCE RLS)", "betrag": 100.0,
    }, headers=auth(admin_id))
    record("POST /api/positions/tagesgeld: Admin-Cross-Access -> 200", admin_res2.status_code == 200,
           f"status={admin_res2.status_code}, body={admin_res2.text}")
    record("POST /api/positions/tagesgeld: protokolliert (+1)", admin_log_count() == log_before2 + 1)

    b_res3 = client.post(
        "/api/depot/import-csv", data={"portfolio_id": str(pf_id7), "broker": "comdirect"},
        files={"file": ("t.csv", b"irrelevant", "text/csv")}, headers=auth(uid_b),
    )
    record("POST /api/depot/import-csv: Member B -> 404", b_res3.status_code == 404, f"status={b_res3.status_code}")

    record("Kein hängender Kontext nach der vollen Suite (ContextVar None)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


if __name__ == "__main__":
    database.init_db()
    api.limiter.reset()
    enable_force_rls()
    print("FORCE ROW LEVEL SECURITY + Owner-Policies für pos_positions/pos_portfolios/"
          "pos_transactions/pos_real_estate aktiv (nur diese Wegwerf-DB).\n")

    test_force_rls_blocks_naive_lookup_but_not_the_function()
    print()
    test_nonexistent_id_returns_null()
    print()
    test_public_cannot_execute()
    print()
    test_full_endpoint_suite_under_force_rls()

    print("\n=== ZUSAMMENFASSUNG ===")
    fehlgeschlagen = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(fehlgeschlagen)}/{len(RESULTS)} Checks bestanden.")
    if fehlgeschlagen:
        sys.exit(1)
