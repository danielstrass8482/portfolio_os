"""
kontoauszug_analyzer.py – KI-gestützte Analyse von Kontoauszügen (PDF) für das
Haushaltsbuch (siehe dashboard.py Tab "💰 Haushaltsbuch" und die Kredit-Analyse
im Tab "🏠 Immobilie"). Liest hochgeladene PDFs per pypdf aus, lässt Claude
Buchungen extrahieren/kategorisieren und dedupliziert über mehrere Uploads
hinweg. Nutzt denselben Claude-Client wie llm_analyst.py (_ask) – bei
API-Ausfall degraded mode, bereits erkannte Batches bleiben erhalten.
"""

import io
import json

from pypdf import PdfReader

from config import ANTHROPIC_API_KEY
from llm_analyst import _ask

KONTOAUSZUG_SYSTEM_PROMPT = """Du bist Experte für deutsche Kontoauszüge.
Analysiere und extrahiere als JSON:
{
  "kreditbuchungen": [
    {"datum": "YYYY-MM-DD", "betrag": 1200.00,
     "empfaenger": "Bank XY", "verwendungszweck": "..."}
  ],
  "kreditanalyse": {
    "erste_buchung": "YYYY-MM-DD",
    "letzte_buchung": "YYYY-MM-DD",
    "anzahl_raten": 0,
    "durchschnittliche_rate": 0.0,
    "gesamt_bezahlt": 0.0
  },
  "buchungen": [
    {"datum": "YYYY-MM-DD", "betrag": 0.0, "empfaenger": "...",
     "verwendungszweck": "...", "kategorie": "...",
     "typ": "einnahme/ausgabe"}
  ]
}
Kategorien: Wohnen, Lebensmittel, Mobilität, Restaurant,
Abonnements, Gesundheit, Versicherung, Sparen, Gehalt, Sonstiges
Antworte NUR als JSON."""

BATCH_SIZE = 5
# Zeichen pro PDF, die in den Prompt übernommen werden – begrenzt den
# Kontextverbrauch bei einem 5er-Batch auf ein handhabbares Tokenbudget
# (analog zu llm_analyst.analyze_kredit_vertrag, dort 15000 Zeichen je Dokument).
MAX_ZEICHEN_PRO_PDF = 8000


def _extract_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        print(f"⚠️  PDF konnte nicht gelesen werden: {e} (übersprungen)")
        return ""


def _parse_batch_antwort(antwort: str) -> dict:
    bereinigt = antwort.strip()
    if bereinigt.startswith("```"):
        bereinigt = bereinigt.strip("`")
        if bereinigt.lower().startswith("json"):
            bereinigt = bereinigt[4:]
    try:
        daten = json.loads(bereinigt.strip())
    except json.JSONDecodeError as e:
        print(f"⚠️  Kontoauszug-Batch nicht parsebar: {e} (übersprungen)")
        return {}
    return daten if isinstance(daten, dict) else {}


def _dedupe(buchungen: list) -> list:
    """Duplikat-Erkennung: gleiche datum+betrag+empfaenger = einmal speichern."""
    gesehen = set()
    ergebnis = []
    for b in buchungen:
        if not isinstance(b, dict):
            continue
        key = (
            b.get("datum"),
            round(float(b.get("betrag") or 0.0), 2),
            (b.get("empfaenger") or "").strip().lower(),
        )
        if key in gesehen:
            continue
        gesehen.add(key)
        ergebnis.append(b)
    return ergebnis


def _kreditanalyse_berechnen(kreditbuchungen: list) -> dict:
    """
    Wird deterministisch aus den (bereits über alle Batches gemergten und
    deduplizierten) Kreditbuchungen berechnet statt aus der KI-Antwort eines
    einzelnen Batches übernommen – ein Batch sieht nie alle Kreditbuchungen
    auf einmal, eine vom LLM gelieferte "kreditanalyse" wäre also pro Batch
    immer nur ein Teilergebnis.
    """
    if not kreditbuchungen:
        return {
            "erste_buchung": None, "letzte_buchung": None, "anzahl_raten": 0,
            "durchschnittliche_rate": 0.0, "gesamt_bezahlt": 0.0,
        }
    daten_sortiert = sorted(kreditbuchungen, key=lambda b: b.get("datum") or "")
    betraege = [float(b.get("betrag") or 0.0) for b in kreditbuchungen]
    return {
        "erste_buchung": daten_sortiert[0].get("datum"),
        "letzte_buchung": daten_sortiert[-1].get("datum"),
        "anzahl_raten": len(kreditbuchungen),
        "durchschnittliche_rate": sum(betraege) / len(betraege) if betraege else 0.0,
        "gesamt_bezahlt": sum(betraege),
    }


def analyze_kontoauszuege(pdf_files: list, progress_callback=None) -> dict:
    """
    Analysiert eine Liste hochgeladener Kontoauszug-PDFs per Claude.

    pdf_files: Liste von (dateiname, bytes)-Tupeln.
    progress_callback: optional callable(aktueller_batch: int, anzahl_batches: int),
        wird vor jedem Batch-API-Call aufgerufen (fürs Fortschritts-UI im Dashboard).

    Bei mehr als 5 PDFs wird in 5er-Gruppen batch-verarbeitet (ein Claude-Call pro
    Batch). Buchungen werden über alle Batches hinweg anhand von
    datum+betrag+empfaenger dedupliziert. Bei API-Ausfall (siehe llm_analyst._ask)
    oder fehlendem Key: degraded mode, bereits erkannte Batches bleiben erhalten,
    "verfuegbar": False zeigt dem Aufrufer an, dass KEIN Batch ausgewertet werden konnte.
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY fehlt – Kontoauszug-Analyse übersprungen (degraded mode)")
        return {"verfuegbar": False, "buchungen": [], "kreditbuchungen": [],
                "kreditanalyse": _kreditanalyse_berechnen([])}

    alle_buchungen = []
    alle_kreditbuchungen = []
    mind_ein_batch_erfolgreich = False

    batches = [pdf_files[i:i + BATCH_SIZE] for i in range(0, len(pdf_files), BATCH_SIZE)]
    anzahl_batches = len(batches)

    for idx, batch in enumerate(batches, start=1):
        if progress_callback:
            progress_callback(idx, anzahl_batches)

        texte = []
        for dateiname, pdf_bytes in batch:
            text = _extract_text(pdf_bytes)
            if text.strip():
                texte.append(f"--- {dateiname} ---\n{text[:MAX_ZEICHEN_PRO_PDF]}")
        if not texte:
            continue

        user_content = "Kontoauszüge:\n\n" + "\n\n".join(texte)
        antwort = _ask(user_content, system=KONTOAUSZUG_SYSTEM_PROMPT, max_tokens=4096)
        if antwort is None:
            continue

        daten = _parse_batch_antwort(antwort)
        if not daten:
            continue

        mind_ein_batch_erfolgreich = True
        alle_buchungen.extend(daten.get("buchungen") or [])
        alle_kreditbuchungen.extend(daten.get("kreditbuchungen") or [])

    alle_buchungen = _dedupe(alle_buchungen)
    alle_kreditbuchungen = _dedupe(alle_kreditbuchungen)

    return {
        "verfuegbar": mind_ein_batch_erfolgreich,
        "buchungen": alle_buchungen,
        "kreditbuchungen": alle_kreditbuchungen,
        "kreditanalyse": _kreditanalyse_berechnen(alle_kreditbuchungen),
    }
