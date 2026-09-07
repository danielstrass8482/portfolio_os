-- RLS-Umbau, Vorbereitung Chunk 5 (siehe docs/rls-force-umbau-plan-21-08.md,
-- Sonderfall c / Restrisiko aus dem Chunk-2-Nachzug-Commit 8e1d89c).
--
-- EINMALIGER MANUELLER SCHRITT für einen Postgres-Superuser
-- (sudo -u postgres psql), NICHT Teil der App-Migration (database.py::
-- _migrate_owner_lookup_functions läuft automatisch bei jedem Start, kann
-- aber keine BYPASSRLS-Rolle anlegen -- das braucht Superuser-Rechte, die
-- die App-Rolle trading_bot_user nicht hat).
--
-- Voraussetzung: die App wurde mindestens einmal gestartet (init_db() hat
-- pos_position_owner_id()/pos_portfolio_owner_id()/pos_transaction_owner_id()/
-- pos_real_estate_owner_id() bereits angelegt) -- ALTER FUNCTION unten
-- schlägt sonst fehl, weil die Funktionen noch nicht existieren.
--
-- Wirkung: erst NACH diesem Schritt bypassen die 4 Owner-Lookup-Funktionen
-- tatsächlich FORCE ROW LEVEL SECURITY (SECURITY DEFINER allein reicht dafür
-- nicht, siehe Kommentar in database.py::_migrate_owner_lookup_functions).
-- Ohne diesen Schritt vor Chunk 7 (FORCE ROW LEVEL SECURITY scharf schalten)
-- würden alle 12 betroffenen Admin-Cross-View-Endpoints für Admins 404
-- werfen, sobald sie eine fremde Ressource referenzieren.
--
-- Auf Produktion NICHT automatisch ausgeführt -- bewusst manuell, siehe
-- Bericht zu dieser Aufgabe (kein Live-DB-Schreibzugriff ohne Rückfrage).

-- App-DB-Rolle (Produktion: trading_bot_user, siehe .env / database.py-
-- Kommentar) fest eingetragen -- die Datei ist damit ohne manuelles
-- Suchen/Ersetzen direkt lauffähig. Bei einer künftigen Rollen-Umbenennung
-- (oder falls dieses Skript für eine andere Umgebung mit anderer App-Rolle
-- wiederverwendet wird) müssten alle trading_bot_user-Vorkommen unten
-- entsprechend angepasst werden.

-- Idempotenz (2026-09-08 ergänzt): CREATE ROLE kennt kein IF NOT EXISTS,
-- schlägt bei wiederholter Ausführung sonst mit "role already exists" fehl
-- (Standard-Postgres-Workaround: DO-Block mit pg_roles-Check). Die restlichen
-- Statements unten (GRANT Rollenmitgliedschaft, ALTER FUNCTION OWNER TO,
-- GRANT SELECT/EXECUTE) sind bereits von Natur aus idempotent -- erneutes
-- Setzen desselben Owners/derselben Grants wirft in Postgres keinen Fehler.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pos_owner_lookup_bypass') THEN
        CREATE ROLE pos_owner_lookup_bypass NOLOGIN BYPASSRLS;
    END IF;
END
$$;

GRANT pos_owner_lookup_bypass TO trading_bot_user;

ALTER FUNCTION pos_position_owner_id(integer)    OWNER TO pos_owner_lookup_bypass;
ALTER FUNCTION pos_portfolio_owner_id(integer)   OWNER TO pos_owner_lookup_bypass;
ALTER FUNCTION pos_transaction_owner_id(integer) OWNER TO pos_owner_lookup_bypass;
ALTER FUNCTION pos_real_estate_owner_id(integer) OWNER TO pos_owner_lookup_bypass;

-- WICHTIG, per lokalem Test entdeckt (2026-09-07): BYPASSRLS überspringt NUR
-- die Row-Security-Ebene, nicht die normale objektbezogene GRANT-Prüfung.
-- Ohne diese vier GRANTs schlägt jeder Aufruf mit "permission denied for
-- table ..." fehl, weil pos_owner_lookup_bypass sonst gar kein SELECT auf
-- den Tabellen hat, die die Funktionskörper lesen (siehe database.py::
-- _migrate_owner_lookup_functions für die genauen Tabellen je Funktion).
GRANT SELECT ON pos_portfolios, pos_positions, pos_transactions, pos_real_estate
    TO pos_owner_lookup_bypass;

-- Nach der Ownership-Umschaltung erbt trading_bot_user EXECUTE nicht mehr
-- implizit über den alten Owner-Status -- erneut explizit vergeben (die
-- REVOKE...FROM PUBLIC-Zeilen in _migrate_owner_lookup_functions bleiben
-- unabhängig davon in Kraft).
GRANT EXECUTE ON FUNCTION pos_position_owner_id(integer)    TO trading_bot_user;
GRANT EXECUTE ON FUNCTION pos_portfolio_owner_id(integer)   TO trading_bot_user;
GRANT EXECUTE ON FUNCTION pos_transaction_owner_id(integer) TO trading_bot_user;
GRANT EXECUTE ON FUNCTION pos_real_estate_owner_id(integer) TO trading_bot_user;

-- Verifikation (als trading_bot_user oder via \df+ als Superuser):
--   SELECT proname, proowner::regrole, prosecdef
--   FROM pg_proc WHERE proname LIKE 'pos_%_owner_id';
-- proowner muss pos_owner_lookup_bypass zeigen, prosecdef = true.
