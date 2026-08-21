"""
test_rls_context.py – Test für den RLS-Session-Kontext-Mechanismus (Chunk 1,
2026-08-21, siehe docs/rls-force-umbau-plan-21-08.md): database.user_context()
+ die dadurch kontextsensitive database.get_session().

WICHTIG: Dieser Chunk aktiviert noch KEIN FORCE ROW LEVEL SECURITY -- die
Tests hier verifizieren NUR, dass app.current_user_id korrekt gesetzt und
zurückgesetzt wird und sich bei parallelen Requests nicht überschreibt. Ob
das tatsächlich Zugriffe einschränkt, testet ein späterer Chunk (Policies +
FORCE, gegen eine isolierte Kopie, siehe Plan-Dokument Chunk 6/7) -- ohne
FORCE bleibt jeder Datenzugriff hier weiterhin ausschließlich durch die
bestehende Query-seitige user_id-Filterung geschützt, nicht durch Postgres.

Deckt ab:
  1. user_context(A) -> current_setting('app.current_user_id') zeigt 'A'.
     Wechsel zu user_context(B) -> zeigt 'B'. Ohne aktiven Kontext -> leer.
  2. Verschachtelung: nach einem inneren Block wird der äußere Kontext exakt
     wiederhergestellt (nicht None/verloren).
  3. Leck-Test: nach Ende eines user_context()-Blocks darf ein get_session()-
     Aufruf AUSSERHALB jedes Kontexts den zuletzt gesetzten Wert nicht mehr
     sehen (weder über die ContextVar direkt noch über current_setting()).
  4. Regressionstest: bestehende, in diesem Chunk NICHT geänderte Endpoints
     (Login, /api/positions, /api/user/alpaca-status) funktionieren
     unverändert -- dieser Chunk darf keine bestehende Funktionalität
     brechen, da RLS noch nicht erzwungen wird.
  5. Concurrency: zwei echte, gleichzeitige HTTP-Requests (Threads) als
     unterschiedliche Nutzer gegen den echten Request-Pfad
     (_apply_user_context-Dependency + Threadpool-Dispatch für sync-def-
     Endpoints, wie es unter uvicorn tatsächlich läuft) -- jede Antwort darf
     ausschließlich die Daten des jeweils anfragenden Nutzers zeigen.

KEINE Produktions-DB (analog test_product_scope.py):
    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER rlsctx_tmp WITH PASSWORD 'rlsctx_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_rlsctx OWNER rlsctx_tmp;"
    DATABASE_URL=postgresql://rlsctx_tmp:rlsctx_tmp_pw@localhost:5432/portfolio_os_rlsctx \
    JWT_SECRET_KEY=test-secret-key-for-local-testing-only \
    python3 test_rls_context.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_rlsctx;"
    sudo -u postgres psql -c "DROP USER rlsctx_tmp;"
"""
import os
import sys
import threading
import traceback

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://rlsctx_tmp:rlsctx_tmp_pw@localhost:5432/portfolio_os_rlsctx",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-testing-only")
os.environ.setdefault("ALERT_EMAIL", "")

import api  # noqa: E402
import database  # noqa: E402
from database import text  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

RESULTS = []
# https zwingend: api.py setzt das Login-Cookie mit Secure=true (siehe
# test_product_scope.py) -- hier größtenteils irrelevant, da wir über Bearer-
# Header statt Cookie authentifizieren, aber der Login-Regressionstest selbst
# braucht es trotzdem.
client = TestClient(api.app, base_url="https://testserver")
pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def current_setting_via_db() -> str | None:
    with database.get_session() as session:
        return session.execute(text("SELECT current_setting('app.current_user_id', true)")).scalar()


def make_user(email: str, *, password: str = "TestPassword123") -> int:
    with database.get_session() as session:
        session.query(database.PosUser).filter_by(email=email).delete()
    with database.get_session() as session:
        u = database.PosUser(
            name="RLS Ctx Test", email=email, password_hash=pwd_context.hash(password),
            rolle="member", status="active", trading_bot_access=True, portfolio_os_access=True,
        )
        session.add(u)
        session.flush()
        return u.id


def mint_token(user_id: int) -> str:
    return api.create_access_token({"sub": str(user_id)})


# ─────────────────────────────────────────────
# 1-3: ContextVar-Mechanismus direkt (kein HTTP)
# ─────────────────────────────────────────────

def test_context_set_switch_and_no_leak():
    val_none = current_setting_via_db()
    record("Ohne aktiven Kontext: current_setting leer/NULL", not val_none, f"val={val_none!r}")

    with database.user_context(1):
        record("user_context(1): ContextVar direkt zeigt 1",
               database._current_user_ctx.get() == 1, f"got={database._current_user_ctx.get()!r}")
        val_a = current_setting_via_db()
        record("user_context(1): current_setting zeigt '1'", val_a == "1", f"val={val_a!r}")

    with database.user_context(9):
        val_b = current_setting_via_db()
        record("user_context(9): current_setting zeigt '9'", val_b == "9", f"val={val_b!r}")

    val_after = current_setting_via_db()
    ctxvar_after = database._current_user_ctx.get()
    record("Nach Blockende: ContextVar zurückgesetzt (None, nicht 9)", ctxvar_after is None,
           f"got={ctxvar_after!r}")
    record("Nach Blockende: current_setting KEIN Leck (leer/NULL, nicht mehr '9')",
           not val_after, f"val={val_after!r}")


def test_nested_context_restores_outer():
    with database.user_context(1):
        with database.user_context(9):
            record("Innerer Block: ContextVar zeigt 9", database._current_user_ctx.get() == 9)
        record("Nach innerem Block: äußerer Kontext (1) wiederhergestellt, nicht None",
               database._current_user_ctx.get() == 1, f"got={database._current_user_ctx.get()!r}")
    record("Nach äußerem Block: zurück auf None",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


def test_exception_inside_block_still_resets():
    try:
        with database.user_context(1):
            raise RuntimeError("absichtlicher Testfehler")
    except RuntimeError:
        pass
    record("Nach Exception INNERHALB des Blocks: Kontext trotzdem zurückgesetzt (try/finally)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


# ─────────────────────────────────────────────
# 4: Regressionstest -- unveränderte Endpoints funktionieren weiter
# ─────────────────────────────────────────────

def test_existing_endpoints_still_work():
    api.limiter.reset()
    uid = make_user("regression@example.com")
    token = mint_token(uid)
    headers = {"Authorization": f"Bearer {token}"}

    res_login = client.post("/api/auth/login", data={"username": "regression@example.com", "password": "TestPassword123"})
    record("Login funktioniert weiterhin (200)", res_login.status_code == 200, f"status={res_login.status_code}")
    # get_current_user() bevorzugt das Cookie vor einem Authorization: Bearer-
    # Header (siehe api.py) -- ohne dieses Clear würde das gerade gesetzte
    # Login-Cookie JEDEN späteren Bearer-authentifizierten Aufruf über diesen
    # gemeinsam genutzten `client` heimlich als "regression@example.com"
    # laufen lassen, egal welcher Bearer-Token übergeben wird (genau das ist
    # bei der ersten Testversion passiert und hat zu falsch zugeordneten
    # Immobilien im Concurrency-Test weiter unten geführt -- Testbug, kein
    # Bug im Kontext-Mechanismus selbst, siehe Bericht).
    client.cookies.clear()

    res_pos = client.get("/api/positions", headers=headers)
    record("GET /api/positions funktioniert weiterhin (200, leeres Depot)",
           res_pos.status_code == 200, f"status={res_pos.status_code}, body={res_pos.text[:200]}")

    res_alpaca = client.get("/api/user/alpaca-status", headers=headers)
    record("GET /api/user/alpaca-status (protected_shared) funktioniert weiterhin (200)",
           res_alpaca.status_code == 200, f"status={res_alpaca.status_code}, body={res_alpaca.text[:200]}")

    # Nach dem Request darf kein Kontext mehr "hängen" (Dependency muss über
    # den Exit-Stack sauber zurückgesetzt haben, s. _apply_user_context).
    record("Nach dem Request: kein hängender Kontext mehr (ContextVar None)",
           database._current_user_ctx.get() is None, f"got={database._current_user_ctx.get()!r}")


# ─────────────────────────────────────────────
# 5: Concurrency -- zwei echte gleichzeitige Requests, unterschiedliche Nutzer
# ─────────────────────────────────────────────

def test_concurrent_requests_no_context_bleed():
    api.limiter.reset()
    client.cookies.clear()  # defensiv, s. Kommentar in test_existing_endpoints_still_work
    uid_a = make_user("ctx-a@example.com")
    uid_b = make_user("ctx-b@example.com")
    token_a = mint_token(uid_a)
    token_b = mint_token(uid_b)

    res_a = client.post("/api/real-estate", json={"adresse": "Adresse-A-geheim", "kaufpreis": 100000},
                         headers={"Authorization": f"Bearer {token_a}"})
    res_b = client.post("/api/real-estate", json={"adresse": "Adresse-B-geheim", "kaufpreis": 200000},
                         headers={"Authorization": f"Bearer {token_b}"})
    record("Setup: Immobilie für Nutzer A angelegt", res_a.status_code == 200, f"status={res_a.status_code}")
    record("Setup: Immobilie für Nutzer B angelegt", res_b.status_code == 200, f"status={res_b.status_code}")

    results = {}
    errors = {}

    def call(label, token):
        # Eigene TestClient-Instanz pro Thread (statt des oben gemeinsam
        # genutzten `client`): Starlettes TestClient hängt an einem einzelnen
        # Portal-/Event-Loop-Thread und ist NICHT dafür ausgelegt, von
        # mehreren externen Python-Threads gleichzeitig auf demselben
        # Client-Objekt genutzt zu werden -- ein erster Versuch mit
        # gemeinsamem Client zeigte Response-Vertauschungen zwischen den
        # Threads (Testartefakt, siehe Bericht), nicht ein Leck im
        # ContextVar-Mechanismus selbst (der über die direkten Tests oben
        # bereits isoliert verifiziert ist). Getrennte Client-Instanzen
        # bilden zwei echte, unabhängige Verbindungen ab -- näher an realen
        # gleichzeitigen HTTP-Clients als ein geteiltes Objekt ohnehin wäre.
        thread_client = TestClient(api.app, base_url="https://testserver")
        try:
            for _ in range(5):
                res = thread_client.get("/api/real-estate", headers={"Authorization": f"Bearer {token}"})
                results.setdefault(label, []).append(res.json())
        except Exception:
            errors[label] = traceback.format_exc()

    t_a = threading.Thread(target=call, args=("a", token_a))
    t_b = threading.Thread(target=call, args=("b", token_b))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    record("Concurrency: keine Exceptions in Thread A", "a" not in errors, errors.get("a", ""))
    record("Concurrency: keine Exceptions in Thread B", "b" not in errors, errors.get("b", ""))

    if "a" in results:
        adressen_a = {im["adresse"] for r in results["a"] for im in r}
        record("Concurrency: Nutzer A sieht in JEDER der 5 Antworten NUR seine eigene Adresse "
               "(kein Context-Bleed von B)",
               adressen_a == {"Adresse-A-geheim"}, f"gesehen={adressen_a}")
    if "b" in results:
        adressen_b = {im["adresse"] for r in results["b"] for im in r}
        record("Concurrency: Nutzer B sieht in JEDER der 5 Antworten NUR seine eigene Adresse "
               "(kein Context-Bleed von A)",
               adressen_b == {"Adresse-B-geheim"}, f"gesehen={adressen_b}")


def main():
    database.init_db()
    for fn in (
        test_context_set_switch_and_no_leak,
        test_nested_context_restores_outer,
        test_exception_inside_block_still_resets,
        test_existing_endpoints_still_work,
        test_concurrent_requests_no_context_bleed,
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
