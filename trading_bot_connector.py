"""
trading_bot_connector.py – Read-only Zugriff auf den Trading Bot für Portfolio-OS.

Der Trading Bot (separates Repo/Prozess, siehe ~/trading_bot) läuft auf DERSELBEN
Postgres-Datenbank wie Portfolio-OS (siehe DATABASE_URL in beiden .env-Dateien),
nur mit eigenen, nicht "pos_"-präfixierten Tabellen ("trades", "daily_log", ...).
Es gibt also keinen Cross-Service-Import – nur ein Lesezugriff auf dieselbe DB
über das bereits von database.py aufgebaute SQLAlchemy-Engine-Objekt.

Der Bot handelt ausschließlich US-Werte über Alpaca (Preise/Kapital in USD,
siehe trading_bot/config.py MAX_CAPITAL_TOTAL) – der zurückgegebene Wert wird
deshalb nach EUR umgerechnet. Bei jedem Fehler (Tabelle fehlt, DB nicht
erreichbar, kein Snapshot vorhanden): degraded mode, 0.0 statt Absturz.
"""

import yfinance as yf
from sqlalchemy import text

from database import engine


def _usd_to_eur(betrag_usd: float) -> float:
    try:
        fx = yf.Ticker("EURUSD=X").info.get("regularMarketPrice", 1.08)
    except Exception:
        fx = 1.08
    return betrag_usd / fx


def get_trading_bot_value_eur() -> float:
    """
    Letzter bekannter Portfolio-Wert des Trading Bots (Tabelle "daily_log",
    wird vom Bot einmal täglich über save_daily_snapshot() geschrieben – siehe
    trading_bot/main.py), umgerechnet von USD nach EUR. 0.0 wenn (noch) kein
    Snapshot existiert oder die Tabelle nicht erreichbar ist.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT portfolio_value FROM daily_log ORDER BY log_date DESC LIMIT 1"
            )).fetchone()
        if row is None or row[0] is None:
            return 0.0
        return _usd_to_eur(float(row[0]))
    except Exception as e:
        print(f"⚠️  Trading-Bot-Wert nicht lesbar: {e} (0€ angenommen, degraded mode)")
        return 0.0
