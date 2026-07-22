"""
portfolio.py – Kernlogik für Portfolio-Verwaltung: Positionen, Transaktionen,
Kursaktualisierung, Ist/Soll-Allokation, CSV-Import und Performance-Kennzahlen.
"""

import re
from datetime import datetime, date, timedelta

import pandas as pd
import yfinance as yf

from config import TICKER_MAPPING
from database import (
    get_session, PosPortfolio, PosPosition, PosTransaction,
    PosAssetClass, PosTargetWeight, PosDailySnapshot,
)
import tax_engine


# ─────────────────────────────────────────────
# ÜBERSICHT / ALLOKATION
# ─────────────────────────────────────────────

def get_portfolio_summary(user_id: int) -> dict:
    """Gesamtvermögen und Assetklassen-Verteilung eines Nutzers über alle Portfolios."""
    with get_session() as session:
        portfolios = session.query(PosPortfolio).filter_by(user_id=user_id).all()
        portfolio_ids = [p.id for p in portfolios]
        positions = (
            session.query(PosPosition).filter(PosPosition.portfolio_id.in_(portfolio_ids)).all()
            if portfolio_ids else []
        )

        gesamtvermoegen = sum(p.market_value for p in positions)
        unrealized_pnl = sum(p.unrealized_pnl for p in positions)

        breakdown: dict = {}
        for pos in positions:
            klass = pos.asset_class.name if pos.asset_class else "Unklassifiziert"
            breakdown[klass] = breakdown.get(klass, 0.0) + pos.market_value

        return {
            "gesamtvermoegen": gesamtvermoegen,
            "unrealized_pnl": unrealized_pnl,
            "positions_count": len(positions),
            "portfolios_count": len(portfolios),
            "asset_breakdown": breakdown,
        }


def get_positions(user_id: int, asset_class: str = None) -> list:
    """
    Liste aller Positionen eines Nutzers über alle Portfolios, optional gefiltert
    nach Assetklasse (Slug, z.B. "aktien"). Gibt reine Dicts zurück (keine ORM-Objekte,
    damit sie außerhalb der Session – z.B. im Dashboard – gefahrlos verwendet werden können).
    """
    with get_session() as session:
        portfolios = session.query(PosPortfolio).filter_by(user_id=user_id).all()
        portfolio_by_id = {p.id: p for p in portfolios}
        if not portfolio_by_id:
            return []

        q = session.query(PosPosition).filter(PosPosition.portfolio_id.in_(portfolio_by_id.keys()))
        if asset_class:
            q = q.join(PosAssetClass).filter(PosAssetClass.slug == asset_class)

        result = []
        for pos in q.all():
            pf = portfolio_by_id[pos.portfolio_id]
            pnl_pct = None
            if pos.current_price and pos.avg_buy_price:
                pnl_pct = (pos.current_price / pos.avg_buy_price - 1) * 100
            result.append({
                "id": pos.id,
                "portfolio_id": pos.portfolio_id,
                "portfolio_name": pf.name,
                "broker": pf.broker,
                "ticker": pos.ticker,
                "name": pos.name,
                "asset_class": pos.asset_class.name if pos.asset_class else None,
                "quantity": pos.quantity,
                "avg_buy_price": pos.avg_buy_price,
                "current_price": pos.current_price,
                "market_value": pos.market_value,
                "unrealized_pnl": pos.unrealized_pnl,
                "unrealized_pnl_pct": pnl_pct,
                "currency": pos.currency,
                "last_updated": pos.last_updated,
            })
        return result


def calculate_allocation(user_id: int) -> list:
    """
    Vergleicht die Ist-Gewichtung (aktuelle Verteilung nach Assetklasse) mit der
    hinterlegten Ziel-Gewichtung (pos_target_weights). Sortiert nach größter
    absoluter Abweichung zuerst (relevant für Rebalancing/Ampel im Dashboard).
    """
    with get_session() as session:
        portfolios = session.query(PosPortfolio).filter_by(user_id=user_id).all()
        portfolio_ids = [p.id for p in portfolios]
        positions = (
            session.query(PosPosition).filter(PosPosition.portfolio_id.in_(portfolio_ids)).all()
            if portfolio_ids else []
        )
        gesamt = sum(p.market_value for p in positions) or 1.0

        ist_by_class = {}
        class_names = {}
        for pos in positions:
            key = pos.asset_class_id or 0
            ist_by_class[key] = ist_by_class.get(key, 0.0) + pos.market_value
            class_names[key] = pos.asset_class.name if pos.asset_class else "Unklassifiziert"

        targets = session.query(PosTargetWeight).filter_by(user_id=user_id).all()
        result, seen = [], set()
        for t in targets:
            ist_wert = ist_by_class.get(t.asset_class_id, 0.0)
            ist_pct = ist_wert / gesamt
            result.append({
                "asset_class_id": t.asset_class_id,
                "asset_class": t.asset_class.name if t.asset_class else class_names.get(t.asset_class_id, "?"),
                "ist_pct": ist_pct,
                "ziel_pct": t.target_pct,
                "abweichung_pct": ist_pct - t.target_pct,
                "min_pct": t.min_pct,
                "max_pct": t.max_pct,
            })
            seen.add(t.asset_class_id)

        # Bestand vorhanden, aber kein Ziel definiert → Ziel implizit 0%
        for key, wert in ist_by_class.items():
            if key not in seen:
                result.append({
                    "asset_class_id": key,
                    "asset_class": class_names.get(key, "Unklassifiziert"),
                    "ist_pct": wert / gesamt,
                    "ziel_pct": 0.0,
                    "abweichung_pct": wert / gesamt,
                    "min_pct": None,
                    "max_pct": None,
                })

        return sorted(result, key=lambda r: -abs(r["abweichung_pct"]))


# ─────────────────────────────────────────────
# TRANSAKTIONEN
# ─────────────────────────────────────────────

def add_transaction(portfolio_id: int, typ: str, ticker: str, quantity: float,
                     price: float, datum, fees: float = 0.0) -> dict:
    """
    Erfasst eine Transaktion (kauf/verkauf/dividende/sparrate) und aktualisiert
    Bestand sowie gleitenden Durchschnittspreis der zugehörigen Position.
    Bei 'verkauf' und 'dividende' wird automatisch die Steuer berechnet
    (siehe tax_engine.calculate_tax) und in der Transaktion vermerkt.
    """
    typ = typ.lower().strip()
    if typ not in ("kauf", "verkauf", "dividende", "sparrate"):
        raise ValueError(f"Unbekannter Transaktionstyp: {typ}")

    with get_session() as session:
        portfolio = session.get(PosPortfolio, portfolio_id)
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} nicht gefunden")

        position = (
            session.query(PosPosition)
            .filter_by(portfolio_id=portfolio_id, ticker=ticker)
            .first()
        )
        if position is None and typ in ("kauf", "sparrate"):
            position = PosPosition(
                portfolio_id=portfolio_id, ticker=ticker, name=ticker,
                quantity=0.0, avg_buy_price=0.0,
            )
            session.add(position)
            session.flush()

        if typ in ("kauf", "sparrate"):
            neuer_bestand = (position.quantity or 0.0) + quantity
            kosten_alt = (position.quantity or 0.0) * (position.avg_buy_price or 0.0)
            kosten_neu = kosten_alt + quantity * price + fees
            position.quantity = neuer_bestand
            position.avg_buy_price = (kosten_neu / neuer_bestand) if neuer_bestand else 0.0
            position.current_price = price
            position.last_updated = datetime.utcnow()

        elif typ == "verkauf":
            if position is None or position.quantity < quantity:
                raise ValueError(f"Nicht genug Bestand von {ticker} zum Verkauf")
            position.quantity -= quantity
            position.current_price = price
            position.last_updated = datetime.utcnow()

        tx = PosTransaction(
            portfolio_id=portfolio_id,
            position_id=position.id if position else None,
            typ=typ, datum=datum, quantity=quantity, price=price, fees=fees,
        )
        session.add(tx)
        session.flush()

        steuer_betrag = 0.0
        if typ in ("verkauf", "dividende"):
            ergebnis = tax_engine.calculate_tax(tx, session=session)
            steuer_betrag = ergebnis.get("steuer", 0.0)
            tx.steuern = steuer_betrag

        return {
            "transaction_id": tx.id,
            "position_id": position.id if position else None,
            "steuern": steuer_betrag,
        }


# ─────────────────────────────────────────────
# KURSAKTUALISIERUNG
# ─────────────────────────────────────────────

def update_prices() -> int:
    """
    Aktualisiert current_price aller Positionen via yfinance – funktioniert für
    beliebige Börsenplätze/Suffixe (z.B. "BMW.DE", "MC.PA", "AAPL"), da der
    Suffix bereits Teil des gespeicherten Tickers ist (siehe resolve_ticker()).

    Läuft fehlertolerant: einzelne nicht auflösbare Ticker überspringen den
    Rest nicht. Gibt die Anzahl erfolgreich aktualisierter Positionen zurück.
    """
    updated = 0
    with get_session() as session:
        positions = session.query(PosPosition).all()
        tickers = sorted({p.ticker for p in positions if p.ticker})
        if not tickers:
            return 0

        try:
            data = yf.download(tickers, period="5d", progress=False, group_by="ticker", threads=True)
        except Exception as e:
            print(f"⚠️  Preisaktualisierung fehlgeschlagen: {e} (degraded mode)")
            return 0

        # yfinance liefert mit group_by="ticker" IMMER einen MultiIndex-Spaltenaufbau
        # (Ticker, Preisfeld) – auch bei genau einem Ticker. Auf die Ticker-Anzahl
        # zu schauen war der Bug: bei einem einzelnen Ticker schlug data["Close"]
        # fehl und die Position wurde stillschweigend übersprungen.
        ist_multiindex = isinstance(data.columns, pd.MultiIndex)

        for pos in positions:
            try:
                closes = data[pos.ticker]["Close"] if ist_multiindex else data["Close"]
                price = float(closes.dropna().iloc[-1])
                pos.current_price = price
                pos.last_updated = datetime.utcnow()
                updated += 1
            except Exception:
                continue

    return updated


# ─────────────────────────────────────────────
# TICKER-AUFLÖSUNG (Suche/Validierung vor dem Speichern)
# ─────────────────────────────────────────────

# ISIN: 2 Länderbuchstaben + 9 alphanumerische Stellen (NSIN) + 1 Prüfziffer.
_ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def _yf_ticker_info(symbol: str) -> dict:
    """
    Prüft via yfinance.Ticker(symbol).info ob ein Symbol existiert und einen
    Kurs liefert. Gibt {"symbol","name","exchange"} zurück oder None.
    Netzwerk-/API-Fehler werden abgefangen (kein Absturz, degraded mode).
    """
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None
    if not info:
        return None
    name = info.get("longName") or info.get("shortName")
    hat_kurs = info.get("regularMarketPrice") is not None or info.get("previousClose") is not None
    if not name or not hat_kurs:
        return None
    return {
        "symbol": symbol,
        "name": name,
        "exchange": info.get("fullExchangeName") or info.get("exchange") or "?",
    }


def _yf_search(query: str, max_results: int = 6) -> list:
    """
    Sucht via yfinance-Volltextsuche (Name oder ISIN) nach passenden Tickern.
    Degradiert bei Fehlern (Netzwerk, API-Änderung) auf eine leere Liste,
    statt die Ticker-Auflösung abzubrechen.
    """
    try:
        ergebnis = yf.Search(query, max_results=max_results)
        quotes = ergebnis.quotes or []
    except Exception as e:
        print(f"⚠️  Ticker-Suche für '{query}' fehlgeschlagen: {e} (degraded mode)")
        return []

    kandidaten = []
    for q in quotes:
        symbol = q.get("symbol")
        if not symbol:
            continue
        kandidaten.append({
            "symbol": symbol,
            "name": q.get("longname") or q.get("shortname") or symbol,
            "exchange": q.get("exchange") or "?",
        })
    return kandidaten


def resolve_ticker(eingabe: str) -> list:
    """
    Löst eine Nutzereingabe (Ticker-Kürzel ODER ISIN) zu einem oder mehreren
    konkreten yfinance-Symbolen auf – zur Bestätigung durch den Nutzer VOR
    dem Speichern einer Position/Transaktion. Reihenfolge:

    1. TICKER_MAPPING (bekannte deutsche/französische Ticker, z.B. "BMW" -> "BMW.DE")
    2. ISIN erkannt -> yfinance-Suche (yf.Search)
    3. Direkter Symbolcheck via yfinance.Ticker(...).info
    4. Fallback: Symbol + ".DE" anhängen und erneut prüfen (deutsche Standardbörse)
    5. Letzter Fallback: yfinance-Volltextsuche (liefert ggf. mehrere Kandidaten,
       u.a. auch die passende .PA/.DE/... Notierung)

    Gibt eine Liste von Kandidaten [{"symbol", "name", "exchange"}, ...] zurück
    (leer, wenn nichts gefunden wurde – niemals ein Fehler/Exception).
    """
    if not eingabe or not eingabe.strip():
        return []
    roh = eingabe.strip().upper()

    if roh in TICKER_MAPPING:
        treffer = _yf_ticker_info(TICKER_MAPPING[roh])
        if treffer:
            return [treffer]

    if _ISIN_PATTERN.match(roh):
        return _yf_search(roh)

    direkt = _yf_ticker_info(roh)
    if direkt:
        return [direkt]

    mit_de = _yf_ticker_info(f"{roh}.DE")
    if mit_de:
        return [mit_de]

    return _yf_search(roh)


# ─────────────────────────────────────────────
# CSV-IMPORT (Comdirect / Trade Republic)
# ─────────────────────────────────────────────

BROKER_COLUMN_MAP = {
    "comdirect": {
        "datum": ["Datum", "Buchungstag"],
        "typ": ["Geschäftsart", "Transaktionstyp"],
        "ticker": ["WKN/ISIN", "ISIN", "WKN"],
        "quantity": ["Nominal/Stück", "Stück", "Nominale"],
        "price": ["Kurs", "Ausführungskurs"],
        "fees": ["Provision", "Gebühren"],
    },
    "tr": {
        "datum": ["Datum", "Date"],
        "typ": ["Typ", "Type", "Transaktionstyp"],
        "ticker": ["ISIN"],
        "quantity": ["Anzahl", "Shares", "Stück"],
        "price": ["Preis", "Price", "Kurs"],
        "fees": ["Gebühr", "Fee", "Gebühren"],
    },
}

TYP_MAP = {
    "kauf": "kauf", "buy": "kauf", "wertpapierkauf": "kauf", "kauf sparplan": "sparrate",
    "verkauf": "verkauf", "sell": "verkauf", "wertpapierverkauf": "verkauf",
    "dividende": "dividende", "dividend": "dividende", "ausschüttung": "dividende",
    "sparplan": "sparrate", "sparrate": "sparrate", "savings plan": "sparrate",
}


def import_csv(portfolio_id: int, filepath: str, broker: str) -> dict:
    """
    Importiert einen CSV-Kontoauszug (Comdirect oder Trade Republic, Broker-Kürzel
    "comdirect"/"tr") und legt für jede erkannte Zeile eine Transaktion an
    (via add_transaction). Nicht zuordenbare Zeilen werden übersprungen statt
    den gesamten Import abzubrechen. Gibt {"imported": n, "skipped": n} zurück.
    """
    broker_key = broker.lower().strip()
    mapping = BROKER_COLUMN_MAP.get(broker_key)
    if mapping is None:
        raise ValueError(f"Unbekannter Broker '{broker}'. Unterstützt: {list(BROKER_COLUMN_MAP)}")

    df = pd.read_csv(filepath, sep=None, engine="python")

    def _find_column(candidates):
        for c in candidates:
            if c in df.columns:
                return c
        return None

    cols = {key: _find_column(cands) for key, cands in mapping.items()}
    required_missing = [k for k in ("datum", "typ", "ticker", "quantity", "price") if cols[k] is None]
    if required_missing:
        raise ValueError(
            f"CSV fehlen erwartete Spalten für Broker '{broker}': {required_missing} "
            f"(gefunden: {list(df.columns)})"
        )

    imported, skipped = 0, 0
    for _, row in df.iterrows():
        try:
            roh_typ = str(row[cols["typ"]]).strip().lower()
            typ = TYP_MAP.get(roh_typ)
            if typ is None:
                skipped += 1
                continue

            datum = pd.to_datetime(row[cols["datum"]], dayfirst=True, errors="coerce")
            if pd.isna(datum):
                skipped += 1
                continue

            ticker = str(row[cols["ticker"]]).strip()
            quantity = abs(float(str(row[cols["quantity"]]).replace(".", "").replace(",", ".")
                                  if "," in str(row[cols["quantity"]]) else row[cols["quantity"]]))
            price = abs(float(str(row[cols["price"]]).replace(".", "").replace(",", ".")
                               if "," in str(row[cols["price"]]) else row[cols["price"]]))
            fees = 0.0
            if cols["fees"] and pd.notna(row[cols["fees"]]):
                raw_fee = str(row[cols["fees"]])
                fees = abs(float(raw_fee.replace(".", "").replace(",", ".") if "," in raw_fee else raw_fee))

            add_transaction(portfolio_id, typ, ticker, quantity, price, datum.date(), fees)
            imported += 1
        except Exception:
            skipped += 1
            continue

    return {"imported": imported, "skipped": skipped}


# ─────────────────────────────────────────────
# PERFORMANCE
# ─────────────────────────────────────────────

ZEITRAUM_TAGE = {"1W": 7, "1M": 30, "3M": 90, "6M": 182, "1J": 365}


def get_performance(user_id: int, zeitraum: str = "1J") -> dict:
    """
    Performance-Kennzahlen auf Basis der täglichen Snapshots (pos_daily_snapshots).
    zeitraum: "1W"/"1M"/"3M"/"6M"/"1J"/"YTD"/"ALL".

    - pnl_abs / pnl_pct: absolute/relative Wertänderung im Zeitraum
    - twr_pct: Time-Weighted-Return, verkettet aus Tagesrenditen (Näherung –
      setzt voraus, dass Snapshots NACH etwaigen Ein-/Auszahlungen erfasst werden;
      eine exakte TWR bräuchte täglich mitprotokollierte Cashflows)
    - mdd_pct: Maximum Drawdown im Zeitraum
    """
    with get_session() as session:
        snapshots = (
            session.query(PosDailySnapshot)
            .filter_by(user_id=user_id)
            .order_by(PosDailySnapshot.datum)
            .all()
        )
        if zeitraum == "YTD":
            start = date(date.today().year, 1, 1)
            snapshots = [s for s in snapshots if s.datum >= start]
        elif zeitraum != "ALL":
            tage = ZEITRAUM_TAGE.get(zeitraum, 365)
            start = date.today() - timedelta(days=tage)
            snapshots = [s for s in snapshots if s.datum >= start]

        werte = [s.gesamtvermoegen for s in snapshots]

    if len(werte) < 2:
        return {"pnl_abs": 0.0, "pnl_pct": 0.0, "twr_pct": 0.0, "mdd_pct": 0.0, "datenpunkte": len(werte)}

    pnl_abs = werte[-1] - werte[0]
    pnl_pct = (pnl_abs / werte[0] * 100) if werte[0] else 0.0

    twr = 1.0
    peak = werte[0]
    max_drawdown = 0.0
    for i in range(1, len(werte)):
        if werte[i - 1]:
            twr *= (werte[i] / werte[i - 1])
        peak = max(peak, werte[i])
        if peak:
            max_drawdown = min(max_drawdown, (werte[i] - peak) / peak)

    return {
        "pnl_abs": pnl_abs,
        "pnl_pct": pnl_pct,
        "twr_pct": (twr - 1) * 100,
        "mdd_pct": max_drawdown * 100,
        "datenpunkte": len(werte),
    }
