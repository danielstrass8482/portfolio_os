#!/bin/bash
# backup-portfolio-db.sh – nächtliches Backup der Produktions-DB trading_bot
# (Portfolio-OS + Trading-Bots), installiert als /usr/local/bin/backup-portfolio-db.sh,
# Cron: administrator, täglich 03:00 (>> /var/log/backup.log 2>&1).
#
# Versioniert seit 2026-10-02 (RLS-Umbau, Risiko R1 vor Chunk 7, siehe
# docs/rls-force-umbau-plan-21-08.md). Vorher lief pg_dump als Tabellenbesitzer
# trading_bot_user ohne pipefail: unter FORCE ROW LEVEL SECURITY bricht so ein
# Dump ab ("query would be affected by row-level security policy"), das Skript
# sah trotzdem Exit 0 und verschlüsselte eine abgeschnittene Attrappe. Mit
# --enable-row-security wäre er zwar fehlerfrei, aber ohne pos_*-Daten.
#
# Jetzt:
#   - pg_dump als Superuser postgres über den lokalen Socket (Peer-Auth), per
#     `sudo -n` (administrator hat NOPASSWD; -n bricht sofort ab statt auf ein
#     Passwort zu warten). Kein DB-Passwort mehr im Skript.
#   - pipefail + Plausibilitätsprüfung des Dumps (gzip intakt, Abschlusszeile
#     vorhanden, Mindestzahl Tabellen mit Daten, Pflicht-Tabellen nicht leer,
#     Mindestgröße), gpg muss erfolgreich sein.
#   - Bei jedem Fehler: Exit 1, Meldung auf stderr, Eintrag im Journal
#     (logger -p user.err) und E-Mail an ALERT_EMAIL über notifier.send_email()
#     von Portfolio-OS. Die unvollständige Datei bleibt als FAILED_… liegen und
#     zählt nicht zur Rotation (verdrängt also keine guten Backups).
#
# Alle Pfade/Schwellen per Umgebungsvariable überschreibbar (für den Test, siehe
# test_backup_script.py); ohne Variablen gelten die Produktionswerte unten.

set -uo pipefail
umask 077

DB="${BACKUP_DB:-trading_bot}"
BACKUP_DIR="${BACKUP_DIR:-/home/administrator/backups}"
PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:-/home/administrator/.backup-passphrase}"
PORTFOLIO_OS_DIR="${BACKUP_PORTFOLIO_OS_DIR:-/home/administrator/portfolio_os}"
ALERT_PYTHON="${BACKUP_ALERT_PYTHON:-$PORTFOLIO_OS_DIR/venv_notify/bin/python}"
PG_DUMP_CMD="${BACKUP_PG_DUMP_CMD:-sudo -n -u postgres pg_dump}"
KEEP="${BACKUP_KEEP:-30}"
MIN_TABLES_WITH_DATA="${BACKUP_MIN_TABLES:-40}"      # 2026-10-02: 46
MIN_BYTES="${BACKUP_MIN_BYTES:-5000000}"              # 2026-10-02: 16,6 MB unkomprimiert
REQUIRED_TABLES="${BACKUP_REQUIRED_TABLES:-pos_users pos_portfolios pos_positions pos_buchungen trades bot_config}"

DATE=$(date +%Y%m%d_%H%M%S)
NAME="portfolio_$DATE.sql.gz"
OUT="$BACKUP_DIR/$NAME"

fail() {
  local grund="$1"
  local failed="$BACKUP_DIR/FAILED_$NAME"
  [ -e "$OUT" ] && mv -f "$OUT" "$failed"
  [ -e "$OUT.gpg" ] && mv -f "$OUT.gpg" "$failed.gpg"
  echo "BACKUP FEHLGESCHLAGEN ($DATE, DB $DB): $grund" >&2
  logger -p user.err -t backup-portfolio-db "BACKUP FEHLGESCHLAGEN ($DATE, DB $DB): $grund" 2>/dev/null || true
  if [ -x "$ALERT_PYTHON" ]; then
    (cd "$PORTFOLIO_OS_DIR" && timeout 90 "$ALERT_PYTHON" - "$grund" "$DATE" "$DB" "$failed" >&2 <<'PY'
import sys
from notifier import send_email
grund, datum, db, datei = sys.argv[1:5]
send_email(
    f"⚠️ Portfolio-OS: nächtliches DB-Backup FEHLGESCHLAGEN ({datum})",
    f"Das Backup der Datenbank {db} vom {datum} ist fehlgeschlagen.\n\n"
    f"Grund: {grund}\n\n"
    f"Die unvollständige Datei liegt zur Analyse unter {datei} und zählt nicht zur Rotation.\n"
    f"Details: /var/log/backup.log und journalctl -t backup-portfolio-db.",
)
PY
    ) || echo "Zusätzlich: Alarm-Mail konnte nicht verschickt werden." >&2
  else
    echo "Zusätzlich: kein Python für den Alarm-Mailversand gefunden ($ALERT_PYTHON)." >&2
  fi
  exit 1
}

mkdir -p "$BACKUP_DIR" || fail "Backup-Verzeichnis $BACKUP_DIR nicht anlegbar"

# 1) Dump -- pipefail: ein Fehler von pg_dump schlägt bis hierher durch.
$PG_DUMP_CMD "$DB" | gzip > "$OUT"
status=("${PIPESTATUS[@]}")
[ "${status[0]}" -eq 0 ] || fail "pg_dump mit Exit ${status[0]} abgebrochen"
[ "${status[1]}" -eq 0 ] || fail "gzip mit Exit ${status[1]} abgebrochen"

# 2) Plausibilität.
gzip -t "$OUT" 2>/dev/null || fail "gzip-Archiv beschädigt"
stats=$(zcat "$OUT" | awk -v req="$REQUIRED_TABLES" '
  BEGIN { n = split(req, r, " "); for (i = 1; i <= n; i++) rows[r[i]] = 0 }
  { bytes += length($0) + 1 }
  /^-- PostgreSQL database dump complete/ { complete = 1 }
  /^COPY public\./ { split($2, a, "."); tbl = a[2]; incopy = 1; next }
  incopy && /^\\\.$/ { incopy = 0; if (cnt[tbl] > 0) withdata++; next }
  incopy { cnt[tbl]++ }
  END {
    missing = ""
    for (t in rows) if (cnt[t] < 1) missing = missing " " t
    printf "%d %d %d%s\n", bytes, complete, withdata, missing
  }')
read -r bytes complete withdata missing <<<"$stats"
[ "$complete" = "1" ] || fail "Abschlusszeile '-- PostgreSQL database dump complete' fehlt (Dump abgeschnitten)"
[ -z "${missing// /}" ] || fail "Pflicht-Tabellen ohne Datenzeilen:$missing (z.B. durch Row-Level-Security gefiltert)"
[ "$withdata" -ge "$MIN_TABLES_WITH_DATA" ] || fail "nur $withdata Tabellen mit Daten (Minimum $MIN_TABLES_WITH_DATA)"
[ "$bytes" -ge "$MIN_BYTES" ] || fail "Dump nur $bytes Bytes unkomprimiert (Minimum $MIN_BYTES)"

# 3) Verschlüsseln -- ein Fehler hier ist ebenfalls ein Fehlschlag (früher: still).
if command -v gpg >/dev/null 2>&1; then
  gpg --batch --yes --symmetric --cipher-algo AES256 \
    --passphrase-file "$PASSPHRASE_FILE" --output "$OUT.gpg" "$OUT" 2>/dev/null \
    || fail "gpg-Verschlüsselung fehlgeschlagen"
  rm -f "$OUT"
  ERGEBNIS="$NAME.gpg"
else
  ERGEBNIS="$NAME"
fi

# 4) Rotation -- nur erfolgreiche Backups (FAILED_* bleiben unberührt).
ls -t "$BACKUP_DIR"/portfolio_* 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

echo "Backup: $ERGEBNIS OK ($withdata Tabellen mit Daten, $bytes Bytes unkomprimiert)"
