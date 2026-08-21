"""
run_scheduled_job.py – Einzelner Cron-Einstiegspunkt für die main.py-Jobs
(Smart Notifications), gedacht für systemd-Timer statt der dauerhaft
laufenden BlockingScheduler-Variante in main.main(). Jeder Timer ruft dieses
Skript mit genau einem Job-Namen auf, das Skript führt den Job aus und
beendet sich (Type=oneshot).

Zeitpläne siehe main.py (DAILY_UPDATE_HOUR etc. aus config.py) bzw. die
systemd .timer-Units unter systemd/notify-*.timer.
"""

import sys

from database import init_db
import main as jobs

JOBS = {
    "daily": jobs.daily_job,
    "weekly": jobs.weekly_job,
    "monthly": jobs.monthly_job,
    "quarterly": jobs.quarterly_job,
    "yearly": jobs.yearly_job,
}


def run(name: str) -> None:
    if name not in JOBS:
        raise SystemExit(f"Unbekannter Job: {name!r} (erwartet: {', '.join(JOBS)})")
    init_db()
    JOBS[name]()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: python run_scheduled_job.py <{'|'.join(JOBS)}>")
    run(sys.argv[1])
