"""
kontoauszug_analyzer.py – KI-gestützte Analyse von Kontoauszügen (PDF) für das
Haushaltsbuch (siehe dashboard.py Tab "💰 Haushaltsbuch" und die Kredit-Analyse
im Tab "🏠 Immobilie"). Liest hochgeladene PDFs per pypdf aus, lässt Claude
Buchungen extrahieren/kategorisieren und dedupliziert über mehrere Uploads
hinweg. Nutzt denselben Claude-Client wie llm_analyst.py (_ask) – bei
API-Ausfall degraded mode, bereits erkannte Batches bleiben erhalten.

Manche PDFs (z.B. Sparda-Bank mit Monospace-Font und Leerzeichen-Formatierung)
liefern per pypdf nur unlesbaren "Zeichensalat". Für diese Fälle gibt es einen
Vision-Fallback: das PDF wird per pdf2image/poppler in PNG-Seiten umgewandelt
und direkt als Bild an Claude Vision geschickt (siehe analyze_with_vision).
"""

import base64
import io
import json
import os
import tempfile

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
  "immobilienkauf": {
    "datum": "YYYY-MM-DD",
    "gesamtbetrag": 172200.00,
    "empfaenger": "d.g Projekt Grünstadt GmbH",
    "objekt": "Wohnung 15.2, Einheit Nr. 17",
    "einzelzahlungen": [
      {"datum": "2023-03-03", "betrag": 163609.00},
      {"datum": "2023-03-08", "betrag": 8591.00}
    ]
  },
  "buchungen": [
    {"datum": "YYYY-MM-DD", "betrag": 0.0, "empfaenger": "...",
     "verwendungszweck": "...", "kategorie": "...",
     "typ": "einnahme/ausgabe"}
  ]
}
Kategorien: Wohnen, Lebensmittel, Mobilität, Restaurant,
Abonnements, Gesundheit, Versicherung, Sparen, Gehalt, Sonstiges

Erkenne auch Darlehensauszahlungen und Kaufpreiszahlungen.
Wenn du Überweisungen an Projektgesellschaften oder Bauträger
siehst (GmbH, Projekt, Bau, Wohnung im Verwendungszweck):
Das sind wahrscheinlich Immobilienkaufpreiszahlungen. Fasse zusammengehörige
Zahlungen an denselben Empfänger unter "immobilienkauf" zusammen, wobei
"gesamtbetrag" die Summe aller "einzelzahlungen" ist und "datum" das Datum der
ersten Zahlung. Wenn KEIN Immobilienkauf erkennbar ist: "immobilienkauf": null.
Antworte NUR als JSON."""

# Gleiche Extraktions-Regeln wie oben, nur der Eingabekanal ist ein Bild statt Text.
KONTOAUSZUG_VISION_SYSTEM_PROMPT = KONTOAUSZUG_SYSTEM_PROMPT + """

Wichtig: Die Eingabe besteht aus Bildern von Kontoauszug-Seiten (mehrere Seiten
eines PDFs als separate Bilder). Analysiere diese Kontoauszug-Seiten als Bilder.
Extrahiere alle Buchungen mit Datum, Betrag, Empfänger und Verwendungszweck.
Format: JSON wie oben beschrieben."""

BATCH_SIZE = 5
# Zeichen pro PDF, die in den Prompt übernommen werden – begrenzt den
# Kontextverbrauch bei einem 5er-Batch auf ein handhabbares Tokenbudget
# (analog zu llm_analyst.analyze_kredit_vertrag, dort 15000 Zeichen je Dokument).
MAX_ZEICHEN_PRO_PDF = 8000
# Ab wie vielen extrahierten Zeichen ein PDF überhaupt als Text-Kandidat gilt.
MIN_TEXT_ZEICHEN = 100
# Claude/Anthropic-API-Limit sind max. 100 Bilder pro Request; ein Kontoauszug
# hat i.d.R. wenige Seiten – wir deckeln defensiv, damit der Call nicht abgelehnt
# wird und das Tokenbudget beherrschbar bleibt.
MAX_BILDER_PRO_REQUEST = 20


def _extract_text(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        print(f"⚠️  PDF konnte nicht gelesen werden: {e} (übersprungen)")
        return ""


def _ist_lesbar(text: str) -> bool:
    """
    Heuristik gegen den "Zeichensalat" mancher Monospace-PDFs (z.B. Sparda-Bank):
    Ein Text gilt nur dann als lesbar, wenn er lang genug ist UND einen
    ausreichend hohen Buchstabenanteil hat. Ist er zu kurz oder überwiegend
    Sonderzeichen/Leerzeichen, wird auf die Vision-Analyse umgeschaltet.
    """
    if len(text.strip()) < MIN_TEXT_ZEICHEN:
        return False
    kompakt = "".join(text.split())
    if not kompakt:
        return False
    buchstaben = sum(1 for c in kompakt if c.isalpha())
    return (buchstaben / len(kompakt)) >= 0.4


def pdf_to_images(pdf_path: str) -> list:
    """
    Konvertiert die Seiten eines PDFs in eine Liste von PNG-Bytes (eine pro Seite)
    via pdf2image/poppler (dpi=200). Voraussetzung: poppler-utils ist auf dem
    System installiert. Bei Fehler (fehlendes poppler, defektes PDF): leere Liste
    (degraded mode), damit der Aufrufer sauber weitermachen kann.
    """
    try:
        from pdf2image import convert_from_path
        seiten = convert_from_path(pdf_path, dpi=200)
    except Exception as e:
        print(f"⚠️  PDF→Bild-Konvertierung fehlgeschlagen: {e} (übersprungen)")
        return []

    bilder = []
    for seite in seiten:
        puffer = io.BytesIO()
        seite.save(puffer, format="PNG")
        bilder.append(puffer.getvalue())
    return bilder


def _pdf_bytes_to_images(pdf_bytes: bytes) -> list:
    """Wie pdf_to_images, arbeitet aber auf rohen PDF-Bytes (die Uploads liegen als
    Bytes vor, nicht als Datei). Schreibt sie temporär und ruft pdf_to_images."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        return pdf_to_images(tmp_path)
    except Exception as e:
        print(f"⚠️  Temporäre PDF-Datei fehlgeschlagen: {e} (übersprungen)")
        return []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def analyze_with_vision(image_bytes_list: list) -> dict:
    """
    Schickt PNG-Seiten (als Bytes) direkt an Claude Vision und lässt die Buchungen
    extrahieren. Mehrere Seiten eines PDFs werden als separate Bilder übergeben.
    Gibt dasselbe dict-Format wie ein Text-Batch zurück (siehe _parse_batch_antwort)
    bzw. {} bei leerer Eingabe oder API-Fehler (degraded mode).
    """
    if not image_bytes_list:
        return {}

    content = []
    for png in image_bytes_list[:MAX_BILDER_PRO_REQUEST]:
        b64 = base64.standard_b64encode(png).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        })
    content.append({
        "type": "text",
        "text": "Extrahiere alle Buchungen dieser Kontoauszug-Seiten als JSON.",
    })

    antwort = _ask(content, system=KONTOAUSZUG_VISION_SYSTEM_PROMPT, max_tokens=4096)
    if antwort is None:
        return {}
    return _parse_batch_antwort(antwort)


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


def _dedupe_immobilienkaeufe(kaeufe: list) -> list:
    """Immobilienkäufe über Batches/PDFs hinweg deduplizieren (empfaenger+gesamtbetrag)."""
    gesehen = set()
    ergebnis = []
    for k in kaeufe:
        if not isinstance(k, dict):
            continue
        key = (
            (k.get("empfaenger") or "").strip().lower(),
            round(float(k.get("gesamtbetrag") or 0.0), 2),
        )
        if key in gesehen:
            continue
        gesehen.add(key)
        ergebnis.append(k)
    return ergebnis


def _merge_batch(daten: dict, buchungen: list, kreditbuchungen: list, immobilienkaeufe: list):
    """Ergebnis eines einzelnen Batches (Text oder Vision) in die Gesamtlisten mergen."""
    buchungen.extend(daten.get("buchungen") or [])
    kreditbuchungen.extend(daten.get("kreditbuchungen") or [])
    imm = daten.get("immobilienkauf")
    if isinstance(imm, dict) and (imm.get("gesamtbetrag") or imm.get("einzelzahlungen")):
        immobilienkaeufe.append(imm)


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
    progress_callback: optional callable(aktueller_schritt: int, anzahl_schritte: int),
        wird vor jedem Batch-/Vision-API-Call aufgerufen (fürs Fortschritts-UI).

    Ablauf je PDF:
      1. pypdf-Text-Extraktion versuchen.
      2. Ist der Text lesbar (>= 100 Zeichen und genügend Buchstaben, siehe
         _ist_lesbar), wandert das PDF in die Text-Verarbeitung (5er-Batches,
         ein Claude-Call pro Batch – schont Tokens).
      3. Ist der Text unlesbar/zu kurz (z.B. Sparda-Bank Monospace-Salat), wird
         automatisch auf Vision umgeschaltet: PDF → PNG-Seiten (pdf2image/poppler)
         → Claude Vision (analyze_with_vision), ein Call pro PDF.

    Buchungen werden über alles hinweg anhand von datum+betrag+empfaenger
    dedupliziert. Bei API-Ausfall (siehe llm_analyst._ask) oder fehlendem Key:
    degraded mode, bereits erkannte Batches bleiben erhalten, "verfuegbar": False
    zeigt dem Aufrufer an, dass KEIN Batch ausgewertet werden konnte.
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY fehlt – Kontoauszug-Analyse übersprungen (degraded mode)")
        return {"verfuegbar": False, "buchungen": [], "kreditbuchungen": [],
                "kreditanalyse": _kreditanalyse_berechnen([]), "immobilienkaeufe": []}

    # 1) Text-Extraktion und Entscheidung Text vs. Vision je PDF
    text_pdfs = []      # (dateiname, extrahierter_text)
    vision_pdfs = []    # (dateiname, pdf_bytes)
    for dateiname, pdf_bytes in pdf_files:
        text = _extract_text(pdf_bytes)
        if _ist_lesbar(text):
            text_pdfs.append((dateiname, text))
        else:
            print(f"ℹ️  '{dateiname}': Text unlesbar/zu kurz – Vision-Analyse")
            vision_pdfs.append((dateiname, pdf_bytes))

    text_batches = [text_pdfs[i:i + BATCH_SIZE] for i in range(0, len(text_pdfs), BATCH_SIZE)]
    anzahl_schritte = len(text_batches) + len(vision_pdfs)

    alle_buchungen = []
    alle_kreditbuchungen = []
    immobilienkaeufe = []
    mind_ein_batch_erfolgreich = False
    schritt = 0

    # 2) Text-Batches (klassischer Weg)
    for batch in text_batches:
        schritt += 1
        if progress_callback:
            progress_callback(schritt, anzahl_schritte)

        texte = [f"--- {dn} ---\n{t[:MAX_ZEICHEN_PRO_PDF]}" for dn, t in batch]
        user_content = "Kontoauszüge:\n\n" + "\n\n".join(texte)
        antwort = _ask(user_content, system=KONTOAUSZUG_SYSTEM_PROMPT, max_tokens=4096)
        if antwort is None:
            continue
        daten = _parse_batch_antwort(antwort)
        if not daten:
            continue
        mind_ein_batch_erfolgreich = True
        _merge_batch(daten, alle_buchungen, alle_kreditbuchungen, immobilienkaeufe)

    # 3) Vision-Fallback je unlesbarem PDF
    for dateiname, pdf_bytes in vision_pdfs:
        schritt += 1
        if progress_callback:
            progress_callback(schritt, anzahl_schritte)

        bilder = _pdf_bytes_to_images(pdf_bytes)
        if not bilder:
            print(f"⚠️  '{dateiname}': keine Bilder erzeugt – übersprungen")
            continue
        daten = analyze_with_vision(bilder)
        if not daten:
            continue
        mind_ein_batch_erfolgreich = True
        _merge_batch(daten, alle_buchungen, alle_kreditbuchungen, immobilienkaeufe)

    alle_buchungen = _dedupe(alle_buchungen)
    alle_kreditbuchungen = _dedupe(alle_kreditbuchungen)
    immobilienkaeufe = _dedupe_immobilienkaeufe(immobilienkaeufe)

    return {
        "verfuegbar": mind_ein_batch_erfolgreich,
        "buchungen": alle_buchungen,
        "kreditbuchungen": alle_kreditbuchungen,
        "kreditanalyse": _kreditanalyse_berechnen(alle_kreditbuchungen),
        "immobilienkaeufe": immobilienkaeufe,
    }
