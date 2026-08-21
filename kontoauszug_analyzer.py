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
import csv
import io
import json
import os
import re
import tempfile
from datetime import datetime

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
     "typ": "einnahme/ausgabe", "referenz": "End-to-End-Referenz oder null"}
  ]
}
Kategorien: Wohnen, Lebensmittel, Mobilität, Restaurant,
Abonnements, Gesundheit, Versicherung, Sparen, Gehalt, Sonstiges

"referenz": die "End-to-End-Ref." (oder falls nicht vorhanden: "Mandatsref")
der Buchung, exakt wie im Dokument angegeben. Steht dort "NOTPROVIDED" oder
gibt es keine erkennbare Referenz (z.B. bei Kartenzahlungen): null setzen -
"NOTPROVIDED" ist ein Platzhalter der Bank und KEINE echte Referenz.

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

# Wie viele Text-Chunks (siehe MAX_ZEICHEN_PRO_PDF/_chunk_text) pro LLM-Call
# gebündelt werden. Niedrig gehalten (2 statt vormals 5), weil ein Chunk bei
# einem dichten Kontoauszug ~15-20 Buchungen enthalten kann – 5 Chunks auf
# einmal würden die JSON-Antwort über das Output-Tokenbudget hinaus aufblähen
# (siehe max_tokens unten) und dadurch mitten im JSON abgeschnitten werden.
BATCH_SIZE = 2
# Maximale Chunk-Größe (Zeichen) je LLM-Call-Eingabe – begrenzt den
# Kontextverbrauch pro Batch auf ein handhabbares Tokenbudget
# (analog zu llm_analyst.analyze_kredit_vertrag, dort 15000 Zeichen je Dokument).
# WICHTIG: Kein harter Text-Cutoff mehr (früher: t[:MAX_ZEICHEN_PRO_PDF], hat bei
# langen Auszügen >85% der Buchungen stillschweigend verworfen) – lange PDFs
# werden stattdessen in mehrere Chunks à max. MAX_ZEICHEN_PRO_PDF Zeichen
# aufgeteilt (siehe _chunk_text), jeder Chunk läuft als eigener Batch-Eintrag
# durch dieselbe Pipeline.
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


# Erkennt die Kopfzeile einer Buchung in deutschen Kontoauszügen: irgendwo in
# der Zeile ein Datum (TT.MM[.JJJJ]) UND ein Betrag im deutschen Zahlenformat
# (mit Tausenderpunkt/Komma), unabhängig von Bank/Layout (Commerzbank, Sparkasse,
# ...). Folgezeilen einer Buchung (Verwendungszweck, Mandatsref, Gläubiger-ID, …)
# matchen das i.d.R. NICHT und bleiben so im selben Block wie ihre Kopfzeile.
_BUCHUNGSZEILE_RE = re.compile(r"\d{1,2}\.\d{1,2}(\.\d{2,4})?.*\d{1,3}(\.\d{3})*,\d{2}-?\s*$")


def _split_in_buchungsbloecke(text: str) -> list:
    """
    Zerlegt den Kontoauszug-Text in Blöcke, die jeweils an einer erkennbaren
    Buchungszeile beginnen (siehe _BUCHUNGSZEILE_RE) und alle folgenden
    Fortsetzungszeilen bis zur nächsten Buchungszeile enthalten. Wird von
    _chunk_text genutzt, damit beim Aufteilen in Chunks nie mitten durch eine
    Buchung geschnitten wird.
    """
    bloecke = []
    aktueller = []
    for zeile in text.split("\n"):
        if _BUCHUNGSZEILE_RE.search(zeile.strip()) and aktueller:
            bloecke.append("\n".join(aktueller))
            aktueller = [zeile]
        else:
            aktueller.append(zeile)
    if aktueller:
        bloecke.append("\n".join(aktueller))
    return bloecke


def _chunk_text(text: str, max_len: int) -> list:
    """
    Teilt einen (potenziell langen) Kontoauszug-Text in Chunks von je
    höchstens max_len Zeichen auf – als Ersatz für den früheren harten
    Text-Cutoff. Chunk-Grenzen liegen an Buchungsblock-Grenzen
    (_split_in_buchungsbloecke), sodass eine einzelne Buchung nie über zwei
    Chunks verteilt wird (kein Zerschneiden, kein Duplikat-Risiko an der Naht).
    Ein einzelner Block, der für sich schon max_len überschreitet (z.B. ein
    Format ohne erkennbare Buchungszeilen), wird als Notfall an einfachen
    Zeilenumbrüchen aufgeteilt, statt Inhalt zu verwerfen.
    """
    if len(text) <= max_len:
        return [text] if text.strip() else []

    chunks = []
    aktuell = ""
    for block in _split_in_buchungsbloecke(text):
        kandidat = f"{aktuell}\n{block}" if aktuell else block
        if len(kandidat) <= max_len:
            aktuell = kandidat
            continue
        if aktuell:
            chunks.append(aktuell)
            aktuell = ""
        if len(block) <= max_len:
            aktuell = block
            continue
        # Notfall: einzelner Block zu groß -> an Zeilenumbrüchen aufteilen.
        teil = ""
        for zeile in block.split("\n"):
            kandidat2 = f"{teil}\n{zeile}" if teil else zeile
            if len(kandidat2) <= max_len:
                teil = kandidat2
            else:
                if teil:
                    chunks.append(teil)
                teil = zeile
        aktuell = teil
    if aktuell:
        chunks.append(aktuell)
    return chunks


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

    # max_tokens=8192 statt 4096: eine einzelne dichte Kontoauszug-Seite kann schon
    # 15-20 Buchungen enthalten, deren JSON sonst mitten im String abgeschnitten wird.
    antwort = _ask(content, system=KONTOAUSZUG_VISION_SYSTEM_PROMPT, max_tokens=8192)
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
    """
    Duplikat-Erkennung: gleiche datum+betrag+empfaenger = einmal speichern.

    Erweiterung (siehe kontoauszug-test 2026-08-21): Ist eine End-to-End-
    Referenz erkannt worden (Feld "referenz"), wird sie zusätzlich Teil des
    Schlüssels. Sonst würden zwei ECHTE, unterschiedliche Buchungen mit
    zufällig gleichem Datum+Betrag+Empfänger (z.B. zwei separate
    Versicherungsraten am selben Tag) fälschlich als Duplikat verworfen -
    live beobachtet bei CONTINENTALE 330,80€, PayPal 4,15€ und einem
    10.000€-Tagesgeld-Transfer, je zweimal am selben Tag mit identischem
    Betrag/Empfänger, aber unterschiedlicher Referenz.

    Fehlt die Referenz (typisch bei Kartenzahlungen, die keine End-to-End-Ref
    haben) oder ist sie nur der Commerzbank-Platzhalter "NOTPROVIDED" (bei
    Daueraufträgen/SEPA ohne echte Referenz), fällt der Schlüssel auf
    datum+betrag+empfaenger zurück - das bleibt dort der einzige Schutz gegen
    echte Duplikate bei erneutem Upload desselben Auszugs.
    """
    gesehen = set()
    ergebnis = []
    for b in buchungen:
        if not isinstance(b, dict):
            continue
        basis = (
            b.get("datum"),
            round(float(b.get("betrag") or 0.0), 2),
            (b.get("empfaenger") or "").strip().lower(),
        )
        referenz = (b.get("referenz") or "").strip().lower()
        key = basis + (referenz,) if referenz and referenz != "notprovided" else basis
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


def _merge_batch(daten: dict, buchungen: list, kreditbuchungen: list, immobilienkaeufe: list,
                  quelle_datei: str = None):
    """
    Ergebnis eines einzelnen Batches (Text oder Vision) in die Gesamtlisten mergen.
    quelle_datei (falls gesetzt) wird als internes Feld "_quelle_datei" an jede
    Buchung angehängt - wird von _saldo_check() gebraucht, um Buchungen ihrer
    Quelldatei zuzuordnen, und vor der Rückgabe aus analyze_kontoauszuege()
    wieder entfernt (kein Teil der öffentlichen Buchungs-Struktur).
    """
    neue_buchungen = daten.get("buchungen") or []
    if quelle_datei:
        for b in neue_buchungen:
            if isinstance(b, dict):
                b["_quelle_datei"] = quelle_datei
    buchungen.extend(neue_buchungen)
    kreditbuchungen.extend(daten.get("kreditbuchungen") or [])
    imm = daten.get("immobilienkauf")
    if isinstance(imm, dict) and (imm.get("gesamtbetrag") or imm.get("einzelzahlungen")):
        immobilienkaeufe.append(imm)


# Erkennt "Alter/Neuer Kontostand vom TT.MM.JJJJ <Betrag>" - bisher nur für
# das Commerzbank-Format getestet. Deterministisch per Regex statt per LLM,
# da Saldi exakte Zahlen sind und nicht der Unschärfe der LLM-Extraktion
# unterliegen sollen (siehe _saldo_check).
_KONTOSTAND_RE = re.compile(r"(Alter|Neuer) Kontostand vom \d{2}\.\d{2}\.\d{4}\s+([\d.]+,\d{2})(-?)")


def _kontostaende(text: str) -> dict:
    """
    Extrahiert Alter/Neuer Kontostand aus dem vollen (ungekürzten) PDF-Text.
    Liefert ein dict mit "alt"/"neu" - fehlt einer oder beide (Format nicht
    erkannt), fehlt der entsprechende Schlüssel; der Aufrufer überspringt den
    Konsistenz-Check für diese Datei dann einfach (degraded mode statt Fehler).
    """
    ergebnis = {}
    for label, betrag_s, minus in _KONTOSTAND_RE.findall(text):
        betrag = float(betrag_s.replace(".", "").replace(",", "."))
        if minus == "-":
            betrag = -betrag
        ergebnis["alt" if label == "Alter" else "neu"] = betrag
    return ergebnis


def _saldo_check(buchungen: list, kontostaende_je_datei: dict) -> list:
    """
    Nachgelagerter Konsistenz-Check (siehe kontoauszug-test 2026-08-21): Alter
    Kontostand + Einnahmen - Ausgaben muss (bis auf Rundung) dem Neuen
    Kontostand entsprechen. Weicht es ab, wurden vermutlich Buchungen nicht
    erkannt (z.B. eine LLM-Extraktionslücke bei mehreren optisch identischen
    Zeilen über einen Seitenumbruch hinweg) - KEINE automatische Korrektur,
    nur eine sichtbare Warnung im Rückgabewert, damit sowas künftig auffällt
    statt stillschweigend zu fehlen.

    Nur für Dateien möglich, für die _kontostaende() beide Werte gefunden hat
    (kontostaende_je_datei) UND die über den Text-Batch-Pfad liefen (nur dort
    ist "_quelle_datei" gesetzt, siehe _merge_batch) - für Vision-PDFs oder
    unbekannte Kontostand-Formate wird der Check für diese Datei übersprungen.
    """
    je_datei = {}
    for b in buchungen:
        dateiname = b.get("_quelle_datei")
        if dateiname:
            je_datei.setdefault(dateiname, []).append(b)

    warnungen = []
    for dateiname, saldi in kontostaende_je_datei.items():
        if "alt" not in saldi or "neu" not in saldi:
            continue
        posten = je_datei.get(dateiname, [])
        einnahmen = sum(float(b.get("betrag") or 0.0) for b in posten if b.get("typ") == "einnahme")
        ausgaben = sum(float(b.get("betrag") or 0.0) for b in posten if b.get("typ") == "ausgabe")
        erwartet = round(saldi["alt"] + einnahmen - ausgaben, 2)
        abweichung = round(erwartet - saldi["neu"], 2)
        if abs(abweichung) > 0.01:
            warnungen.append({
                "dateiname": dateiname,
                "alter_kontostand": saldi["alt"],
                "neuer_kontostand_pdf": saldi["neu"],
                "neuer_kontostand_berechnet": erwartet,
                "abweichung": abweichung,
                "hinweis": ("Kontoauszug-Summen weichen von den erkannten Buchungen ab - "
                            "evtl. wurden Buchungen nicht erkannt."),
            })
    return warnungen


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
         _ist_lesbar), wird er in Chunks à max. MAX_ZEICHEN_PRO_PDF Zeichen
         aufgeteilt (_chunk_text, Grenzen an Buchungsblöcken statt mitten in
         einer Buchung) und in Batches von je BATCH_SIZE Chunks verarbeitet
         (ein Claude-Call pro Batch). Ein langes PDF kann so mehrere Batches
         belegen statt wie früher nach den ersten 8000 Zeichen abgeschnitten
         zu werden.
      3. Ist der Text unlesbar/zu kurz (z.B. Sparda-Bank Monospace-Salat), wird
         automatisch auf Vision umgeschaltet: PDF → PNG-Seiten (pdf2image/poppler)
         → Claude Vision (analyze_with_vision), ein Call pro PDF.

    Buchungen werden über alles hinweg anhand von datum+betrag+empfaenger(+referenz,
    siehe _dedupe) dedupliziert. Bei API-Ausfall (siehe llm_analyst._ask) oder
    fehlendem Key: degraded mode, bereits erkannte Batches bleiben erhalten,
    "verfuegbar": False zeigt dem Aufrufer an, dass KEIN Batch ausgewertet
    werden konnte.

    Zusätzlich (siehe _saldo_check): wenn eine Datei einen erkennbaren Alter/
    Neuer-Kontostand ausweist, wird die Summe der erkannten Buchungen dagegen
    geprüft; Abweichungen landen im Rückgabefeld "saldo_warnungen" (leer,
    wenn kein Kontostand gefunden wurde oder alles aufgeht).
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY fehlt – Kontoauszug-Analyse übersprungen (degraded mode)")
        return {"verfuegbar": False, "buchungen": [], "kreditbuchungen": [],
                "kreditanalyse": _kreditanalyse_berechnen([]), "immobilienkaeufe": [],
                "saldo_warnungen": []}

    # 1) Text-Extraktion, Kontostand-Erkennung, Chunking und Entscheidung
    # Text vs. Vision je PDF. text_batches: Liste von (dateiname, batch),
    # ein Batch NIE aus mehreren PDFs gemischt (_saldo_check muss Buchungen
    # eindeutig ihrer Quelldatei zuordnen können).
    text_batches = []
    kontostaende_je_datei = {}  # dateiname -> {"alt": float, "neu": float}
    vision_pdfs = []    # (dateiname, pdf_bytes)
    for dateiname, pdf_bytes in pdf_files:
        text = _extract_text(pdf_bytes)
        if _ist_lesbar(text):
            saldi = _kontostaende(text)
            if saldi:
                kontostaende_je_datei[dateiname] = saldi
            chunks = _chunk_text(text, MAX_ZEICHEN_PRO_PDF)
            labeled = [
                (dateiname if len(chunks) == 1 else f"{dateiname} (Teil {i}/{len(chunks)})", chunk)
                for i, chunk in enumerate(chunks, start=1)
            ]
            for i in range(0, len(labeled), BATCH_SIZE):
                text_batches.append((dateiname, labeled[i:i + BATCH_SIZE]))
        else:
            print(f"ℹ️  '{dateiname}': Text unlesbar/zu kurz – Vision-Analyse")
            vision_pdfs.append((dateiname, pdf_bytes))

    anzahl_schritte = len(text_batches) + len(vision_pdfs)

    alle_buchungen = []
    alle_kreditbuchungen = []
    immobilienkaeufe = []
    mind_ein_batch_erfolgreich = False
    schritt = 0

    # 2) Text-Batches (klassischer Weg)
    for dateiname, batch in text_batches:
        schritt += 1
        if progress_callback:
            progress_callback(schritt, anzahl_schritte)

        texte = [f"--- {dn} ---\n{t}" for dn, t in batch]
        user_content = "Kontoauszüge:\n\n" + "\n\n".join(texte)
        # max_tokens=8192 statt 4096: bei dichten Kontoauszügen kann allein ein
        # Chunk (siehe MAX_ZEICHEN_PRO_PDF) schon 15-20 Buchungen enthalten, ein
        # 2er-Batch entsprechend 30-40 - deren JSON sonst mitten im String
        # abgeschnitten wird (siehe kontoauszug-test 2026-08-21: "Unterminated string").
        antwort = _ask(user_content, system=KONTOAUSZUG_SYSTEM_PROMPT, max_tokens=8192)
        if antwort is None:
            continue
        daten = _parse_batch_antwort(antwort)
        if not daten:
            continue
        mind_ein_batch_erfolgreich = True
        _merge_batch(daten, alle_buchungen, alle_kreditbuchungen, immobilienkaeufe,
                     quelle_datei=dateiname)

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
        _merge_batch(daten, alle_buchungen, alle_kreditbuchungen, immobilienkaeufe,
                     quelle_datei=dateiname)

    alle_buchungen = _dedupe(alle_buchungen)
    alle_kreditbuchungen = _dedupe(alle_kreditbuchungen)
    immobilienkaeufe = _dedupe_immobilienkaeufe(immobilienkaeufe)

    saldo_warnungen = _saldo_check(alle_buchungen, kontostaende_je_datei)
    for b in alle_buchungen:
        b.pop("_quelle_datei", None)

    return {
        "verfuegbar": mind_ein_batch_erfolgreich,
        "buchungen": alle_buchungen,
        "kreditbuchungen": alle_kreditbuchungen,
        "kreditanalyse": _kreditanalyse_berechnen(alle_kreditbuchungen),
        "immobilienkaeufe": immobilienkaeufe,
        "saldo_warnungen": saldo_warnungen,
    }


# ─────────────────────────────────────────────
# CSV-KONTOAUSZÜGE (Fix 2 – Comdirect/DKB/ING direkt parsen, ohne Vision)
# ─────────────────────────────────────────────
# CSV-Exporte sind bereits strukturierter Text – ein Vision-/LLM-Call ist hier
# unnötig (Kosten/Latenz) und würde bei großen Umsatzlisten unnötig Tokens
# verbrauchen. Nur für ein wirklich unbekanntes Format (parse_generic_csv)
# greifen wir auf dieselbe Claude-Textanalyse wie bei PDF-Text zurück.

def _parse_datum_iso(raw: str) -> str | None:
    """
    Konvertiert ein deutsches Bank-Datumsformat (meist DD.MM.YYYY) nach ISO
    (YYYY-MM-DD) – save_buchungen() in database.py parst zwingend über
    date.fromisoformat() und würde jede Zeile mit "falschem" Format sonst
    stillschweigend überspringen.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_betrag_de(raw: str) -> float:
    """Deutsches Zahlenformat (1.234,56) -> float. Leer/unparsbar -> 0.0."""
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw.replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def _decode_csv_bytes(content: bytes) -> str | None:
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def parse_csv_kontoauszug(content: bytes) -> dict:
    """
    Erkennt das Bank-Format anhand typischer Spalten/Marker und parst die CSV
    direkt (ohne LLM). Unbekannte Formate laufen über parse_generic_csv (KI-
    gestützte Extraktion wie bei PDF-Text).
    """
    text = _decode_csv_bytes(content)
    if text is None:
        print("⚠️  CSV-Kontoauszug: Encoding nicht erkannt (weder UTF-8 noch Latin-1/CP1252)")
        return {"buchungen": [], "format": "unlesbar"}

    lines = text.splitlines()
    if not lines:
        return {"buchungen": [], "format": "leer"}

    erste_zeile = lines[0]
    if "buchungstag" in erste_zeile.lower():
        return parse_comdirect_csv(text)
    if "Gläubiger-ID" in text or "Mandatsreferenz" in text:
        return parse_dkb_csv(text)
    if "IBAN" in erste_zeile:
        return parse_ing_csv(text)
    return parse_generic_csv(text)


def parse_comdirect_csv(text: str) -> dict:
    """Comdirect-Umsatzübersicht: "Buchungstag";"Valuta";"Vorgang";"Buchungstext";"Umsatz in EUR"."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    buchungen = []
    for row in reader:
        try:
            datum = _parse_datum_iso(row.get("Buchungstag", ""))
            if not datum:
                continue
            betrag = _parse_betrag_de(row.get("Umsatz in EUR", "0"))
            buchungen.append({
                "datum": datum,
                "betrag": betrag,
                "empfaenger": (row.get("Buchungstext") or "").strip()[:100],
                "verwendungszweck": (row.get("Vorgang") or "").strip(),
                "typ": "einnahme" if betrag > 0 else "ausgabe",
            })
        except Exception:
            continue
    return {"buchungen": buchungen, "format": "comdirect_csv"}


def parse_dkb_csv(text: str) -> dict:
    """DKB-Umsatzexport – Spaltennamen variieren je nach Konto-/Exportversion,
    daher mehrere Kandidaten pro Feld."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    buchungen = []
    for row in reader:
        try:
            datum = _parse_datum_iso(row.get("Buchungsdatum") or row.get("Wertstellung") or "")
            if not datum:
                continue
            betrag = _parse_betrag_de(row.get("Betrag (€)") or row.get("Betrag") or "0")
            empfaenger = (
                row.get("Zahlungsempfänger*in") or row.get("Zahlungsempfänger")
                or row.get("Auftraggeber / Begünstigter") or ""
            ).strip()[:100]
            buchungen.append({
                "datum": datum,
                "betrag": betrag,
                "empfaenger": empfaenger,
                "verwendungszweck": (row.get("Verwendungszweck") or "").strip(),
                "typ": "einnahme" if betrag > 0 else "ausgabe",
            })
        except Exception:
            continue
    return {"buchungen": buchungen, "format": "dkb_csv"}


def parse_ing_csv(text: str) -> dict:
    """ING-Umsatzexport – "Buchung";"Valuta";"Auftraggeber/Empfänger";"Buchungstext";"Verwendungszweck";"Saldo";"Betrag";"Währung"."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    buchungen = []
    for row in reader:
        try:
            datum = _parse_datum_iso(row.get("Buchung") or row.get("Valuta") or "")
            if not datum:
                continue
            betrag = _parse_betrag_de(row.get("Betrag") or "0")
            buchungen.append({
                "datum": datum,
                "betrag": betrag,
                "empfaenger": (row.get("Auftraggeber/Empfänger") or "").strip()[:100],
                "verwendungszweck": (row.get("Verwendungszweck") or row.get("Buchungstext") or "").strip(),
                "typ": "einnahme" if betrag > 0 else "ausgabe",
            })
        except Exception:
            continue
    return {"buchungen": buchungen, "format": "ing_csv"}


def parse_generic_csv(text: str) -> dict:
    """Unbekanntes CSV-Format – dieselbe KI-Extraktion wie bei lesbarem PDF-Text
    (siehe KONTOAUSZUG_SYSTEM_PROMPT), da eine CSV-Datei bereits reiner Text ist."""
    antwort = _ask(
        f"Kontoauszug (CSV):\n\n{text[:MAX_ZEICHEN_PRO_PDF]}",
        system=KONTOAUSZUG_SYSTEM_PROMPT, max_tokens=4096,
    )
    if antwort is None:
        return {"buchungen": [], "format": "generic_csv"}
    daten = _parse_batch_antwort(antwort)
    daten["format"] = "generic_csv"
    return daten
