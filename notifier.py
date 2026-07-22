"""
notifier.py – E-Mail-Benachrichtigungen für Portfolio-OS.
Gleiche SMTP-Logik wie der Trading Bot (smtplib, Port 465 primär, Fallback-Port
bei blockiertem 465). Fällt SMTP aus, wird nur geloggt – nie ein Absturz.
"""

import smtplib
from email.mime.text import MIMEText
from datetime import date

from config import (
    ALERT_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_FALLBACK_PORT,
    SMTP_USER, SMTP_PASSWORD, SMTP_TIMEOUT, BASE_URL,
)


def send_email(subject: str, body: str, to_email: str = None):
    """
    Verschickt eine E-Mail via smtplib (Standardbibliothek, kein externes Package).
    Ohne ALERT_EMAIL/SMTP-Zugangsdaten wird nur in die Logs geschrieben.
    """
    empfaenger = to_email or ALERT_EMAIL
    if not empfaenger or not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print(f"📧 [E-Mail nicht konfiguriert – nur Log] {subject}\n{body}")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = empfaenger

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [empfaenger], msg.as_string())
        print(f"📧 E-Mail versendet: {subject} (Port {SMTP_PORT})")
    except (TimeoutError, OSError) as e:
        print(f"⚠️  SMTP Port {SMTP_PORT} nicht erreichbar ({e}) – Fallback auf Port {SMTP_FALLBACK_PORT}")
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_FALLBACK_PORT, timeout=SMTP_TIMEOUT) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [empfaenger], msg.as_string())
            print(f"📧 E-Mail versendet: {subject} (Port {SMTP_FALLBACK_PORT})")
        except Exception as fallback_e:
            print(f"⚠️  E-Mail-Versand fehlgeschlagen (Fallback Port {SMTP_FALLBACK_PORT}): {fallback_e}")
    except Exception as e:
        print(f"⚠️  E-Mail-Versand fehlgeschlagen: {e}")


# ─────────────────────────────────────────────
# TAGES-ALERT
# ─────────────────────────────────────────────

def send_daily_alert(user_id: int):
    """Sofort-Mail, wenn ein Schwellwert (ALERT oder SELL) überschritten wurde."""
    import rebalancing

    try:
        check = rebalancing.check_thresholds(user_id)
    except Exception as e:
        print(f"⚠️  send_daily_alert: Schwellwert-Prüfung fehlgeschlagen: {e}")
        return

    relevante = check["alert"] + check["sell_empfehlung"]
    if not relevante:
        return  # nichts zu melden

    zeilen = [f"⚠️  Portfolio-Alert: {len(relevante)} Assetklasse(n) außerhalb der Toleranz\n"]
    for d in relevante:
        zeilen.append(
            f"- {d['asset_class']}: Ist {d['ist_pct']*100:.1f}% / Ziel {d['ziel_pct']*100:.1f}% "
            f"(Abweichung {d['abweichung_pct']*100:+.1f} Punkte, Status: {d['status']})"
        )
    zeilen.append(f"\nDashboard: {BASE_URL}")
    send_email(f"Portfolio-Alert – {len(relevante)} Abweichung(en)", "\n".join(zeilen))


# ─────────────────────────────────────────────
# WOCHEN-SUMMARY
# ─────────────────────────────────────────────

def send_weekly_summary(user_id: int):
    """Montags: Performance der letzten Woche + offene Rebalancing-Vorschläge."""
    import portfolio as portfolio_module
    import rebalancing

    try:
        summary = portfolio_module.get_portfolio_summary(user_id)
        performance = portfolio_module.get_performance(user_id, "1W")
        offene = [p for p in rebalancing.get_rebalancing_history(user_id) if p["status"] == "pending"]
    except Exception as e:
        print(f"⚠️  send_weekly_summary fehlgeschlagen: {e}")
        return

    body = f"""Wochen-Summary

Gesamtvermögen: {summary['gesamtvermoegen']:.2f} EUR
Performance (7 Tage): {performance['pnl_pct']:+.2f}% ({performance['pnl_abs']:+.2f} EUR)
Offene Rebalancing-Vorschläge: {len(offene)}

Dashboard: {BASE_URL}"""
    send_email("Portfolio Wochen-Summary", body)


# ─────────────────────────────────────────────
# MONATS-REPORT
# ─────────────────────────────────────────────

def send_monthly_report(user_id: int, sparrate_betrag: float = None):
    """1. des Monats: Performance, Sparraten-Empfehlung, Zielfortschritt."""
    import portfolio as portfolio_module
    import rebalancing
    from database import get_session, PosFamilyGoal

    try:
        summary = portfolio_module.get_portfolio_summary(user_id)
        performance = portfolio_module.get_performance(user_id, "1M")
        sparrate_zeilen = []
        if sparrate_betrag:
            empfehlung = rebalancing.get_sparrate_empfehlung(user_id, sparrate_betrag)
            sparrate_zeilen = [f"- {e['asset_class']}: {e['betrag']:.2f} EUR" for e in empfehlung]

        with get_session() as session:
            ziele = session.query(PosFamilyGoal).all()
            ziel_zeilen = [
                f"- {z.name}: {z.fortschritt_pct:.0f}% ({z.aktuell_betrag:.0f}/{z.ziel_betrag:.0f} EUR)"
                for z in ziele
            ]
    except Exception as e:
        print(f"⚠️  send_monthly_report fehlgeschlagen: {e}")
        return

    body = f"""Monats-Report

Gesamtvermögen: {summary['gesamtvermoegen']:.2f} EUR
Performance (30 Tage): {performance['pnl_pct']:+.2f}%

Sparraten-Empfehlung:
{chr(10).join(sparrate_zeilen) if sparrate_zeilen else '(kein Sparraten-Betrag übergeben)'}

Zielfortschritt:
{chr(10).join(ziel_zeilen) if ziel_zeilen else '(keine Familienziele hinterlegt)'}

Dashboard: {BASE_URL}"""
    send_email("Portfolio Monats-Report", body)


# ─────────────────────────────────────────────
# QUARTALS-REPORT
# ─────────────────────────────────────────────

def send_quarterly_report(user_id: int):
    """Quartalsweise: vollständiger Bericht inkl. Rebalancing-Vorschlag und KI-Analyse."""
    import llm_analyst
    import rebalancing

    try:
        report = llm_analyst.generate_quarterly_report(user_id)
        proposal = rebalancing.create_rebalancing_proposal(user_id, "quartal")
    except Exception as e:
        print(f"⚠️  send_quarterly_report fehlgeschlagen: {e}")
        return

    body = f"""Quartals-Report

{report['text']}

── Rebalancing-Vorschlag #{proposal['id']} ──
{proposal['begruendung']}

Bestätigen/Ablehnen im Dashboard: {BASE_URL}/?proposal={proposal['id']}"""
    send_email("Portfolio Quartals-Report", body)


# ─────────────────────────────────────────────
# JAHRES-REPORT
# ─────────────────────────────────────────────

def send_yearly_report(user_id: int, jahr: int = None):
    """1. Januar: Steuerübersicht des abgelaufenen Jahres."""
    import tax_engine

    jahr = jahr or (date.today().year - 1)
    try:
        uebersicht = tax_engine.generate_jahresuebersicht(user_id, jahr)
    except Exception as e:
        print(f"⚠️  send_yearly_report fehlgeschlagen: {e}")
        return

    body = f"""Jahresübersicht {jahr}

Realisierte Gewinne: {uebersicht['realisierte_gewinne']:.2f} EUR
Realisierte Verluste: {uebersicht['realisierte_verluste']:.2f} EUR
Netto-Ergebnis: {uebersicht['netto_ergebnis']:.2f} EUR
Gezahlte Steuer: {uebersicht['steuer_gezahlt']:.2f} EUR

Freistellungsauftrag: {uebersicht['freistellungsauftrag']:.2f} EUR (aktuell genutzt: {uebersicht['freistellung_genutzt_aktuell']:.2f} EUR)
Verlusttopf (aktueller Stand): {uebersicht['verlusttopf_aktuell']:.2f} EUR

Hinweis: Näherungswerte für die eigene Planung, keine Steuerberatung.
Details im Dashboard (Tab Steuer): {BASE_URL}"""
    send_email(f"Portfolio Jahresübersicht {jahr}", body)


# ─────────────────────────────────────────────
# REBALANCING-VORSCHLAG
# ─────────────────────────────────────────────

def send_rebalancing_proposal(proposal: dict):
    """Sendet einen einzelnen Rebalancing-Vorschlag mit Bestätigungs-Link."""
    confirm_link = f"{BASE_URL}/?confirm_proposal={proposal['id']}"
    body = f"""Neuer Rebalancing-Vorschlag #{proposal['id']} ({proposal.get('typ', 'unbekannt')})

{proposal['begruendung']}

{('KI-Einordnung: ' + proposal['ki_analyse']) if proposal.get('ki_analyse') else ''}

Bestätigen oder ablehnen: {confirm_link}
(Es wird NICHTS automatisch ausgeführt – die Entscheidung liegt bei dir.)"""
    send_email(f"Rebalancing-Vorschlag #{proposal['id']}", body)
