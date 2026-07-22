"""
main.py – Orchestrierung von Portfolio-OS via APScheduler.

Jobs: tägliche Kursaktualisierung + Schwellwert-Check, Wochen-/Monats-/
Quartals-/Jahres-Reports. Das System beobachtet und benachrichtigt nur –
es wird NIE automatisch gehandelt (jede Rebalancing-Umsetzung erfordert
eine explizite Bestätigung über das Dashboard, siehe rebalancing.py).
"""

from datetime import date

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config import (
    validate_config, DAILY_UPDATE_HOUR, WEEKLY_SUMMARY_HOUR,
    MONTHLY_REPORT_HOUR, QUARTERLY_REPORT_HOUR, YEARLY_REPORT_HOUR,
)
from database import init_db, get_session, PosUser, save_daily_snapshot
import portfolio as portfolio_module
import notifier


def _alle_user_ids() -> list:
    with get_session() as session:
        return [u.id for u in session.query(PosUser).all()]


def daily_job():
    """Täglich 08:00: Preise aktualisieren, Schwellwerte prüfen, Snapshot speichern."""
    print(f"[{date.today()}] Daily Job: Preise aktualisieren...")
    aktualisiert = portfolio_module.update_prices()
    print(f"  {aktualisiert} Position(en) aktualisiert")

    for user_id in _alle_user_ids():
        try:
            summary = portfolio_module.get_portfolio_summary(user_id)
            with get_session() as session:
                save_daily_snapshot(session, user_id, summary["gesamtvermoegen"], summary["asset_breakdown"])
            notifier.send_daily_alert(user_id)
        except Exception as e:
            print(f"⚠️  Daily Job für Nutzer {user_id} fehlgeschlagen: {e} (degraded mode, weiter mit nächstem Nutzer)")


def weekly_job():
    """Montags 07:00: Wochen-Summary."""
    print(f"[{date.today()}] Weekly Job: Wochen-Summary...")
    for user_id in _alle_user_ids():
        try:
            notifier.send_weekly_summary(user_id)
        except Exception as e:
            print(f"⚠️  Weekly Job für Nutzer {user_id} fehlgeschlagen: {e}")


def monthly_job():
    """1. des Monats, 07:00: Monats-Report."""
    print(f"[{date.today()}] Monthly Job: Monats-Report...")
    for user_id in _alle_user_ids():
        try:
            notifier.send_monthly_report(user_id)
        except Exception as e:
            print(f"⚠️  Monthly Job für Nutzer {user_id} fehlgeschlagen: {e}")


def quarterly_job():
    """Quartalsweise (Jan/Apr/Jul/Okt, 1.), 07:00: Quartals-Report + Rebalancing-Vorschlag."""
    print(f"[{date.today()}] Quarterly Job: Quartals-Report + Rebalancing-Vorschlag...")
    for user_id in _alle_user_ids():
        try:
            # send_quarterly_report() erstellt intern bereits einen Rebalancing-Vorschlag
            # (typ="quartal") und verschickt ihn zusammen mit dem KI-Bericht in einer Mail.
            notifier.send_quarterly_report(user_id)
        except Exception as e:
            print(f"⚠️  Quarterly Job für Nutzer {user_id} fehlgeschlagen: {e}")


def yearly_job():
    """Jährlich am 1. Januar: Jahres-Report (Steuerübersicht des abgelaufenen Jahres)."""
    print(f"[{date.today()}] Yearly Job: Jahres-Report...")
    for user_id in _alle_user_ids():
        try:
            notifier.send_yearly_report(user_id)
        except Exception as e:
            print(f"⚠️  Yearly Job für Nutzer {user_id} fehlgeschlagen: {e}")


def main():
    init_db()
    for warnung in validate_config():
        print(f"⚠️  {warnung}")

    tz = pytz.timezone("Europe/Berlin")
    scheduler = BlockingScheduler(timezone=tz)

    scheduler.add_job(daily_job, CronTrigger(hour=DAILY_UPDATE_HOUR, minute=0, timezone=tz), id="daily_job")
    scheduler.add_job(weekly_job, CronTrigger(day_of_week="mon", hour=WEEKLY_SUMMARY_HOUR, minute=0, timezone=tz), id="weekly_job")
    scheduler.add_job(monthly_job, CronTrigger(day=1, hour=MONTHLY_REPORT_HOUR, minute=0, timezone=tz), id="monthly_job")
    scheduler.add_job(
        quarterly_job,
        CronTrigger(month="1,4,7,10", day=1, hour=QUARTERLY_REPORT_HOUR, minute=0, timezone=tz),
        id="quarterly_job",
    )
    scheduler.add_job(yearly_job, CronTrigger(month=1, day=1, hour=YEARLY_REPORT_HOUR, minute=0, timezone=tz), id="yearly_job")

    print("✅ Portfolio-OS Scheduler gestartet (Europe/Berlin).")
    scheduler.start()


if __name__ == "__main__":
    main()
