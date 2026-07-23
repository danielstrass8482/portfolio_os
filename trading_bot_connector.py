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


def get_bot_positions() -> list[dict]:
    """
    Offene Bot-Positionen (Tabelle "trades", status='OPEN') als reine Dicts –
    read-only für die Anzeige im Positionen-Tab. Leere Liste bei jedem Fehler
    (degraded mode), damit das Dashboard nie am Bot scheitert.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT ticker, direction, instrument_type, entry_price,
                       stop_loss, take_profit, quantity, capital_used,
                       rule_score, created_at, mode
                FROM trades
                WHERE status = 'OPEN'
                ORDER BY created_at DESC
            """)).fetchall()
        return [dict(r._mapping) for r in rows]
    except Exception as e:
        print(f"⚠️  Bot-Positionen nicht lesbar: {e} (leer angenommen, degraded mode)")
        return []


def get_bot_positions_detail() -> list[dict]:
    """
    Offene Bot-Positionen mit LIVE-Marktwert (USD und EUR) je Position.
    market_value_usd = aktueller Kurs (yfinance) × Menge; Fallback Einstiegskurs.
    Für Donut-Tooltip und Marktwert-Summe (siehe get_bot_positions_value_eur).
    """
    detail = []
    for p in get_bot_positions():
        try:
            kurs = yf.Ticker(p["ticker"]).fast_info.get("lastPrice") or p["entry_price"]
        except Exception:
            kurs = p["entry_price"]
        kurs = float(kurs)
        mv_usd = kurs * float(p["quantity"])
        detail.append({
            "ticker": p["ticker"],
            "quantity": float(p["quantity"]),
            "current_price_usd": round(kurs, 2),
            "market_value_usd": round(mv_usd, 2),
            "market_value_eur": round(_usd_to_eur(mv_usd), 2),
        })
    return detail


def get_bot_positions_value_eur() -> float:
    """Summe der LIVE-Marktwerte aller offenen Bot-Positionen in EUR."""
    return round(sum(d["market_value_eur"] for d in get_bot_positions_detail()), 2)


def get_bot_account_value_eur() -> dict:
    """
    LIVE-Kontowert des Trading Bots = Marktwert der offenen Positionen
    (aktueller Kurs × Menge) + freies Guthaben. Das freie Guthaben wird aus der
    DB abgeleitet: Startkapital (bot_config.MAX_CAPITAL_TOTAL) + realisierter PnL
    (geschlossene Trades) − in offenen Positionen gebundenes Kapital (zu Kosten).

    Genauer und aktueller als der einmal täglich geschriebene daily_log-Snapshot.
    Rückgabe: dict mit USD-/EUR-Summen und Positions-Detail (für den Tooltip).
    Bei Fehler: degraded mode (Cash 0, nur Positionen), niemals Absturz.
    """
    detail = get_bot_positions_detail()
    positionen_usd = sum(d["market_value_usd"] for d in detail)

    cash_usd = 0.0
    try:
        with engine.connect() as conn:
            realized = conn.execute(text(
                "SELECT COALESCE(SUM(pnl_usd), 0) FROM trades "
                "WHERE status IN ('CLOSED_SL', 'CLOSED_TP', 'CLOSED_MANUAL')"
            )).scalar() or 0.0
            invested = conn.execute(text(
                "SELECT COALESCE(SUM(capital_used), 0) FROM trades WHERE status = 'OPEN'"
            )).scalar() or 0.0
        start_capital = float(get_bot_config_all().get("MAX_CAPITAL_TOTAL", 475.0))
        cash_usd = start_capital + float(realized) - float(invested)
    except Exception as e:
        print(f"⚠️  Bot-Guthaben nicht berechenbar: {e} (nur Positionen, degraded mode)")

    total_usd = cash_usd + positionen_usd
    return {
        "positionen_usd":     round(positionen_usd, 2),
        "cash_usd":           round(cash_usd, 2),
        "total_usd":          round(total_usd, 2),
        "positionen_eur":     round(_usd_to_eur(positionen_usd), 2),
        "cash_eur":           round(_usd_to_eur(cash_usd), 2),
        "total_eur":          round(_usd_to_eur(total_usd), 2),
        "positionen_detail":  detail,
    }


def get_bot_config_all() -> dict:
    """Alle Bot-Parameter aus bot_config als dict key -> value (roher String)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT key, value FROM bot_config")).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        print(f"⚠️  Bot-Config nicht lesbar: {e} (leer angenommen, degraded mode)")
        return {}


def set_bot_config(werte: dict) -> None:
    """
    Schreibt/aktualisiert Bot-Parameter in bot_config (Upsert, Postgres).
    Wirkt beim nächsten Bot-Zyklus, da der Bot get_live_config() pro Zyklus liest.
    """
    with engine.begin() as conn:
        for key, value in werte.items():
            conn.execute(text("""
                INSERT INTO bot_config (key, value, updated_at)
                VALUES (:k, :v, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
            """), {"k": key, "v": str(value)})
