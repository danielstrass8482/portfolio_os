-- RLS-Umbau Chunk 5 (siehe docs/rls-force-umbau-plan-21-08.md, Abschnitte 4/5/6):
-- ALLE Row-Level-Security-Policies von Portfolio-OS an einer Stelle, versioniert.
--
-- Umfang (Plan-Dokument, Einleitung): 14 Tabellen.
--   - 7 Tabellen mit bereits bestehender user_isolation-Policy (am 2026-07-24
--     von Hand auf Produktion angelegt, bis hierhin nirgends im Repo) --
--     hier mit IDENTISCHER Definition übernommen;
--   - pos_transactions (RLS war schon an, aber ohne Policy -- unter FORCE wäre
--     die Tabelle komplett gesperrt), §4;
--   - 5 bisher ungeschützte Tabellen, §6;
--   - pos_admin_access_log, §5 Variante C (Entscheidung 2026-10-01): NUR eine
--     INSERT-Policy, keine SELECT/UPDATE/DELETE-Policy -- die App kann das
--     Audit-Log unter FORCE nur noch schreiben, nicht lesen/ändern/löschen;
--     lesen nur noch als Superuser.
-- Bewusst außerhalb: pos_users, pos_asset_classes, pos_family_goals (keine
-- Nutzerspalte bzw. global) und alle Trading-Bot-Tabellen.
--
-- Ausführung: als Postgres-Superuser (oder Tabellenbesitzer trading_bot_user):
--   sudo -u postgres psql -d trading_bot -v ON_ERROR_STOP=1 -f docs/rls-policies.sql
-- Wiederholbar (DROP POLICY IF EXISTS + CREATE, ENABLE ist idempotent) und
-- atomar (eine Transaktion). Setzt KEIN FORCE ROW LEVEL SECURITY (Chunk 7) --
-- ohne FORCE gelten Policies nicht für den Tabellenbesitzer trading_bot_user,
-- über den die App zugreift; dieses Skript ändert am Verhalten der App also
-- nichts. pos_owner_lookup_bypass (einzige weitere Rolle mit Rechten auf
-- pos_*) hat BYPASSRLS.
--
-- Muster wie die bestehenden Policies: cmd ALL, TO public, nur USING -- ohne
-- WITH CHECK verwendet Postgres den USING-Ausdruck auch für INSERT/UPDATE,
-- d.h. niemand kann Zeilen für fremde Nutzer anlegen oder umhängen.

BEGIN;

-- ── 7 bestehende Policies (unverändert übernommen) ───────────────────────
ALTER TABLE pos_portfolios ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_portfolios;
CREATE POLICY user_isolation ON pos_portfolios
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_positions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_positions;
CREATE POLICY user_isolation ON pos_positions
  USING (portfolio_id IN (
    SELECT pos_portfolios.id FROM pos_portfolios
    WHERE pos_portfolios.user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
  ));

ALTER TABLE pos_real_estate ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_real_estate;
CREATE POLICY user_isolation ON pos_real_estate
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_goals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_goals;
CREATE POLICY user_isolation ON pos_goals
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_target_weights ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_target_weights;
CREATE POLICY user_isolation ON pos_target_weights
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_tax_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_tax_config;
CREATE POLICY user_isolation ON pos_tax_config
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_buchungen ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_buchungen;
CREATE POLICY user_isolation ON pos_buchungen
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

-- ── §4: pos_transactions (über portfolio_id, Muster wie pos_positions) ────
ALTER TABLE pos_transactions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_transactions;
CREATE POLICY user_isolation ON pos_transactions
  USING (portfolio_id IN (
    SELECT pos_portfolios.id FROM pos_portfolios
    WHERE pos_portfolios.user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer
  ));

-- ── §6: 5 bisher ungeschützte Tabellen mit direktem user_id ──────────────
ALTER TABLE pos_daily_snapshots ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_daily_snapshots;
CREATE POLICY user_isolation ON pos_daily_snapshots
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_investment_preferences ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_investment_preferences;
CREATE POLICY user_isolation ON pos_investment_preferences
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_kategorisierungsregeln ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_kategorisierungsregeln;
CREATE POLICY user_isolation ON pos_kategorisierungsregeln
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_rebalancing_proposals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_rebalancing_proposals;
CREATE POLICY user_isolation ON pos_rebalancing_proposals
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

ALTER TABLE pos_tax_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS user_isolation ON pos_tax_events;
CREATE POLICY user_isolation ON pos_tax_events
  USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

-- ── §5 Variante C: pos_admin_access_log nur INSERT ───────────────────────
-- Der eintragende Admin muss der aktuelle Kontext sein (log_admin_access()
-- wird in api.py überall aufgerufen, BEVOR der Kontext auf das Ziel umschaltet).
-- Keine SELECT/UPDATE/DELETE-Policy: unter FORCE sieht die App 0 Zeilen und
-- kann nichts ändern oder löschen. Die Namen der verworfenen Varianten A/B
-- werden mit entfernt, falls sie je von Hand angelegt wurden.
ALTER TABLE pos_admin_access_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS admin_sees_own_accesses ON pos_admin_access_log;
DROP POLICY IF EXISTS admin_writes_own_accesses ON pos_admin_access_log;
DROP POLICY IF EXISTS admin_or_target_sees_entry ON pos_admin_access_log;
DROP POLICY IF EXISTS admin_insert_only ON pos_admin_access_log;
CREATE POLICY admin_insert_only ON pos_admin_access_log
  FOR INSERT
  WITH CHECK (admin_user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

COMMIT;

-- Verifikation (als Superuser):
--   SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies
--   WHERE schemaname = 'public' ORDER BY tablename;
-- erwartet: 14 Policies (13x user_isolation cmd=ALL, 1x admin_insert_only
-- cmd=INSERT auf pos_admin_access_log); 14 Tabellen mit relrowsecurity=true,
-- relforcerowsecurity weiterhin false (FORCE ist Chunk 7).
