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
    PosAssetClass, PosTargetWeight, PosDailySnapshot, PosRealEstate,
)
import tax_engine
import trading_bot_connector


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


def get_total_wealth(user_id: int, include_trading_bot: bool = True) -> dict:
    """
    Gesamtvermögen eines Nutzers über ALLE Vermögensklassen (nicht nur Depot-
    Positionen wie get_portfolio_summary): Depot/ETF/Aktien/Krypto/Tagesgeld-
    Positionen (bereits in get_portfolio_summary erfasst, da strukturell
    identisch gepflegt) + Immobilien-Eigenkapital (Schätzwert - Restschuld,
    über ALLE Immobilien des Nutzers summiert) + Trading-Bot-Depotwert
    (separate Postgres-Tabellen, siehe trading_bot_connector.py).

    `include_trading_bot=False` für den Familien-Modus: der Trading Bot ist ein
    einzelnes gemeinsames Konto, kein personenbezogenes Vermögen – der Aufrufer
    addiert ihn dort selbst genau einmal statt pro Nutzer (siehe dashboard.py).
    """
    summary = get_portfolio_summary(user_id)
    breakdown = dict(summary["asset_breakdown"])

    with get_session() as session:
        immobilien = session.query(PosRealEstate).filter_by(user_id=user_id).all()
        schaetzwert_summe = sum((im.letzter_schaetzwert or im.kaufpreis or 0.0) for im in immobilien)
        restschuld_summe = sum((im.restschuld or 0.0) for im in immobilien)
    immobilien_eigenkapital = schaetzwert_summe - restschuld_summe
    if immobilien:
        breakdown["Immobilie"] = breakdown.get("Immobilie", 0.0) + immobilien_eigenkapital

    trading_bot_wert = 0.0
    if include_trading_bot:
        trading_bot_wert = trading_bot_connector.get_trading_bot_value_eur()
        if trading_bot_wert:
            breakdown["Trading Bot"] = breakdown.get("Trading Bot", 0.0) + trading_bot_wert

    return {
        "gesamtvermoegen": summary["gesamtvermoegen"] + immobilien_eigenkapital + trading_bot_wert,
        "unrealized_pnl": summary["unrealized_pnl"],
        "positions_count": summary["positions_count"],
        "portfolios_count": summary["portfolios_count"],
        "asset_breakdown": breakdown,
        "immobilien_eigenkapital": immobilien_eigenkapital,
        "immobilien_schaetzwert_summe": schaetzwert_summe,
        "immobilien_restschuld_summe": restschuld_summe,
        "trading_bot_wert": trading_bot_wert,
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
                # "name": Anzeigename mit Fallback (display_name wenn gesetzt, sonst Ticker) –
                # das ist der Wert, der überall im Dashboard angezeigt wird. "display_name"
                # bleibt zusätzlich roh (auch None) verfügbar, damit Bearbeiten-Formulare den
                # tatsächlich gespeicherten Wert vorbelegen können statt des aufgelösten Fallbacks.
                "name": pos.display_name or pos.ticker,
                "display_name": pos.display_name,
                "asset_class": pos.asset_class.name if pos.asset_class else "Unklassifiziert",
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

def normalize_ticker(ticker: str) -> str:
    """
    Normalisiert einen Ticker für den Duplikat-Abgleich: immer uppercase;
    bekannte deutsche/französische Ticker aus TICKER_MAPPING werden auf ihr
    kanonisches Symbol inkl. Börsensuffix aufgelöst (z.B. "SIX3" -> "SIX3.DE"),
    damit unterschiedliche Schreibweisen derselben Aktie (manuelle Eingabe,
    Ticker-Suche, CSV-Import) dieselbe Position treffen statt ein Duplikat
    anzulegen.
    """
    if not ticker:
        return ticker
    roh = ticker.strip().upper()
    return TICKER_MAPPING.get(roh, roh)


def _finde_bestehende_position(session, portfolio_id: int, ticker: str):
    """
    Sucht eine bereits vorhandene Position im Portfolio – neben dem exakten
    (normalisierten) Ticker zusätzlich mit/ohne ".DE"-Suffix getauscht, damit
    z.B. "SIX3" und "SIX3.DE" als dieselbe Position erkannt werden, statt ein
    Duplikat anzulegen (siehe normalize_ticker()).
    """
    kandidaten = {ticker}
    if ticker.endswith(".DE"):
        kandidaten.add(ticker[:-3])
    else:
        kandidaten.add(f"{ticker}.DE")
    return (
        session.query(PosPosition)
        .filter(PosPosition.portfolio_id == portfolio_id, PosPosition.ticker.in_(kandidaten))
        .first()
    )


def add_transaction(portfolio_id: int, typ: str, ticker: str, quantity: float,
                     price: float, datum, fees: float = 0.0, asset_class_id: int = None) -> dict:
    """
    Erfasst eine Transaktion (kauf/verkauf/dividende/sparrate) und aktualisiert
    Bestand sowie gleitenden Durchschnittspreis der zugehörigen Position.
    Bei 'verkauf' und 'dividende' wird automatisch die Steuer berechnet
    (siehe tax_engine.calculate_tax) und in der Transaktion vermerkt.

    `asset_class_id` wird bei NEU angelegten Positionen gesetzt; existiert die
    Position bereits, aber ohne Assetklasse, wird sie nachträglich ergänzt.
    """
    typ = typ.lower().strip()
    if typ not in ("kauf", "verkauf", "dividende", "sparrate"):
        raise ValueError(f"Unbekannter Transaktionstyp: {typ}")

    ticker = normalize_ticker(ticker)

    with get_session() as session:
        portfolio = session.get(PosPortfolio, portfolio_id)
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} nicht gefunden")

        position = _finde_bestehende_position(session, portfolio_id, ticker)
        if position is None and typ in ("kauf", "sparrate"):
            position = PosPosition(
                portfolio_id=portfolio_id, ticker=ticker, name=ticker,
                quantity=0.0, avg_buy_price=0.0, asset_class_id=asset_class_id,
            )
            session.add(position)
            session.flush()
        elif position is not None and asset_class_id and not position.asset_class_id:
            position.asset_class_id = asset_class_id

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


def _recompute_position(session, position_id: int):
    """
    Berechnet Bestand und gleitenden Durchschnittspreis einer Position komplett
    aus ihren (verbleibenden) Transaktionen neu – wird nach delete_transaction()
    aufgerufen, damit gelöschte Buchungen korrekt rückgängig gemacht werden.
    """
    position = session.get(PosPosition, position_id)
    if position is None:
        return
    txs = (
        session.query(PosTransaction)
        .filter_by(position_id=position_id)
        .order_by(PosTransaction.datum, PosTransaction.id)
        .all()
    )
    qty, avg = 0.0, 0.0
    for tx in txs:
        if tx.typ in ("kauf", "sparrate"):
            neuer_bestand = qty + tx.quantity
            kosten_neu = qty * avg + tx.quantity * tx.price + (tx.fees or 0.0)
            qty = neuer_bestand
            avg = (kosten_neu / neuer_bestand) if neuer_bestand else 0.0
        elif tx.typ == "verkauf":
            qty -= tx.quantity
    position.quantity = qty
    position.avg_buy_price = avg


def delete_transaction(transaction_id: int):
    """Löscht eine Transaktion und berechnet die zugehörige Position neu (siehe _recompute_position)."""
    with get_session() as session:
        tx = session.get(PosTransaction, transaction_id)
        if tx is None:
            raise ValueError(f"Transaktion {transaction_id} nicht gefunden")
        position_id = tx.position_id
        session.delete(tx)
        session.flush()
        if position_id:
            _recompute_position(session, position_id)


def update_transaction(transaction_id: int, typ: str = None, quantity: float = None,
                        price: float = None, datum=None, fees: float = None):
    """
    Ändert eine bestehende Transaktion (nur übergebene Felder) und berechnet die
    zugehörige Position anschließend komplett aus allen Transaktionen neu (siehe
    _recompute_position), damit Bestand/Ø-Kaufpreis nach der Änderung wieder
    korrekt sind. Bereits gebuchte Steuer (tx.steuern) wird dabei NICHT neu
    berechnet – wie auch delete_transaction() die Steuer-Historie unangetastet lässt.
    """
    with get_session() as session:
        tx = session.get(PosTransaction, transaction_id)
        if tx is None:
            raise ValueError(f"Transaktion {transaction_id} nicht gefunden")
        if typ is not None:
            typ = typ.lower().strip()
            if typ not in ("kauf", "verkauf", "dividende", "sparrate"):
                raise ValueError(f"Unbekannter Transaktionstyp: {typ}")
            tx.typ = typ
        if quantity is not None:
            tx.quantity = quantity
        if price is not None:
            tx.price = price
        if datum is not None:
            tx.datum = datum
        if fees is not None:
            tx.fees = fees
        session.flush()
        if tx.position_id:
            _recompute_position(session, tx.position_id)


def delete_position(position_id: int):
    """Löscht eine Position samt aller zugehörigen Transaktionen (cascade, siehe database.py)."""
    with get_session() as session:
        position = session.get(PosPosition, position_id)
        if position is None:
            raise ValueError(f"Position {position_id} nicht gefunden")
        session.delete(position)


# ─────────────────────────────────────────────
# PORTFOLIO-VERWALTUNG (CRUD)
# ─────────────────────────────────────────────

def update_portfolio(portfolio_id: int, name: str = None, typ: str = None, broker: str = None,
                      is_kinderdepot: bool = None):
    """Ändert Name/Typ/Broker/Kinderdepot-Flag eines bestehenden Portfolios (nur übergebene Felder werden geändert)."""
    with get_session() as session:
        portfolio = session.get(PosPortfolio, portfolio_id)
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} nicht gefunden")
        if name is not None:
            portfolio.name = name
        if typ is not None:
            portfolio.typ = typ
        if broker is not None:
            portfolio.broker = broker
        if is_kinderdepot is not None:
            portfolio.is_kinderdepot = is_kinderdepot


def update_position(position_id: int, display_name: str = None, ticker: str = None,
                     asset_class_id: int = None, quantity: float = None, avg_buy_price: float = None):
    """
    Ändert Anzeigename/Ticker/Assetklasse/Bestand/Ø-Kaufpreis einer bestehenden Position
    (nur übergebene Felder werden geändert). `display_name=""` löscht den Anzeigenamen
    explizit wieder (get_positions() fällt dann auf den Ticker zurück, siehe dort).
    """
    with get_session() as session:
        pos = session.get(PosPosition, position_id)
        if pos is None:
            raise ValueError(f"Position {position_id} nicht gefunden")
        if display_name is not None:
            pos.display_name = display_name
        if ticker is not None:
            pos.ticker = normalize_ticker(ticker)
        if asset_class_id is not None:
            pos.asset_class_id = asset_class_id
        if quantity is not None:
            pos.quantity = quantity
        if avg_buy_price is not None:
            pos.avg_buy_price = avg_buy_price


def delete_portfolio(portfolio_id: int):
    """
    Löscht ein Portfolio – NUR wenn es keine Positionen mehr enthält, damit
    nicht versehentlich Bestandsdaten/Transaktionshistorie verschwinden.
    Positionen zuerst einzeln über delete_position() entfernen.
    """
    with get_session() as session:
        portfolio = session.get(PosPortfolio, portfolio_id)
        if portfolio is None:
            raise ValueError(f"Portfolio {portfolio_id} nicht gefunden")
        anzahl_positionen = (
            session.query(PosPosition).filter_by(portfolio_id=portfolio_id).count()
        )
        if anzahl_positionen > 0:
            raise ValueError(
                f"Portfolio '{portfolio.name}' enthält noch {anzahl_positionen} Position(en) – "
                f"diese zuerst löschen."
            )
        session.delete(portfolio)


# ─────────────────────────────────────────────
# KURSAKTUALISIERUNG
# ─────────────────────────────────────────────

def _aktueller_kurs(ticker: str) -> float:
    """
    Aktueller Kurs für EINEN Ticker (beliebiger Börsensuffix, z.B. ".DE"/".PA").
    Erst yfinance.Ticker(...).fast_info["lastPrice"] (ein schneller Request),
    bei Fehler/fehlendem Preis Fallback auf yfinance.download(period="1d").
    Gibt None zurück, wenn der Ticker auf keinem der beiden Wege auflösbar ist.
    """
    try:
        preis = yf.Ticker(ticker).fast_info.get("lastPrice")
        if preis:
            return float(preis)
    except Exception:
        pass

    try:
        data = yf.download(ticker, period="1d", progress=False)
        if not data.empty:
            closes = data["Close"]
            if hasattr(closes, "columns"):  # MultiIndex-Fall: DataFrame statt Series
                closes = closes.iloc[:, 0]
            closes = closes.dropna()
            if len(closes):
                return float(closes.iloc[-1])
    except Exception:
        pass

    return None


def get_price_in_eur(ticker: str) -> float:
    """
    Aktueller Kurs eines Tickers, umgerechnet in EUR. Liest die von yfinance
    gemeldete Handelswährung (info["currency"]) aus statt den Rohpreis
    unkonvertiert zu übernehmen – London-Stock-Exchange-Ticker (".L") notieren
    in Pence (GBp), nicht Pfund, und müssen vor der FX-Umrechnung durch 100
    geteilt werden. yfinance meldet das nicht bei jedem ".L"-Ticker korrekt
    als "GBp", daher zusätzlich am Suffix erkannt.
    """
    t = yf.Ticker(ticker)
    info = t.info
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    currency = info.get("currency", "EUR")

    if price is None:
        return None

    if currency == "GBp" or ticker.upper().endswith(".L"):
        price = price / 100
        currency = "GBP"

    if currency == "GBP":
        fx = yf.Ticker("GBPEUR=X").info.get("regularMarketPrice", 1.18)
        price = price * fx
    elif currency == "USD":
        fx = yf.Ticker("EURUSD=X").info.get("regularMarketPrice", 1.08)
        price = price / fx
    elif currency == "CHF":
        fx = yf.Ticker("CHFEUR=X").info.get("regularMarketPrice", 1.04)
        price = price * fx
    # EUR bleibt EUR

    return round(price, 2)


def update_prices() -> int:
    """
    Aktualisiert current_price aller Positionen – Preise werden über
    get_price_in_eur() geholt, laufen also immer bereits in EUR umgerechnet
    ein (siehe get_price_in_eur()). Läuft fehlertolerant: einzelne nicht
    auflösbare oder fehlerhafte Ticker überspringen den Rest nicht, werden
    aber geloggt. Gibt die Anzahl erfolgreich aktualisierter Positionen zurück.
    """
    updated = 0
    with get_session() as session:
        positions = session.query(PosPosition).all()
        for pos in positions:
            if not pos.ticker:
                continue
            try:
                preis = get_price_in_eur(pos.ticker)
            except Exception as e:
                print(f"⚠️  Fehler beim Kursabruf für Ticker '{pos.ticker}': {e} (übersprungen)")
                continue
            if preis is None:
                print(f"⚠️  Kein aktueller Kurs für Ticker '{pos.ticker}' gefunden (übersprungen)")
                continue
            pos.current_price = preis
            pos.currency = "EUR"
            pos.last_updated = datetime.utcnow()
            updated += 1

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

    # Cache: dieselbe ISIN/derselbe Ticker kommt in einem Kontoauszug oft mehrfach
    # vor (mehrere Sparplan-Ausführungen etc.) – pro eindeutigem Rohwert nur
    # einmal via yfinance auflösen, statt bei jeder Zeile erneut zu suchen.
    ticker_cache: dict = {}

    def _aufgeloester_ticker(roh_ticker: str) -> str:
        if roh_ticker not in ticker_cache:
            kandidaten = resolve_ticker(roh_ticker)
            # Broker-Exporte liefern meist ISIN/WKN statt Symbol – diese über
            # dieselbe Ticker-Suche wie im Dashboard auflösen (behebt das
            # Duplikat-Problem, wenn dieselbe Aktie später manuell mit einem
            # bereits mit .DE-Suffix versehenen Symbol nachgebucht wird).
            ticker_cache[roh_ticker] = kandidaten[0]["symbol"] if kandidaten else normalize_ticker(roh_ticker)
        return ticker_cache[roh_ticker]

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

            roh_ticker = str(row[cols["ticker"]]).strip()
            ticker = _aufgeloester_ticker(roh_ticker)
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
