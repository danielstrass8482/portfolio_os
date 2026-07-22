"""
rebalancing.py – Rebalancing-Logik (Hybrid-Modell):
1. Sparraten-Lenkung (kleine, laufende Korrektur ohne Verkauf/Steuer)
2. Quartalsweise Prüfung (turnusmäßiger Vorschlag)
3. Schwellwert-Trigger (sofortige Alarmierung bei starker Abweichung)

Das System empfiehlt ausschließlich – jede Umsetzung erfordert eine explizite
Bestätigung durch den Nutzer (confirm_proposal). Kein Autoexec.
"""

from config import (
    REBALANCING_THRESHOLD_ALERT, REBALANCING_THRESHOLD_SPARRATE,
    REBALANCING_THRESHOLD_SELL,
)
from database import get_session, PosRebalancingProposal
from portfolio import calculate_allocation


def _status_fuer_abweichung(abweichung_pct: float) -> str:
    """Ampel-Status für eine Abweichung (grün/gelb/rot)."""
    abw = abs(abweichung_pct)
    if abw >= REBALANCING_THRESHOLD_SELL:
        return "rot"
    if abw >= REBALANCING_THRESHOLD_ALERT:
        return "gelb"
    if abw >= REBALANCING_THRESHOLD_SPARRATE:
        return "hellgelb"
    return "gruen"


def calculate_deviations(user_id: int) -> list:
    """Ist/Soll-Abweichungen je Assetklasse, angereichert um einen Ampel-Status."""
    allokation = calculate_allocation(user_id)
    for eintrag in allokation:
        eintrag["status"] = _status_fuer_abweichung(eintrag["abweichung_pct"])
    return allokation


def check_thresholds(user_id: int) -> dict:
    """
    Gruppiert die aktuellen Abweichungen nach überschrittenem Schwellwert.
    Wird von main.py genutzt, um zu entscheiden ob eine Alert-Mail nötig ist.
    """
    deviations = calculate_deviations(user_id)
    return {
        "sell_empfehlung": [d for d in deviations if abs(d["abweichung_pct"]) >= REBALANCING_THRESHOLD_SELL],
        "alert": [
            d for d in deviations
            if REBALANCING_THRESHOLD_ALERT <= abs(d["abweichung_pct"]) < REBALANCING_THRESHOLD_SELL
        ],
        "sparrate_empfehlung": [
            d for d in deviations
            if REBALANCING_THRESHOLD_SPARRATE <= abs(d["abweichung_pct"]) < REBALANCING_THRESHOLD_ALERT
        ],
    }


def get_sparrate_empfehlung(user_id: int, sparrate_betrag: float) -> list:
    """
    Verteilt eine neue Sparrate proportional auf die untergewichteten Assetklassen,
    um sich der Zielgewichtung anzunähern – ohne bestehende Positionen zu verkaufen.
    Übergewichtete Klassen erhalten 0 €.
    """
    deviations = calculate_deviations(user_id)
    untergewichtet = [d for d in deviations if d["abweichung_pct"] < 0]

    gesamt_defizit = sum(-d["abweichung_pct"] for d in untergewichtet)
    if gesamt_defizit <= 0:
        # Nichts untergewichtet → gleichmäßig nach Zielgewichtung verteilen
        ziel_summe = sum(d["ziel_pct"] for d in deviations) or 1.0
        return [
            {
                "asset_class": d["asset_class"],
                "asset_class_id": d["asset_class_id"],
                "betrag": round(sparrate_betrag * (d["ziel_pct"] / ziel_summe), 2),
                "anteil_der_sparrate": d["ziel_pct"] / ziel_summe,
            }
            for d in deviations if d["ziel_pct"] > 0
        ]

    result = []
    for d in untergewichtet:
        anteil = (-d["abweichung_pct"]) / gesamt_defizit
        result.append({
            "asset_class": d["asset_class"],
            "asset_class_id": d["asset_class_id"],
            "betrag": round(sparrate_betrag * anteil, 2),
            "anteil_der_sparrate": anteil,
        })
    return sorted(result, key=lambda r: -r["betrag"])


def _begruendung_text(typ: str, deviations: list) -> str:
    """Regelbasierte Begründung (unabhängig von der KI verfügbar, s. llm_analyst.py)."""
    relevante = [d for d in deviations if d["status"] in ("gelb", "rot")]
    if not relevante:
        return f"Rebalancing-Check ({typ}): Alle Assetklassen liegen innerhalb der Toleranz."

    zeilen = [f"Rebalancing-Check ({typ}): {len(relevante)} Assetklasse(n) außerhalb der Toleranz."]
    for d in relevante:
        richtung = "übergewichtet" if d["abweichung_pct"] > 0 else "untergewichtet"
        zeilen.append(
            f"- {d['asset_class']}: Ist {d['ist_pct']*100:.1f}% vs. Ziel {d['ziel_pct']*100:.1f}% "
            f"({richtung} um {abs(d['abweichung_pct'])*100:.1f} Punkte, Status: {d['status']})"
        )
    return "\n".join(zeilen)


def create_rebalancing_proposal(user_id: int, typ: str, sparrate_betrag: float = None) -> dict:
    """
    Erstellt einen Rebalancing-Vorschlag (status="pending") für typ
    "sparrate" / "quartal" / "schwellwert". Reine Empfehlung – keine Order
    wird ausgeführt. `sparrate_betrag` ist nur für typ="sparrate" relevant.
    """
    if typ not in ("sparrate", "quartal", "schwellwert"):
        raise ValueError(f"Unbekannter Rebalancing-Typ: {typ}")

    deviations = calculate_deviations(user_id)

    vorschlag = {"typ": typ, "deviations": deviations}
    if typ == "sparrate" and sparrate_betrag:
        vorschlag["sparrate_verteilung"] = get_sparrate_empfehlung(user_id, sparrate_betrag)
    else:
        # quartal/schwellwert: Verkaufs-/Kaufempfehlung für alle Klassen außerhalb der Toleranz
        vorschlag["massnahmen"] = [
            {
                "asset_class": d["asset_class"],
                "aktion": "reduzieren" if d["abweichung_pct"] > 0 else "aufstocken",
                "abweichung_pct": d["abweichung_pct"],
                "status": d["status"],
            }
            for d in deviations if d["status"] in ("gelb", "rot")
        ]

    begruendung = _begruendung_text(typ, deviations)

    ki_analyse = None
    try:
        import llm_analyst
        ki_analyse = llm_analyst.analyze_rebalancing(vorschlag)
    except Exception as e:
        print(f"⚠️  KI-Analyse für Rebalancing-Vorschlag nicht verfügbar: {e} (degraded mode)")

    with get_session() as session:
        proposal = PosRebalancingProposal(
            user_id=user_id,
            status="pending",
            begruendung=begruendung,
            ki_analyse=ki_analyse,
        )
        proposal.set_vorschlag(vorschlag)
        session.add(proposal)
        session.flush()
        return {
            "id": proposal.id,
            "user_id": user_id,
            "typ": typ,
            "status": proposal.status,
            "begruendung": begruendung,
            "ki_analyse": ki_analyse,
            "vorschlag": vorschlag,
        }


def confirm_proposal(proposal_id: int, status: str = "confirmed") -> dict:
    """
    Markiert einen Vorschlag als bestätigt (Default) oder abgelehnt
    (status="rejected"). Führt selbst KEINE Trades aus – das bleibt manuell.
    """
    if status not in ("confirmed", "rejected"):
        raise ValueError("status muss 'confirmed' oder 'rejected' sein")

    with get_session() as session:
        proposal = session.get(PosRebalancingProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"Rebalancing-Vorschlag {proposal_id} nicht gefunden")
        proposal.status = status
        return {"id": proposal.id, "status": proposal.status}


def get_rebalancing_history(user_id: int) -> list:
    """Vergangene Rebalancing-Vorschläge, neueste zuerst."""
    with get_session() as session:
        proposals = (
            session.query(PosRebalancingProposal)
            .filter_by(user_id=user_id)
            .order_by(PosRebalancingProposal.erstellt_am.desc())
            .all()
        )
        return [
            {
                "id": p.id,
                "erstellt_am": p.erstellt_am,
                "status": p.status,
                "begruendung": p.begruendung,
                "ki_analyse": p.ki_analyse,
                "vorschlag": p.get_vorschlag(),
            }
            for p in proposals
        ]
