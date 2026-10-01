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

2026-10-01: 5. Funktion pos_buchung_owner_id() / api._buchung_owner_id() für
PUT /api/haushaltsbuch/{buchung_id} (13. Endpoint, vorher _require_buchung_access()
mit session.get() und OHNE Kontext-Umschaltung vor dem Schreiben). Gruppe 6
unten reproduziert den alten Bug (alter Codepfad -> 404, Admin-UPDATE trifft
0 Zeilen) und beweist den Fix (1 Zeile, Kategorisierungsregel im Owner-Kontext).

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
  5. Voller HTTP-Roundtrip der 12 ursprünglichen Endpoints MIT echtem FORCE
     RLS (Obermenge von test_rls_admin_bypass_helpers.py, dort ohne FORCE):
     2-Konten-Cross-Access mit expliziten, getrennten Accounts (uid_a
     Owner, uid_b NICHT Owner/NICHT Admin) -- Member B -> 404 (IDOR-Schutz
     unter FORCE RLS weiterhin intakt), Admin-Cross-Access -> 200 + geloggt
     (nur dank der SECURITY DEFINER Funktionen möglich).
  6. 13. Endpoint PUT /api/haushaltsbuch/{buchung_id} (FORCE RLS auch auf
     pos_buchungen + pos_kategorisierungsregeln): Regression des alten
     Codepfads, Member B -> 404, Admin -> 200 + geänderte Zeile + Regel mit
     user_id des Owners (beweist, dass add_kategorisierungsregel() im
     umgeschalteten Kontext läuft).

VORAUSSETZUNG (zusätzlich zum Setup unten): die 5 Funktionen müssen bereits
der BYPASSRLS-Rolle pos_owner_lookup_bypass gehören (docs/rls-owner-lookup-
bypass-role-setup.sql) -- ohne diesen manuellen Schritt schlagen die
Sanity-Checks in Gruppe 2 (und in Gruppe 5 die Admin-Cross-Access-Checks)
erwartungsgemäß fehl, weil die Funktionen dann selbst noch RLS-gefiltert
wären (SECURITY DEFINER allein bypassed nichts, siehe database.py-Kommentar).

Mehrfach hintereinander gegen DIESELBE DB lauffähig (seit 2026-10-01: Test-
User-E-Mails und Hilfsrolle tragen eine pro Lauf eindeutige RUN_ID, Teardown
der Hilfsrolle entzieht vorher CONNECT) -- Drop/Neuanlage der DB zwischen zwei
Läufen ist nicht nötig.

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
      GRANT pos_owner_lookup_bypass TO ownerlookup_tmp WITH INHERIT TRUE, SET FALSE;
      ALTER FUNCTION pos_position_owner_id(integer)    OWNER TO pos_owner_lookup_bypass;
      ALTER FUNCTION pos_portfolio_owner_id(integer)   OWNER TO pos_owner_lookup_bypass;
      ALTER FUNCTION pos_transaction_owner_id(integer) OWNER TO pos_owner_lookup_bypass;
      ALTER FUNCTION pos_real_estate_owner_id(integer) OWNER TO pos_owner_lookup_bypass;
      ALTER FUNCTION pos_buchung_owner_id(integer)     OWNER TO pos_owner_lookup_bypass;
      GRANT SELECT ON pos_portfolios, pos_positions, pos_transactions, pos_real_estate, pos_buchungen
          TO pos_owner_lookup_bypass;
      GRANT EXECUTE ON FUNCTION pos_position_owner_id(integer)    TO ownerlookup_tmp;
      GRANT EXECUTE ON FUNCTION pos_portfolio_owner_id(integer)   TO ownerlookup_tmp;
      GRANT EXECUTE ON FUNCTION pos_transaction_owner_id(integer) TO ownerlookup_tmp;
      GRANT EXECUTE ON FUNCTION pos_real_estate_owner_id(integer) TO ownerlookup_tmp;
      GRANT EXECUTE ON FUNCTION pos_buchung_owner_id(integer)     TO ownerlookup_tmp;
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
import uuid
from types import SimpleNamespace

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://ownerlookup_tmp:ownerlookup_tmp_pw@localhost:5432/portfolio_os_ownerlookup",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")

import api  # noqa: E402
import database  # noqa: E402
from database import text  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

RESULTS = []
client = TestClient(api.app, base_url="https://testserver")
pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")
_counter = {"n": 0}

# Pro Lauf eindeutige Kennung für Test-User-E-Mails und die Hilfsrolle in
# Gruppe 4: die Suite kann so mehrfach gegen DIESELBE DB laufen, ohne mit
# Usern/Daten früherer Läufe zu kollidieren (früher: make_user() löschte per
# E-Mail und scheiterte am Fremdschlüssel der Portfolios des Vorlaufs). Bewusst
# NICHT in _uniq(), das auch Ticker (String(20)) erzeugt.
RUN_ID = uuid.uuid4().hex[:8]

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
    local, domain = email.split("@", 1)
    email = f"{local}-{RUN_ID}@{domain}"
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


def make_buchung(uid: int) -> int:
    """Legt eine Buchung für uid an (im eigenen Kontext, wie der echte
    Kontoauszug-Import) und gibt ihre id zurück."""
    empfaenger = f"Test-Empfaenger-{_uniq()}"
    with database.user_context(uid):
        neu = database.save_buchungen(uid, [{
            "datum": "2026-04-01", "betrag": 12.34, "empfaenger": empfaenger,
            "typ": "ausgabe", "kategorie": "Sonstiges",
        }])
        assert neu == 1, neu
        with database.get_session() as session:
            return session.query(database.PosBuchung).filter_by(
                user_id=uid, empfaenger=empfaenger).one().id


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
        # pos_buchungen (Produktion: Policy existiert schon, nur FORCE fehlt) und
        # pos_kategorisierungsregeln (Policy laut Plan-Dokument Abschnitt 6, Chunk 5):
        # direktes user_id. Die Policy ohne WITH CHECK gilt auch für INSERT/UPDATE
        # -- eine Regel mit fremder user_id unter falschem Kontext wird abgelehnt.
        "ALTER TABLE pos_buchungen ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE pos_buchungen FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS user_isolation ON pos_buchungen",
        """CREATE POLICY user_isolation ON pos_buchungen USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
        )""",
        "ALTER TABLE pos_kategorisierungsregeln ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE pos_kategorisierungsregeln FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS user_isolation ON pos_kategorisierungsregeln",
        """CREATE POLICY user_isolation ON pos_kategorisierungsregeln USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
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
    res["buchung_id"] = make_buchung(uid_a)

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
            ("pos_buchung_owner_id", "pos_buchung_owner_id", "buchung_id"),
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
        record("api._buchung_owner_id() liefert echten Owner",
               api._buchung_owner_id(res["buchung_id"]) == uid_a)


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
        record("api._buchung_owner_id(999999999) -> None",
               api._buchung_owner_id(999999999) is None)


# ─────────────────────────────────────────────
# 4: PUBLIC kann die Funktionen NICHT ausführen
# ─────────────────────────────────────────────

def _psql_su(*args, db=None):
    cmd = ["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-At"]
    if db:
        cmd += ["-d", db]
    return subprocess.run(cmd + list(args), capture_output=True, text=True)


def _drop_unpriv_role(role: str) -> subprocess.CompletedProcess:
    """CONNECT-Recht zuerst entziehen -- solange es besteht, schlägt DROP ROLE
    mit 'role ... cannot be dropped because some objects depend on it' fehl
    (die Rolle blieb früher still liegen und kollidierte im nächsten Lauf)."""
    _psql_su("-c", f"REVOKE CONNECT ON DATABASE {_TEST_DB} FROM {role};", db=_TEST_DB)
    return _psql_su("-c", f"DROP ROLE IF EXISTS {role};")


def test_public_cannot_execute():
    # Reste aus Läufen VOR diesem Fix (ohne REVOKE im Teardown) wegräumen.
    leftovers = _psql_su("-c", "SELECT rolname FROM pg_roles WHERE rolname LIKE 'ownerlookup\\_unpriv\\_%';")
    for old in leftovers.stdout.split():
        _drop_unpriv_role(old)
    role = f"ownerlookup_unpriv_{RUN_ID}"
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
        dropped = _drop_unpriv_role(role)
        record(f"Teardown: Hilfsrolle {role} wieder gelöscht",
               dropped.returncode == 0 and _psql_su(
                   "-c", f"SELECT count(*) FROM pg_roles WHERE rolname = '{role}';").stdout.strip() == "0",
               dropped.stderr.strip())


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


# ─────────────────────────────────────────────
# 6: 13. Endpoint PUT /api/haushaltsbuch/{buchung_id} -- Regression des alten
#    Codepfads + 2-Konten-Cross-Access + Kategorisierungsregel im Owner-Kontext
# ─────────────────────────────────────────────

def _old_update_buchung(buchung_id: int, current_user, kategorie: str) -> None:
    """Wörtlicher Nachbau des Codepfads VOR 2026-10-01 (update_buchung +
    _require_buchung_access): Lookup per session.get() unter dem Kontext des
    Aufrufers, Admin wird nur protokolliert, KEINE Kontext-Umschaltung."""
    with database.get_session() as session:
        buchung = session.get(database.PosBuchung, buchung_id)
        if buchung is None:
            raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
        if buchung.user_id != current_user.id:
            if current_user.rolle != "admin":
                raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
            database.log_admin_access(current_user.id, buchung.user_id, "/old", "PUT")
        buchung.kategorie = kategorie


def _buchung_row(buchung_id: int, owner_id: int):
    with database.user_context(owner_id):
        with database.get_session() as session:
            b = session.get(database.PosBuchung, buchung_id)
            return (b.kategorie, b.user_id) if b else None


def _regeln(owner_id: int, empfaenger: str) -> list:
    with database.user_context(owner_id):
        with database.get_session() as session:
            return [(r.user_id, r.kategorie) for r in session.query(database.PosKategorisierungsregel)
                    .filter_by(empfaenger_contains=empfaenger).all()]


def test_buchung_endpoint_under_force_rls():
    uid_a = make_user(f"hb-owner-{_uniq()}@example.com")
    uid_b = make_user(f"hb-b-{_uniq()}@example.com")
    admin_id = make_user(f"hb-admin-{_uniq()}@example.com", rolle="admin")
    assert len({uid_a, uid_b, admin_id}) == 3, "explizite, getrennte Accounts (kein ID-Zufall)"
    admin = SimpleNamespace(id=admin_id, rolle="admin")
    buchung_id = make_buchung(uid_a)

    # 6a) Regression: alter Codepfad unter FORCE RLS -> Admin bekommt 404.
    with database.user_context(admin_id):
        try:
            _old_update_buchung(buchung_id, admin, "ALT-PFAD")
            old_status = 200
        except HTTPException as e:
            old_status = e.status_code
    record("Regression: ALTER Codepfad (session.get unter Admin-Kontext) -> 404 unter FORCE RLS "
           "(Bug reproduziert)", old_status == 404, f"status={old_status}")
    record("Regression: alter Codepfad hat die Buchung NICHT geändert",
           _buchung_row(buchung_id, uid_a)[0] == "Sonstiges", f"row={_buchung_row(buchung_id, uid_a)}")

    # 6b) Rohes UPDATE: 0 Zeilen unter Admin-Kontext, 1 Zeile nach Umschaltung
    #     über exakt den Helferpfad des neuen Endpoints.
    with database.user_context(admin_id):
        with database.get_session() as session:
            rc_admin = session.execute(text("UPDATE pos_buchungen SET kategorie = kategorie WHERE id = :id"),
                                       {"id": buchung_id}).rowcount
        api._switch_context_for_admin_write(api._buchung_owner_id(buchung_id), admin, "/test", "PUT")
        with database.get_session() as session:
            rc_switched = session.execute(text("UPDATE pos_buchungen SET kategorie = kategorie WHERE id = :id"),
                                          {"id": buchung_id}).rowcount
    record("Regression: Admin-UPDATE OHNE Umschaltung trifft 0 Zeilen (alter Zustand)",
           rc_admin == 0, f"rowcount={rc_admin}")
    record("Fix: Admin-UPDATE NACH _buchung_owner_id + _switch_context_for_admin_write trifft 1 Zeile",
           rc_switched == 1, f"rowcount={rc_switched}")
    record("Kein hängender Kontext nach 6b", database._current_user_ctx.get() is None)

    with database.user_context(uid_a):
        with database.get_session() as session:
            empfaenger = session.get(database.PosBuchung, buchung_id).empfaenger

    # 6c) Gegenprobe: eine Regel mit user_id=A unter Admin-Kontext wird von der
    #     Policy abgelehnt -- d.h. ein Erfolg in 6e geht NUR im umgeschalteten Kontext.
    with database.user_context(admin_id):
        try:
            database.add_kategorisierungsregel(uid_a, f"gegenprobe-{empfaenger}", "X")
            regel_falscher_kontext = "eingefügt"
        except Exception as e:  # noqa: BLE001
            regel_falscher_kontext = type(e).__name__ + ": " + str(e).splitlines()[0]
    record("Gegenprobe: Regel für A unter Admin-Kontext wird von FORCE RLS abgelehnt",
           "row-level security" in regel_falscher_kontext, regel_falscher_kontext)

    payload = {"kategorie": "Versicherung", "immer_so_kategorisieren": True}

    # 6d) Member B (nicht Owner, nicht Admin) -> 404, nichts geändert, keine Regel.
    b_res = client.put(f"/api/haushaltsbuch/{buchung_id}", json=payload, headers=auth(uid_b))
    record("PUT /api/haushaltsbuch/{id}: Member B -> 404 (IDOR unter FORCE RLS)",
           b_res.status_code == 404, f"status={b_res.status_code}")
    record("PUT /api/haushaltsbuch/{id}: Member B hat nichts geändert",
           _buchung_row(buchung_id, uid_a)[0] == "Sonstiges" and _regeln(uid_a, empfaenger) == [],
           f"row={_buchung_row(buchung_id, uid_a)}, regeln={_regeln(uid_a, empfaenger)}")

    # 6e) Admin-Cross-Access -> 200, Zeile geändert, Regel mit user_id=A, geloggt.
    log_before = admin_log_count()
    admin_res = client.put(f"/api/haushaltsbuch/{buchung_id}", json=payload, headers=auth(admin_id))
    record("PUT /api/haushaltsbuch/{id}: Admin-Cross-Access -> 200 unter FORCE RLS",
           admin_res.status_code == 200, f"status={admin_res.status_code}, body={admin_res.text}")
    record("PUT /api/haushaltsbuch/{id}: Buchung tatsächlich geändert (Owner unverändert A)",
           _buchung_row(buchung_id, uid_a) == ("Versicherung", uid_a), f"row={_buchung_row(buchung_id, uid_a)}")
    record("PUT /api/haushaltsbuch/{id}: Kategorisierungsregel mit user_id=A angelegt "
           "(add_kategorisierungsregel lief im umgeschalteten Kontext)",
           _regeln(uid_a, empfaenger) == [(uid_a, "Versicherung")], f"regeln={_regeln(uid_a, empfaenger)}")
    record("PUT /api/haushaltsbuch/{id}: protokolliert (+1)",
           admin_log_count() == log_before + 1, f"vorher={log_before}, nachher={admin_log_count()}")

    # 6f) Owner selbst -> 200, nicht protokolliert; nicht existierende ID -> 404.
    log_before = admin_log_count()
    a_res = client.put(f"/api/haushaltsbuch/{buchung_id}", json={"kategorie": "Haushalt"}, headers=auth(uid_a))
    record("PUT /api/haushaltsbuch/{id}: Owner selbst -> 200, nicht protokolliert",
           a_res.status_code == 200 and admin_log_count() == log_before
           and _buchung_row(buchung_id, uid_a)[0] == "Haushalt", f"status={a_res.status_code}")
    for who, uid in (("Admin", admin_id), ("Member B", uid_b)):
        r = client.put("/api/haushaltsbuch/999999999", json=payload, headers=auth(uid))
        record(f"PUT /api/haushaltsbuch/999999999: {who} -> 404", r.status_code == 404, f"status={r.status_code}")

    record("Kein hängender Kontext nach Gruppe 6", database._current_user_ctx.get() is None)


if __name__ == "__main__":
    database.init_db()
    api.limiter.reset()
    enable_force_rls()
    print("FORCE ROW LEVEL SECURITY + Owner-Policies für pos_positions/pos_portfolios/"
          "pos_transactions/pos_real_estate/pos_buchungen/pos_kategorisierungsregeln "
          "aktiv (nur diese Wegwerf-DB).\n")

    test_force_rls_blocks_naive_lookup_but_not_the_function()
    print()
    test_nonexistent_id_returns_null()
    print()
    test_public_cannot_execute()
    print()
    test_full_endpoint_suite_under_force_rls()
    print()
    test_buchung_endpoint_under_force_rls()

    print("\n=== ZUSAMMENFASSUNG ===")
    fehlgeschlagen = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(fehlgeschlagen)}/{len(RESULTS)} Checks bestanden.")
    if fehlgeschlagen:
        sys.exit(1)
