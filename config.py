"""
config.py – Zentrale Konfiguration von Portfolio-OS.
Alle Parameter werden aus .env geladen. Teilt die Postgres-Instanz mit dem
Trading Bot (eigene Tabellen mit Präfix "pos_", siehe database.py).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# DATENBANK
# ─────────────────────────────────────────────
# Gleiche Postgres-Instanz wie der Trading Bot. "postgres://" (Railway/Heroku-Format)
# wird zu "postgresql://" normalisiert, da SQLAlchemy 1.4+/2.0 nur Letzteres akzeptiert.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///portfolio_os.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ─────────────────────────────────────────────
# LLM (Anthropic Claude)
# ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL         = "claude-sonnet-4-6"
LLM_MAX_TOKENS    = 1536

# ─────────────────────────────────────────────
# DASHBOARD / LINKS
# ─────────────────────────────────────────────
BASE_URL = os.getenv("BASE_URL", "https://portfolio.straesschen.de")

# ─────────────────────────────────────────────
# ALERTS / SMTP
# ─────────────────────────────────────────────
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# E-Mail-Versand via smtplib (Standardbibliothek). Ohne SMTP_HOST/USER/PASSWORD
# wird nicht versendet, sondern nur geloggt (siehe notifier.py) – kein Absturz.
SMTP_HOST          = os.getenv("SMTP_HOST", "")
SMTP_PORT          = int(os.getenv("SMTP_PORT", "465"))
SMTP_FALLBACK_PORT = int(os.getenv("SMTP_FALLBACK_PORT", "2525"))
SMTP_USER          = os.getenv("SMTP_USER", "")
SMTP_PASSWORD      = os.getenv("SMTP_PASSWORD", "")
SMTP_TIMEOUT       = 10

# ─────────────────────────────────────────────
# REBALANCING-SCHWELLWERTE
# ─────────────────────────────────────────────
REBALANCING_THRESHOLD_ALERT    = float(os.getenv("REBALANCING_THRESHOLD_ALERT", "0.10"))    # 10% Abweichung → sofort E-Mail
REBALANCING_THRESHOLD_SPARRATE = float(os.getenv("REBALANCING_THRESHOLD_SPARRATE", "0.05"))  # 5% → Sparrate umlenken
REBALANCING_THRESHOLD_SELL     = float(os.getenv("REBALANCING_THRESHOLD_SELL", "0.15"))      # 15% → Verkauf empfehlen

# ─────────────────────────────────────────────
# STEUER-DEFAULTS (Deutschland)
# ─────────────────────────────────────────────
ABGELTUNGSSTEUER             = float(os.getenv("ABGELTUNGSSTEUER", "0.25"))
SOLI                         = float(os.getenv("SOLI", "0.055"))              # 5,5% Soli auf die Abgeltungssteuer
KIRCHENSTEUER_SATZ           = float(os.getenv("KIRCHENSTEUER_SATZ", "0.09")) # 8% oder 9% je Bundesland, auf die Abgeltungssteuer
FREISTELLUNGSAUFTRAG_DEFAULT = float(os.getenv("FREISTELLUNGSAUFTRAG_DEFAULT", "1000.0"))

# Effektiver Steuersatz ohne Kirchensteuer (25% + 5,5% Soli auf die 25%)
EFFEKTIVER_STEUERSATZ = ABGELTUNGSSTEUER * (1 + SOLI)


def effektiver_steuersatz(kirchensteuer: bool) -> float:
    """Effektiver Steuersatz auf Kapitalerträge, optional inkl. Kirchensteuer."""
    satz = ABGELTUNGSSTEUER * (1 + SOLI)
    if kirchensteuer:
        satz += ABGELTUNGSSTEUER * KIRCHENSTEUER_SATZ
    return satz


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
DAILY_UPDATE_HOUR     = 8   # Preise aktualisieren, Schwellwerte prüfen
WEEKLY_SUMMARY_HOUR   = 7   # Montags
MONTHLY_REPORT_HOUR   = 7   # 1. des Monats
QUARTERLY_REPORT_HOUR = 7   # Quartalsweise
YEARLY_REPORT_HOUR    = 7   # 1. Januar

# ─────────────────────────────────────────────
# ASSETKLASSEN (Standard-Seeds für pos_asset_classes)
# ─────────────────────────────────────────────
DEFAULT_ASSET_CLASSES = [
    "Aktien", "ETF", "Anleihen", "Krypto", "Immobilie", "Konto/Cash", "Sonstiges",
]


def validate_config() -> list[str]:
    """Prüft ob kritische Konfiguration vorhanden ist. Gibt Liste mit Warnings zurück."""
    warnings = []
    if not ANTHROPIC_API_KEY:
        warnings.append("ANTHROPIC_API_KEY fehlt – KI-Analyse deaktiviert (degraded mode)")
    if not ALERT_EMAIL or not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        warnings.append("SMTP/ALERT_EMAIL unvollständig – E-Mail-Benachrichtigungen deaktiviert (nur Logs)")
    if not (0 < REBALANCING_THRESHOLD_SPARRATE < REBALANCING_THRESHOLD_ALERT < REBALANCING_THRESHOLD_SELL):
        warnings.append("Rebalancing-Schwellwerte nicht aufsteigend (SPARRATE < ALERT < SELL erwartet)")
    return warnings
