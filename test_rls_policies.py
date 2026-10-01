"""
test_rls_policies.py – RLS-Umbau Chunk 5 (2026-10-01): prüft
docs/rls-policies.sql, die versionierte Gesamtdefinition aller 14
Portfolio-OS-Policies.

  1. Die Datei läuft zweimal fehlerfrei (wiederholbar, eine Transaktion).
  2. pg_policies entspricht exakt der Erwartung: 13x user_isolation (cmd ALL)
     + admin_insert_only (cmd INSERT) auf pos_admin_access_log; die 7 schon
     auf Produktion vorhandenen Policies haben WORTGLEICH dieselbe Definition
     wie dort (PROD_QUAL unten, am 2026-10-01 aus pg_policies gelesen);
     RLS an auf allen 14 Tabellen, FORCE weiterhin aus.
  3. Unter FORCE (nur in dieser Wegwerf-DB) je Tabelle: Nutzer A sieht nur
     eigene Zeilen, keine von B; INSERT für B, UPDATE/DELETE von B's Zeilen
     aus A's Kontext scheitern bzw. treffen 0 Zeilen; ohne Kontext 0 Zeilen.
  4. pos_admin_access_log (§5 Variante C): INSERT nur mit admin_user_id =
     Kontext; SELECT liefert 0 Zeilen, auch für den eintragenden Admin;
     UPDATE/DELETE treffen 0 Zeilen; der echte App-Pfad
     database.log_admin_access() schreibt den Eintrag tatsächlich (rohes
     INSERT ohne RETURNING), ein abgelehnter Eintrag erzeugt eine sichtbare
     Warnung statt still verloren zu gehen.
  5. Gleichwertigkeit (ohne FORCE, damit der alte ORM-Weg noch geht): eine
     Zeile über log_admin_access() ist identisch zu einer über den alten
     ORM-Weg (session.add) -- gleiche Spalten, gleiche Werte bis auf id und
     den Zeitpunkt in created_at (beide naiver UTC-Zeitstempel aus
     datetime.utcnow, im Messfenster); das Modell hat genau die Spalten und
     Defaults, die der rohe INSERT nachbildet.

Mehrfach gegen dieselbe DB lauffähig (RUN_ID).

Setup:
    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER rlspol_tmp WITH PASSWORD 'rlspol_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_rlspol OWNER rlspol_tmp;"
    DATABASE_URL=postgresql://rlspol_tmp:rlspol_tmp_pw@localhost:5432/portfolio_os_rlspol \\
    python3 test_rls_policies.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_rlspol;"
    sudo -u postgres psql -c "DROP OWNED BY rlspol_tmp; DROP ROLE rlspol_tmp;"
"""
import io
import json
import os
import subprocess
import sys
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_URL", "postgresql://rlspol_tmp:rlspol_tmp_pw@localhost:5432/portfolio_os_rlspol")

import database  # noqa: E402
from database import text  # noqa: E402

RESULTS = []
RUN_ID = uuid.uuid4().hex[:8]
DB = database.engine.url.database
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "rls-policies.sql")

_CTX = "(NULLIF(current_setting('app.current_user_id'::text, true), ''::text))::integer"
# Wortgleich aus pg_policies auf Produktion (2026-10-01, vor Chunk 5).
PROD_QUAL = {
    "pos_buchungen": f"(user_id = {_CTX})",
    "pos_goals": f"(user_id = {_CTX})",
    "pos_portfolios": f"(user_id = {_CTX})",
    "pos_positions": f"(portfolio_id IN ( SELECT pos_portfolios.id\n   FROM pos_portfolios\n  WHERE (pos_portfolios.user_id = {_CTX})))",
    "pos_real_estate": f"(user_id = {_CTX})",
    "pos_target_weights": f"(user_id = {_CTX})",
    "pos_tax_config": f"(user_id = {_CTX})",
}
DIREKT = ["pos_portfolios", "pos_real_estate", "pos_goals", "pos_target_weights", "pos_tax_config",
          "pos_buchungen", "pos_daily_snapshots", "pos_investment_preferences", "pos_kategorisierungsregeln",
          "pos_rebalancing_proposals", "pos_tax_events"]
UEBER_PORTFOLIO = ["pos_positions", "pos_transactions"]
ALLE_14 = DIREKT + UEBER_PORTFOLIO + ["pos_admin_access_log"]


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + str(detail)) if detail else ''}")


def su(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", DB, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def apply_sql_file() -> subprocess.CompletedProcess:
    with open(SQL_FILE) as f:
        return subprocess.run(["sudo", "-u", "postgres", "psql", "-d", DB, "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                              stdin=f, capture_output=True, text=True)


# ─────────────────────────────────────────────
# 1+2: Datei anwenden, Ergebnis gegen Erwartung
# ─────────────────────────────────────────────

def test_apply_and_definitions():
    for lauf in (1, 2):
        r = apply_sql_file()
        record(f"docs/rls-policies.sql Lauf {lauf}: fehlerfrei", r.returncode == 0, r.stderr.strip()[-200:] if r.returncode else "")

    rows = su("SELECT tablename || '|' || policyname || '|' || cmd || '|' || roles::text || '|' || permissive "
              "FROM pg_policies WHERE schemaname='public' ORDER BY 1").split("\n")
    erwartet = sorted([f"{t}|user_isolation|ALL|{{public}}|PERMISSIVE" for t in DIREKT + UEBER_PORTFOLIO]
                      + ["pos_admin_access_log|admin_insert_only|INSERT|{public}|PERMISSIVE"])
    record("pg_policies: genau die 14 erwarteten Policies (Name, cmd, Rolle, PERMISSIVE)", sorted(rows) == erwartet,
           f"fehlend={sorted(set(erwartet) - set(rows))}, zusätzlich={sorted(set(rows) - set(erwartet))}")

    abweichend = []
    for t, q in PROD_QUAL.items():
        ist = su(f"SELECT qual FROM pg_policies WHERE tablename='{t}' AND policyname='user_isolation'")
        if ist != q:
            abweichend.append((t, ist))
    record("Die 7 bestehenden Policies: Definition wortgleich mit Produktion", not abweichend, abweichend)
    for t in ("pos_transactions",):
        record(f"{t}: Definition wortgleich mit pos_positions-Muster",
               su(f"SELECT qual FROM pg_policies WHERE tablename='{t}'") == PROD_QUAL["pos_positions"])
    direkte_neu = [t for t in DIREKT if t not in PROD_QUAL]
    falsch = [t for t in direkte_neu if su(f"SELECT qual FROM pg_policies WHERE tablename='{t}'") != f"(user_id = {_CTX})"]
    record("Die 5 neuen §6-Policies: gleiches Muster wie die bestehenden", not falsch, falsch)
    wc = su("SELECT count(*) FROM pg_policies WHERE schemaname='public' AND with_check IS NOT NULL")
    record("Nur admin_insert_only hat WITH CHECK, alle anderen nur USING (wie Produktion)", wc == "1", f"with_check={wc}")
    rls = su("SELECT string_agg(relname || ':' || relrowsecurity || '/' || relforcerowsecurity, ',' ORDER BY relname) "
             f"FROM pg_class WHERE relname IN ({','.join(repr(t) for t in ALLE_14)})")
    record("RLS an auf allen 14 Tabellen, FORCE weiterhin aus (das Skript setzt kein FORCE)",
           rls.count(":true/false") == 14, rls)
    aussen = su("SELECT string_agg(relname || ':' || relrowsecurity, ',') FROM pg_class "
                "WHERE relname IN ('pos_users','pos_asset_classes','pos_family_goals')")
    record("pos_users/pos_asset_classes/pos_family_goals unberührt (RLS aus)", "true" not in aussen, aussen)


# ─────────────────────────────────────────────
# 3: Trennung je Tabelle unter FORCE
# ─────────────────────────────────────────────

def make_user(tag):
    with database.get_session() as s:
        u = database.PosUser(name=f"POL-{tag}-{RUN_ID}", email=f"pol-{tag.lower()}-{RUN_ID}@example.com",
                             rolle="admin" if tag == "ADMIN" else "member", status="active", portfolio_os_access=True)
        s.add(u)
        s.flush()
        return u.id


def insert_rows(uid) -> dict:
    """Je Tabelle eine Zeile für uid, im eigenen Kontext (wie die App). Gibt {tabelle: id}."""
    ac = su("SELECT min(id) FROM pos_asset_classes")
    ids = {}
    stmts = {
        "pos_portfolios": "INSERT INTO pos_portfolios(user_id,name,typ) VALUES (:u,'pf','depot') RETURNING id",
        "pos_real_estate": "INSERT INTO pos_real_estate(user_id,adresse,kaufpreis) VALUES (:u,'x',1) RETURNING id",
        "pos_goals": "INSERT INTO pos_goals(user_id,name,zielbetrag,zeitraum_jahre) VALUES (:u,'g',1,1) RETURNING id",
        "pos_target_weights": f"INSERT INTO pos_target_weights(user_id,asset_class_id,target_pct) VALUES (:u,{ac},0.5) RETURNING id",
        "pos_tax_config": "INSERT INTO pos_tax_config(user_id) VALUES (:u) RETURNING id",
        "pos_buchungen": "INSERT INTO pos_buchungen(user_id,datum,betrag) VALUES (:u,current_date,1) RETURNING id",
        "pos_daily_snapshots": "INSERT INTO pos_daily_snapshots(user_id,datum,gesamtvermoegen) VALUES (:u,current_date,1) RETURNING id",
        "pos_investment_preferences": "INSERT INTO pos_investment_preferences(user_id) VALUES (:u) RETURNING id",
        "pos_kategorisierungsregeln": "INSERT INTO pos_kategorisierungsregeln(user_id,empfaenger_contains,kategorie) VALUES (:u,'e','k') RETURNING id",
        "pos_rebalancing_proposals": "INSERT INTO pos_rebalancing_proposals(user_id) VALUES (:u) RETURNING id",
        "pos_tax_events": "INSERT INTO pos_tax_events(user_id,gewinn_verlust) VALUES (:u,1) RETURNING id",
    }
    with database.user_context(uid):
        with database.get_session() as s:
            for t, q in stmts.items():
                ids[t] = s.execute(text(q), {"u": uid}).scalar()
            pf = ids["pos_portfolios"]
            ids["pos_positions"] = s.execute(text("INSERT INTO pos_positions(portfolio_id,ticker) VALUES (:p,'T') RETURNING id"),
                                             {"p": pf}).scalar()
            ids["pos_transactions"] = s.execute(text("INSERT INTO pos_transactions(portfolio_id,typ) VALUES (:p,'kauf') RETURNING id"),
                                                {"p": pf}).scalar()
    return ids


def as_ctx(uid, sql, params=None):
    """Führt sql im Kontext uid (oder ohne Kontext bei uid=None) aus; gibt (ergebnis, fehler)."""
    try:
        if uid is None:
            with database.get_session() as s:
                r = s.execute(text(sql), params or {})
                return (r.scalar() if r.returns_rows else r.rowcount), None
        with database.user_context(uid):
            with database.get_session() as s:
                r = s.execute(text(sql), params or {})
                return (r.scalar() if r.returns_rows else r.rowcount), None
    except Exception as e:  # noqa: BLE001
        return None, str(e).splitlines()[0]


def test_isolation_per_table(a, b, rows_a, rows_b):
    for t in DIREKT + UEBER_PORTFOLIO:
        eigen, _ = as_ctx(a, f"SELECT count(*) FROM {t} WHERE id = :i", {"i": rows_a[t]})
        fremd, _ = as_ctx(a, f"SELECT count(*) FROM {t} WHERE id = :i", {"i": rows_b[t]})
        ohne, _ = as_ctx(None, f"SELECT count(*) FROM {t} WHERE id IN (:i, :j)", {"i": rows_a[t], "j": rows_b[t]})
        upd, _ = as_ctx(a, f"UPDATE {t} SET id = id WHERE id = :i", {"i": rows_b[t]})
        dele, _ = as_ctx(a, f"DELETE FROM {t} WHERE id = :i", {"i": rows_b[t]})
        if t in UEBER_PORTFOLIO:
            spalte = "portfolio_id,ticker" if t == "pos_positions" else "portfolio_id,typ"
            wert = "'T'" if t == "pos_positions" else "'kauf'"
            _, ins_err = as_ctx(a, f"INSERT INTO {t}({spalte}) VALUES (:p,{wert})", {"p": rows_b["pos_portfolios"]})
        else:
            # WITH CHECK (= USING): eine eigene Zeile darf nicht an B umgehängt werden
            ins_err = as_ctx(a, f"UPDATE {t} SET user_id = :b WHERE id = :i", {"b": b, "i": rows_a[t]})[1]
        ok = eigen == 1 and fremd == 0 and ohne == 0 and upd == 0 and dele == 0 and ins_err and "row-level security" in ins_err
        record(f"{t}: A sieht eigene (1) / fremde (0) / ohne Kontext (0); UPDATE+DELETE fremd = 0; "
               f"{'INSERT in B-Portfolio' if t in UEBER_PORTFOLIO else 'eigene Zeile an B umhängen'} abgelehnt",
               ok, f"eigen={eigen} fremd={fremd} ohne={ohne} upd={upd} del={dele} schreib_fremd={(ins_err or 'KEIN FEHLER')[:60]}")
    still_da = su("SELECT count(*) FROM pos_portfolios WHERE id = %d" % rows_b["pos_portfolios"])
    record("B's Daten nach allen Angriffsversuchen aus A's Kontext unverändert vorhanden", still_da == "1")


# ─────────────────────────────────────────────
# 4: pos_admin_access_log nur INSERT (Variante C)
# ─────────────────────────────────────────────

def test_admin_access_log(admin, a):
    zaehlen = lambda: int(su(f"SELECT count(*) FROM pos_admin_access_log WHERE admin_user_id = {admin}"))  # noqa: E731
    vorher = zaehlen()
    n, err = as_ctx(admin, "INSERT INTO pos_admin_access_log(admin_user_id,target_user_id,endpoint,method) "
                           "VALUES (:a,:t,'/pol-test','GET')", {"a": admin, "t": a})
    record("access_log: INSERT mit admin_user_id = Kontext erlaubt", n == 1 and err is None, err or "")
    _, err = as_ctx(admin, "INSERT INTO pos_admin_access_log(admin_user_id,target_user_id,endpoint,method) "
                           "VALUES (:x,:t,'/pol-fake','GET')", {"x": a, "t": a})
    record("access_log: INSERT im Namen eines anderen Admins abgelehnt", bool(err) and "row-level security" in err, err)
    _, err = as_ctx(None, "INSERT INTO pos_admin_access_log(admin_user_id,target_user_id,endpoint,method) "
                          "VALUES (:a,:t,'/pol-nocontext','GET')", {"a": admin, "t": a})
    record("access_log: INSERT ohne Kontext abgelehnt", bool(err) and "row-level security" in err, err)
    sicht, _ = as_ctx(admin, "SELECT count(*) FROM pos_admin_access_log")
    record("access_log: SELECT liefert 0 Zeilen -- auch für den eintragenden Admin selbst", sicht == 0, f"sichtbar={sicht}")
    upd, _ = as_ctx(admin, "UPDATE pos_admin_access_log SET endpoint = 'x' WHERE admin_user_id = :a", {"a": admin})
    dele, _ = as_ctx(admin, "DELETE FROM pos_admin_access_log WHERE admin_user_id = :a", {"a": admin})
    record("access_log: UPDATE und DELETE treffen 0 Zeilen (nur erweiterbar)", upd == 0 and dele == 0, f"upd={upd}, del={dele}")
    record("access_log: Einträge existieren weiterhin (Superuser-Sicht)", zaehlen() == vorher + 1, f"{vorher} -> {zaehlen()}")

    vorher = zaehlen()
    with database.user_context(admin):
        database.log_admin_access(admin, a, "/pol-log_admin_access", "GET")
    record("access_log: echter App-Pfad database.log_admin_access() schreibt den Eintrag tatsächlich",
           zaehlen() == vorher + 1, f"{vorher} -> {zaehlen()}")

    # Abgelehnter Eintrag (Kontext != admin_user_id): keine Exception, aber sichtbare Warnung.
    buf = io.StringIO()
    vorher = zaehlen()
    try:
        with redirect_stdout(buf):
            with database.user_context(a):
                database.log_admin_access(admin, a, "/pol-abgelehnt", "GET")
        exc = None
    except Exception as e:  # noqa: BLE001
        exc = repr(e)
    record("access_log: abgelehnter Eintrag -> keine Exception, aber sichtbare Warnung, nichts geschrieben",
           exc is None and "NICHT protokolliert" in buf.getvalue() and zaehlen() == vorher,
           buf.getvalue().strip()[:120] or exc)


# ─────────────────────────────────────────────
# 5: rohes INSERT == alter ORM-Weg (ohne FORCE)
# ─────────────────────────────────────────────

ROH_SPALTEN = ["admin_user_id", "target_user_id", "endpoint", "method", "created_at"]


def test_raw_insert_equivalent_to_orm(admin, a):
    tbl = database.PosAdminAccessLog.__table__
    nicht_pk = [c.name for c in tbl.columns if not c.primary_key]
    mit_default = sorted(c.name for c in tbl.columns
                         if c.default is not None or c.server_default is not None
                         or c.onupdate is not None or c.server_onupdate is not None)
    record("Modell: Nicht-PK-Spalten = genau die Spalten des rohen INSERT", nicht_pk == ROH_SPALTEN, nicht_pk)
    record("Modell: einziger ORM-/Server-Default ist created_at (wird im rohen INSERT nachgebildet)",
           mit_default == ["created_at"], mit_default)
    db_defaults = su("SELECT string_agg(column_name || '=' || coalesce(column_default,'-'), ',' ORDER BY ordinal_position) "
                     "FROM information_schema.columns WHERE table_name='pos_admin_access_log'")
    record("DB: keine Spalten-Defaults außer der id-Sequenz", db_defaults.count("nextval") == 1
           and db_defaults.replace("id=nextval('pos_admin_access_log_id_seq'::regclass)", "").count("=-") == 5, db_defaults)

    args = dict(admin_user_id=admin, target_user_id=a, endpoint=f"/pol-equiv-{RUN_ID}", method="PUT")
    t0 = datetime.utcnow()
    with database.user_context(admin):
        with database.get_session() as s:      # ALTER Weg (bis 2026-10-01 in log_admin_access)
            s.add(database.PosAdminAccessLog(**args))
        database.log_admin_access(**args)      # NEUER Weg (rohes INSERT)
    t1 = datetime.utcnow()
    zeilen = [json.loads(z) for z in su(
        "SELECT row_to_json(x) FROM (SELECT * FROM pos_admin_access_log WHERE endpoint = "
        f"'{args['endpoint']}' ORDER BY id) x").split("\n")]
    record("Gleichwertigkeit: genau 2 Zeilen (alt ORM, neu roh)", len(zeilen) == 2, len(zeilen))
    if len(zeilen) != 2:
        return
    alt, neu = zeilen
    record("Gleichwertigkeit: identische Spaltenmenge", list(alt) == list(neu), f"alt={list(alt)}, neu={list(neu)}")
    abw = {k: (alt[k], neu[k]) for k in alt if k not in ("id", "created_at") and alt[k] != neu[k]}
    record("Gleichwertigkeit: alle Werte außer id/created_at identisch", not abw, abw or {k: neu[k] for k in ROH_SPALTEN[:4]})
    ca_alt, ca_neu = datetime.fromisoformat(alt["created_at"]), datetime.fromisoformat(neu["created_at"])
    record("Gleichwertigkeit: created_at bei beiden gesetzt, naiv (ohne Zeitzone) und im UTC-Messfenster",
           ca_alt.tzinfo is None and ca_neu.tzinfo is None
           and t0 - timedelta(seconds=1) <= ca_alt <= ca_neu <= t1 + timedelta(seconds=1),
           f"alt={ca_alt}, neu={ca_neu}, fenster=[{t0}, {t1}]")
    record("Gleichwertigkeit: id fortlaufend aus derselben Sequenz", neu["id"] == alt["id"] + 1, f"{alt['id']} -> {neu['id']}")


if __name__ == "__main__":
    database.init_db()
    print(f"RUN_ID={RUN_ID}, DB={DB}\n")
    test_apply_and_definitions()
    print()
    a, b, admin = make_user("A"), make_user("B"), make_user("ADMIN")
    rows_a, rows_b = insert_rows(a), insert_rows(b)
    test_raw_insert_equivalent_to_orm(admin, a)
    print()
    for t in ALLE_14:
        su(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    print("FORCE ROW LEVEL SECURITY auf allen 14 Tabellen (nur diese Wegwerf-DB)\n")
    try:
        test_isolation_per_table(a, b, rows_a, rows_b)
        print()
        test_admin_access_log(admin, a)
    finally:
        for t in ALLE_14:
            su(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
    record("Kein hängender Kontext am Ende", database.current_user_context() is None)

    print("\n=== ZUSAMMENFASSUNG ===")
    fehlgeschlagen = [r for r in RESULTS if not r[1]]
    for name, ok, _ in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(fehlgeschlagen)}/{len(RESULTS)} Checks bestanden.")
    if fehlgeschlagen:
        sys.exit(1)
