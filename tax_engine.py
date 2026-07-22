"""
tax_engine.py – Steuer-Intelligence für deutsche Kapitalerträge.

Modell (vereinfacht, aber mit den drei wichtigsten Hebeln der Praxis):
1. Verlustverrechnungstopf: neue Gewinne werden zuerst gegen offene Verluste
   aus Vorjahren/Vorverkäufen verrechnet (pos_tax_config.verlusttopf_vorjahr).
2. Sparerpauschbetrag/Freistellungsauftrag: danach wird der verbleibende
   Gewinn gegen den noch nicht genutzten Freibetrag verrechnet.
3. Erst der danach verbleibende Betrag wird mit Abgeltungssteuer + Soli
   (+ optional Kirchensteuer) versteuert.

Nicht abgebildet: Vorabpauschale auf thesaurierende Fonds, Teilfreistellung
nach InvStG, unterjährige Verlusttopf-Trennung Aktien/Sonstiges. Das System
gibt Näherungswerte für die eigene Planung, keine Steuerberatung.
"""

from datetime import date

from config import effektiver_steuersatz, FREISTELLUNGSAUFTRAG_DEFAULT
from database import get_session, PosPosition, PosTaxConfig, PosTaxEvent, PosTransaction


def _get_or_create_tax_config(session, user_id: int) -> PosTaxConfig:
    cfg = session.query(PosTaxConfig).filter_by(user_id=user_id).first()
    if cfg is None:
        cfg = PosTaxConfig(user_id=user_id, freistellungsauftrag=FREISTELLUNGSAUFTRAG_DEFAULT)
        session.add(cfg)
        session.flush()
    return cfg


def _gewinn_fuer_transaktion(tx: PosTransaction) -> float:
    """Roh-Gewinn/Verlust einer Transaktion vor jeder Verrechnung."""
    if tx.typ == "verkauf":
        cost_basis = (tx.position.avg_buy_price or 0.0) if tx.position else 0.0
        return (tx.price - cost_basis) * tx.quantity - (tx.fees or 0.0)
    if tx.typ == "dividende":
        # Dividenden sind in voller Höhe Kapitalertrag (kein Cost-Basis-Abzug).
        return tx.quantity * tx.price
    return 0.0


def calculate_tax(transaction: PosTransaction, session=None) -> dict:
    """
    Berechnet die Steuer für eine Transaktion (verkauf/dividende) und verbucht
    das Ergebnis als PosTaxEvent inkl. Fortschreibung von Verlusttopf und
    genutztem Freistellungsauftrag. Für kauf/sparrate: keine Steuerwirkung.

    Wird `session` übergeben, läuft die Funktion INNERHALB dieser bestehenden
    Transaktion mit (kein eigener Commit) – so bleibt z.B. add_transaction()
    in portfolio.py atomar. Ohne `session` öffnet die Funktion selbst eine.
    """
    if transaction.typ not in ("verkauf", "dividende"):
        return {"gewinn": 0.0, "steuer": 0.0}

    def _run(s):
        user_id = transaction.portfolio.user_id
        cfg = _get_or_create_tax_config(s, user_id)

        gewinn = _gewinn_fuer_transaktion(transaction)
        steuer = 0.0
        verlusttopf_verrechnet = 0.0
        freistellung_verrechnet = 0.0

        if gewinn <= 0:
            # Verlust wandert in den Topf für zukünftige Gewinnverrechnung.
            cfg.verlusttopf_vorjahr = (cfg.verlusttopf_vorjahr or 0.0) + abs(gewinn)
        else:
            steuerpflichtig = gewinn

            if cfg.verlusttopf_vorjahr and cfg.verlusttopf_vorjahr > 0:
                verlusttopf_verrechnet = min(steuerpflichtig, cfg.verlusttopf_vorjahr)
                cfg.verlusttopf_vorjahr -= verlusttopf_verrechnet
                steuerpflichtig -= verlusttopf_verrechnet

            freibetrag_rest = max(0.0, (cfg.freistellungsauftrag or 0.0) - (cfg.freistellungsgenutzt or 0.0))
            if steuerpflichtig > 0 and freibetrag_rest > 0:
                freistellung_verrechnet = min(steuerpflichtig, freibetrag_rest)
                cfg.freistellungsgenutzt = (cfg.freistellungsgenutzt or 0.0) + freistellung_verrechnet
                steuerpflichtig -= freistellung_verrechnet

            if steuerpflichtig > 0:
                steuer = steuerpflichtig * effektiver_steuersatz(cfg.kirchensteuer)

        s.add(PosTaxEvent(
            user_id=user_id,
            transaction_id=transaction.id,
            gewinn_verlust=gewinn,
            steuer_betrag=steuer,
            datum=transaction.datum if transaction.datum else date.today(),
        ))

        return {
            "gewinn": gewinn,
            "steuer": steuer,
            "verlusttopf_verrechnet": verlusttopf_verrechnet,
            "freistellung_verrechnet": freistellung_verrechnet,
        }

    if session is not None:
        return _run(session)
    with get_session() as s:
        return _run(s)


def get_tax_preview(position_id: int, verkauf_preis: float, quantity: float = None) -> dict:
    """
    Steuervorschau VOR einem Verkauf – simuliert calculate_tax() ohne etwas zu
    persistieren. Ohne `quantity` wird der vollständige Bestand der Position
    angenommen.
    """
    with get_session() as session:
        position = session.get(PosPosition, position_id)
        if position is None:
            raise ValueError(f"Position {position_id} nicht gefunden")

        verkaufsmenge = quantity if quantity is not None else position.quantity
        gewinn = (verkauf_preis - (position.avg_buy_price or 0.0)) * verkaufsmenge

        user_id = position.portfolio.user_id
        cfg = _get_or_create_tax_config(session, user_id)

        steuerpflichtig = max(0.0, gewinn)
        verlusttopf_verrechnet = min(steuerpflichtig, max(0.0, cfg.verlusttopf_vorjahr or 0.0))
        steuerpflichtig -= verlusttopf_verrechnet

        freibetrag_rest = max(0.0, (cfg.freistellungsauftrag or 0.0) - (cfg.freistellungsgenutzt or 0.0))
        freistellung_verrechnet = min(steuerpflichtig, freibetrag_rest)
        steuerpflichtig -= freistellung_verrechnet

        steuer = steuerpflichtig * effektiver_steuersatz(cfg.kirchensteuer) if gewinn > 0 else 0.0
        netto_erloes = verkaufsmenge * verkauf_preis - steuer

        return {
            "position_id": position_id,
            "ticker": position.ticker,
            "quantity": verkaufsmenge,
            "brutto_gewinn": gewinn,
            "verlusttopf_verrechnet": verlusttopf_verrechnet,
            "freistellung_verrechnet": freistellung_verrechnet,
            "steuer": steuer,
            "netto_erloes": netto_erloes,
        }


def get_remaining_freistellung(user_id: int) -> float:
    """Verbleibender, noch nicht genutzter Freistellungsauftrag."""
    with get_session() as session:
        cfg = _get_or_create_tax_config(session, user_id)
        return max(0.0, (cfg.freistellungsauftrag or 0.0) - (cfg.freistellungsgenutzt or 0.0))


def update_freistellung_genutzt(user_id: int, betrag: float) -> float:
    """Erhöht den verbrauchten Freistellungsauftrag manuell um `betrag` und gibt den neuen Stand zurück."""
    with get_session() as session:
        cfg = _get_or_create_tax_config(session, user_id)
        cfg.freistellungsgenutzt = (cfg.freistellungsgenutzt or 0.0) + betrag
        return cfg.freistellungsgenutzt


def find_tax_loss_harvesting(user_id: int) -> list:
    """
    Positionen im unrealisierten Minus, sortiert nach potenzieller Steuerersparnis
    absteigend. Die Ersparnis ist eine Schätzung: sie geht davon aus, dass der
    realisierte Verlust gegen bestehende oder künftige Gewinne verrechnet wird.
    """
    with get_session() as session:
        from database import PosPortfolio
        portfolio_ids = [p.id for p in session.query(PosPortfolio).filter_by(user_id=user_id).all()]
        if not portfolio_ids:
            return []

        cfg = _get_or_create_tax_config(session, user_id)
        satz = effektiver_steuersatz(cfg.kirchensteuer)

        positions = (
            session.query(PosPosition)
            .filter(PosPosition.portfolio_id.in_(portfolio_ids))
            .all()
        )

        result = []
        for pos in positions:
            if pos.current_price is None or pos.avg_buy_price is None:
                continue
            if pos.current_price >= pos.avg_buy_price:
                continue
            verlust = (pos.avg_buy_price - pos.current_price) * pos.quantity
            result.append({
                "position_id": pos.id,
                "ticker": pos.display_name or pos.ticker,
                "quantity": pos.quantity,
                "avg_buy_price": pos.avg_buy_price,
                "current_price": pos.current_price,
                "unrealisierter_verlust": verlust,
                "geschaetzte_steuerersparnis": verlust * satz,
            })

        return sorted(result, key=lambda r: -r["geschaetzte_steuerersparnis"])


def get_optimal_sell_order(user_id: int, ziel_betrag: float) -> list:
    """
    Schlägt eine steuerlich möglichst günstige Reihenfolge vor, um `ziel_betrag`
    an Cash freizusetzen: Positionen mit Verlust bzw. der geringsten relativen
    Gewinnquote zuerst, hohe Gewinne zuletzt. Die letzte Position wird ggf. nur
    teilweise verkauft, um den Zielbetrag möglichst genau zu treffen.
    """
    with get_session() as session:
        from database import PosPortfolio
        portfolio_ids = [p.id for p in session.query(PosPortfolio).filter_by(user_id=user_id).all()]
        if not portfolio_ids:
            return []

        positions = (
            session.query(PosPosition)
            .filter(PosPosition.portfolio_id.in_(portfolio_ids))
            .all()
        )

        kandidaten = []
        for pos in positions:
            if not pos.quantity or pos.current_price is None:
                continue
            wert = pos.market_value
            gewinn_ratio = 0.0
            if pos.avg_buy_price:
                gewinn_ratio = (pos.current_price - pos.avg_buy_price) / pos.avg_buy_price
            kandidaten.append({
                "position_id": pos.id,
                "ticker": pos.ticker,
                "market_value": wert,
                "current_price": pos.current_price,
                "quantity_verfuegbar": pos.quantity,
                "gewinn_ratio": gewinn_ratio,
            })

        kandidaten.sort(key=lambda k: k["gewinn_ratio"])  # größte Verluste zuerst, größte Gewinne zuletzt

        plan = []
        rest = ziel_betrag
        for k in kandidaten:
            if rest <= 0:
                break
            verkaufswert = min(k["market_value"], rest)
            verkaufsmenge = verkaufswert / k["current_price"] if k["current_price"] else 0.0
            preview = get_tax_preview(k["position_id"], k["current_price"], quantity=verkaufsmenge)
            plan.append({
                "position_id": k["position_id"],
                "ticker": k["ticker"],
                "verkaufen_wert": verkaufswert,
                "verkaufen_quantity": verkaufsmenge,
                "geschaetzte_steuer": preview["steuer"],
            })
            rest -= verkaufswert

        return plan


def generate_jahresuebersicht(user_id: int, jahr: int) -> dict:
    """
    Jahresübersicht: realisierte Gewinne/Verluste und gezahlte Steuer im
    Kalenderjahr `jahr`, sowie der aktuelle (nicht rückwirkende) Stand von
    Freistellungsauftrag und Verlusttopf.
    """
    with get_session() as session:
        events = session.query(PosTaxEvent).filter_by(user_id=user_id).all()
        events_jahr = [e for e in events if e.datum and e.datum.year == jahr]

        gewinne = sum(e.gewinn_verlust for e in events_jahr if e.gewinn_verlust > 0)
        verluste = sum(-e.gewinn_verlust for e in events_jahr if e.gewinn_verlust < 0)
        steuer_gezahlt = sum(e.steuer_betrag for e in events_jahr)

        cfg = _get_or_create_tax_config(session, user_id)

        return {
            "jahr": jahr,
            "anzahl_ereignisse": len(events_jahr),
            "realisierte_gewinne": gewinne,
            "realisierte_verluste": verluste,
            "netto_ergebnis": gewinne - verluste,
            "steuer_gezahlt": steuer_gezahlt,
            "freistellungsauftrag": cfg.freistellungsauftrag,
            "freistellung_genutzt_aktuell": cfg.freistellungsgenutzt,
            "verlusttopf_aktuell": cfg.verlusttopf_vorjahr,
        }


# Rückwärtskompatibler Alias für den in der Spezifikation genannten Funktionsnamen
# (Nicht-ASCII-Funktionsnamen sind in Python zwar erlaubt, aber unüblich für Imports).
generate_jahresübersicht = generate_jahresuebersicht
