"""
test_backup_script.py – Test für ops/backup-portfolio-db.sh (RLS-Umbau, Risiko R1
vor Chunk 7, 2026-10-02).

Wegwerf-Postgres mit den Policies aus docs/rls-policies.sql und FORCE ROW LEVEL
SECURITY auf allen 14 Tabellen (Zielzustand nach Chunk 7), dazu kleine
Stellvertreter der Bot-Tabellen trades/bot_config. Geprüft:
  1. ALTES Skript (bis 2026-10-02 auf Produktion, wörtlich nachgebaut; nur DB,
     Passwort und Pfade lokal): Exit 0, obwohl pg_dump abbricht -- es entsteht
     eine verschlüsselte Attrappe ohne Abschlusszeile (der Fehler aus der Diagnose).
  2. NEUES Skript, Standard (pg_dump als postgres per sudo -n): Exit 0,
     vollständiger Dump inkl. pos_*-Daten, nur die .gpg-Datei bleibt liegen.
  3. NEUES Skript mit pg_dump als Tabellenbesitzer (der alte Weg): sichtbarer
     Fehlschlag -- Exit 1, Meldung auf stderr, Alarm-Mail, FAILED_-Datei, kein
     neues "gutes" Backup.
  4. NEUES Skript mit --enable-row-security als Besitzer (Dump ohne Fehler,
     aber pos_*-Tabellen leer): Fehlschlag über die Pflicht-Tabellen-Prüfung.
  5. gpg schlägt fehl (Passphrase-Datei fehlt): Fehlschlag statt stiller
     unverschlüsselter Datei.
  6. Produktions-Schwellen (40 Tabellen / 5 MB) gegen die kleine Test-DB:
     Fehlschlag -- die Schwellen sind aktiv.
  7. Rotation: behält 30 erfolgreiche Backups, FAILED_-Dateien bleiben unberührt.

Setup:
    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER bkp_owner WITH PASSWORD 'bkp_owner_pw';"
    sudo -u postgres psql -c "CREATE DATABASE portfolio_os_bkp OWNER bkp_owner;"
    python3 test_backup_script.py
    sudo -u postgres psql -c "DROP DATABASE portfolio_os_bkp;"
    sudo -u postgres psql -c "DROP OWNED BY bkp_owner; DROP ROLE bkp_owner;"
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

DB = "portfolio_os_bkp"
OWNER, OWNER_PW = "bkp_owner", "bkp_owner_pw"
os.environ.setdefault("DATABASE_URL", f"postgresql://{OWNER}:{OWNER_PW}@localhost:5432/{DB}")
HERE = os.path.dirname(os.path.abspath(__file__))
NEW_SCRIPT = os.path.join(HERE, "ops", "backup-portfolio-db.sh")
POLICY_FILE = os.path.join(HERE, "docs", "rls-policies.sql")
RESULTS = []
WORKSPACES = []  # am Ende gelöscht (enthalten Test-Dumps)

# Wörtlich das Skript, das bis 2026-10-02 als /usr/local/bin/backup-portfolio-db.sh
# lief -- nur Passwort, Rolle, DB und Pfade durch lokale Platzhalter ersetzt.
OLD_SCRIPT = r"""#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="__DIR__"
mkdir -p $BACKUP_DIR
PGPASSWORD=__PW__ pg_dump -U __OWNER__ __DB__ -h localhost | \
  gzip > "$BACKUP_DIR/portfolio_$DATE.sql.gz"
# Verschlüsseln mit gpg falls Key vorhanden
if command -v gpg &> /dev/null; then
  gpg --batch --symmetric --cipher-algo AES256 \
    --passphrase-file __PASS__ \
    "$BACKUP_DIR/portfolio_$DATE.sql.gz" 2>/dev/null && \
    rm "$BACKUP_DIR/portfolio_$DATE.sql.gz"
fi
# Nur letzte 30 Backups behalten
ls -t $BACKUP_DIR/portfolio_* | tail -n +31 | xargs rm -f 2>/dev/null
echo "Backup: portfolio_$DATE"
"""


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + str(detail)) if detail else ''}")


def su(sql):
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", DB, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def setup_db():
    import database
    database.init_db()
    with open(POLICY_FILE) as f:
        subprocess.run(["sudo", "-u", "postgres", "psql", "-d", DB, "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                       stdin=f, check=True, capture_output=True)
    su(f"SET ROLE {OWNER}; "
       "CREATE TABLE IF NOT EXISTS trades (id serial primary key, symbol text, user_id int); "
       "CREATE TABLE IF NOT EXISTS bot_config (key text primary key, value text);")
    if su("SELECT count(*) FROM pos_users") == "0":
        su("INSERT INTO pos_users(name,email,rolle,status) VALUES ('bkp','bkp@example.com','admin','active')")
        uid = su("SELECT min(id) FROM pos_users")
        su(f"INSERT INTO pos_portfolios(user_id,name,typ) VALUES ({uid},'pf','depot')")
        pf = su("SELECT min(id) FROM pos_portfolios")
        su(f"INSERT INTO pos_positions(portfolio_id,ticker) VALUES ({pf},'T')")
        su(f"INSERT INTO pos_buchungen(user_id,datum,betrag) VALUES ({uid},current_date,1)")
        su("INSERT INTO trades(symbol,user_id) VALUES ('AAPL',1)")
        su("INSERT INTO bot_config(key,value) VALUES ('MAX_CAPITAL_TOTAL','475')")
    tables = [l.split()[2] for l in open(POLICY_FILE) if l.startswith("ALTER TABLE") and "ENABLE ROW LEVEL" in l]
    for t in tables:
        su(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    return tables


def workspace():
    d = tempfile.mkdtemp(prefix="bkptest_")
    WORKSPACES.append(d)
    os.chmod(d, 0o755)
    passfile = os.path.join(d, "passphrase")
    with open(passfile, "w") as f:
        f.write("test-passphrase\n")
    os.chmod(passfile, 0o600)
    # Stub-"Portfolio-OS" für den Alarm: notifier.send_email schreibt in eine Datei.
    pos_dir = os.path.join(d, "pos")
    os.makedirs(pos_dir)
    mails = os.path.join(d, "mails.txt")
    with open(os.path.join(pos_dir, "notifier.py"), "w") as f:
        f.write(f"def send_email(subject, body, to_email=None):\n"
                f"    open({mails!r}, 'a').write(subject + '\\n' + body + '\\n---\\n')\n")
    backups = os.path.join(d, "backups")
    os.makedirs(backups)
    os.chmod(backups, 0o755)
    return {"dir": d, "pass": passfile, "pos": pos_dir, "mails": mails, "backups": backups}


# Produktions-Standard des Skripts ist `sudo -n -u postgres pg_dump` (VPS:
# administrator hat NOPASSWD: ALL). Wo sudo ohne Passwort nur für root gilt
# (z.B. Cloud Shell), testen wir den gleichwertigen Superuser-Dump per
# runuser -- der wörtliche Produktionsbefehl wird beim Deploy auf dem VPS geprüft.
SUDO_POSTGRES_OK = subprocess.run(["sudo", "-n", "-u", "postgres", "true"], capture_output=True).returncode == 0
SUPERUSER_DUMP = None if SUDO_POSTGRES_OK else "sudo -n runuser -u postgres -- pg_dump"


def run_new(ws, **overrides):
    if SUPERUSER_DUMP and "BACKUP_PG_DUMP_CMD" not in overrides:
        overrides["BACKUP_PG_DUMP_CMD"] = SUPERUSER_DUMP
    env = dict(os.environ, BACKUP_DB=DB, BACKUP_DIR=ws["backups"], BACKUP_PASSPHRASE_FILE=ws["pass"],
               BACKUP_PORTFOLIO_OS_DIR=ws["pos"], BACKUP_ALERT_PYTHON=sys.executable,
               BACKUP_MIN_TABLES="5", BACKUP_MIN_BYTES="1000")
    env.update({k: v for k, v in overrides.items()})
    return subprocess.run(["bash", NEW_SCRIPT], env=env, capture_output=True, text=True)


def decrypt(ws, path):
    r = subprocess.run(f"gpg --batch --quiet --passphrase-file {ws['pass']} -d {path} | zcat", shell=True,
                       capture_output=True, text=True)
    return r.stdout


def copy_rows(dump, table):
    rows, inblock = 0, False
    for line in dump.splitlines():
        if line.startswith(f"COPY public.{table} "):
            inblock = True
            continue
        if inblock:
            if line == "\\.":
                return rows
            rows += 1
    return rows


def mails(ws):
    return open(ws["mails"]).read() if os.path.exists(ws["mails"]) else ""


def main():
    tables = setup_db()
    print(f"FORCE RLS auf {len(tables)} Tabellen aus docs/rls-policies.sql (nur diese Wegwerf-DB)")
    print(f"Superuser-Dump: {'sudo -n -u postgres pg_dump (Produktions-Standard)' if SUDO_POSTGRES_OK else SUPERUSER_DUMP}\n")
    owner_dump = f"env PGPASSWORD={OWNER_PW} pg_dump -U {OWNER} -h localhost"

    # 1) altes Skript
    ws = workspace()
    old = OLD_SCRIPT.replace("__DIR__", ws["backups"]).replace("__PW__", OWNER_PW).replace("__OWNER__", OWNER) \
                    .replace("__DB__", DB).replace("__PASS__", ws["pass"])
    path = os.path.join(ws["dir"], "old.sh")
    open(path, "w").write(old)
    r = subprocess.run(["bash", path], capture_output=True, text=True)
    files = glob.glob(os.path.join(ws["backups"], "portfolio_*"))
    dump = decrypt(ws, files[0]) if files else ""
    record("ALT: Exit 0 trotz abgebrochenem pg_dump (stiller Fehler reproduziert)",
           r.returncode == 0 and "row-level security" in r.stderr, f"exit={r.returncode}, stderr={r.stderr.strip()[:90]}")
    record("ALT: verschlüsselte Attrappe -- ohne Abschlusszeile, ohne pos_*-Daten",
           len(files) == 1 and "dump complete" not in dump and copy_rows(dump, "pos_portfolios") == 0,
           f"datei={os.path.basename(files[0]) if files else None}, {os.path.getsize(files[0]) if files else 0} Bytes, "
           f"meldet: {r.stdout.strip()}")

    # 2) neues Skript, Standard (postgres)
    ws = workspace()
    r = run_new(ws)
    files = sorted(os.listdir(ws["backups"]))
    dump = decrypt(ws, os.path.join(ws["backups"], files[0])) if files else ""
    record("NEU (postgres): Exit 0", r.returncode == 0, (r.stdout + r.stderr).strip()[:160])
    record("NEU (postgres): genau eine .gpg-Datei, keine unverschlüsselte .gz, keine FAILED_",
           len(files) == 1 and files[0].endswith(".sql.gz.gpg") and files[0].startswith("portfolio_"), files)
    record("NEU (postgres): Dump vollständig -- Abschlusszeile + pos_*-Daten trotz FORCE",
           "dump complete" in dump and all(copy_rows(dump, t) >= 1 for t in
                                           ("pos_users", "pos_portfolios", "pos_positions", "pos_buchungen")),
           {t: copy_rows(dump, t) for t in ("pos_users", "pos_portfolios", "pos_positions", "pos_buchungen", "trades")})
    record("NEU (postgres): keine Alarm-Mail bei Erfolg", mails(ws) == "")

    # 3) neues Skript, Dump als Besitzer -> muss sichtbar scheitern
    ws = workspace()
    r = run_new(ws, BACKUP_PG_DUMP_CMD=owner_dump)
    files = sorted(os.listdir(ws["backups"]))
    record("NEU (Besitzer): Exit 1 mit sichtbarer Meldung", r.returncode == 1 and "BACKUP FEHLGESCHLAGEN" in r.stderr,
           r.stderr.strip().splitlines()[-2:] if r.stderr else "")
    record("NEU (Besitzer): Alarm-Mail verschickt", "FEHLGESCHLAGEN" in mails(ws) and "pg_dump" in mails(ws),
           mails(ws).splitlines()[:1])
    record("NEU (Besitzer): nur FAILED_-Datei, kein 'gutes' Backup",
           files and all(f.startswith("FAILED_") for f in files), files)

    # 4) Besitzer + --enable-row-security: pg_dump ok, aber pos_* leer
    ws = workspace()
    r = run_new(ws, BACKUP_PG_DUMP_CMD=owner_dump + " --enable-row-security")
    record("NEU (Besitzer, --enable-row-security): Fehlschlag über Pflicht-Tabellen-Prüfung",
           r.returncode == 1 and "Pflicht-Tabellen ohne Datenzeilen" in r.stderr and "pos_portfolios" in r.stderr
           and "pos_positions" in r.stderr and "pos_buchungen" in r.stderr,  # pos_users hat kein RLS -> voll
           r.stderr.strip().splitlines()[0] if r.stderr else "")

    # 5) gpg schlägt fehl
    ws = workspace()
    r = run_new(ws, BACKUP_PASSPHRASE_FILE=os.path.join(ws["dir"], "gibt-es-nicht"))
    files = sorted(os.listdir(ws["backups"]))
    record("NEU (gpg-Fehler): Exit 1, keine unverschlüsselte Datei unter normalem Namen",
           r.returncode == 1 and "gpg" in r.stderr and all(f.startswith("FAILED_") for f in files), files)

    # 6) Produktions-Schwellen auf der kleinen Test-DB
    ws = workspace()
    r = run_new(ws, BACKUP_MIN_TABLES="40", BACKUP_MIN_BYTES="5000000")
    record("NEU (Prod-Schwellen 40 Tabellen / 5 MB): kleine Test-DB wird abgelehnt",
           r.returncode == 1 and "Tabellen mit Daten" in r.stderr, r.stderr.strip().splitlines()[0] if r.stderr else "")

    # 7) Rotation
    ws = workspace()
    for i in range(32):
        p = os.path.join(ws["backups"], f"portfolio_20200101_0000{i:02d}.sql.gz.gpg")
        open(p, "w").write("x")
        os.utime(p, (1_600_000_000 + i, 1_600_000_000 + i))
    open(os.path.join(ws["backups"], "FAILED_portfolio_20200101_000000.sql.gz"), "w").write("x")
    r = run_new(ws)
    files = os.listdir(ws["backups"])
    ok_files = [f for f in files if f.startswith("portfolio_")]
    record("Rotation: genau 30 erfolgreiche Backups behalten, das neueste ist dabei, FAILED_ unberührt",
           r.returncode == 0 and len(ok_files) == 30 and any(not f.startswith("portfolio_2020") for f in ok_files)
           and "FAILED_portfolio_20200101_000000.sql.gz" in files, f"portfolio_*={len(ok_files)}, alle={len(files)}")

    for t in tables:
        su(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
    for d in WORKSPACES:
        shutil.rmtree(d, ignore_errors=True)

    print("\n=== ZUSAMMENFASSUNG ===")
    fehl = [r for r in RESULTS if not r[1]]
    for name, ok, _ in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(fehl)}/{len(RESULTS)} Checks bestanden.")
    sys.exit(1 if fehl else 0)


if __name__ == "__main__":
    main()
