"""
llm_analyst.py – KI-Analyse via Claude API für Portfolio-OS.

Das LLM entscheidet nichts und exekutiert nichts – es analysiert und erklärt.
Bei API-Ausfall läuft das System im degraded mode weiter (siehe _ask()).
"""

import base64
import io
import json

import anthropic

from config import ANTHROPIC_API_KEY, LLM_MODEL, LLM_MAX_TOKENS

KREDIT_VERTRAG_SYSTEM_PROMPT = """Du bist ein Experte für deutsche Immobilienkreditverträge.
Analysiere das Dokument und extrahiere folgende Daten als JSON:
{
    "kredit_gesamtbetrag": 0.0,
    "kredit_abgerufen": 0.0,
    "zinssatz": 0.0,
    "laufzeit_jahre": 0,
    "zinsbindung_bis": "YYYY-MM-DD oder null",
    "monatliche_rate": 0.0,
    "vorfaelligkeitsgebuehr_pct": 0.0,
    "bank": "Name der Bank",
    "darlehensnehmer": "Name(n)",
    "objekt_adresse": "Adresse wenn vorhanden",
    "abschluss_datum": "YYYY-MM-DD oder null",
    "besonderheiten": "z.B. KfW-Förderung, Tilgungsaussetzung etc."
}
Wenn ein Wert nicht im Dokument steht: null setzen.
Antworte NUR mit dem JSON, kein anderer Text."""

PORTFOLIO_SCREENSHOT_SYSTEM_PROMPT = """Du liest Screenshots von Broker-/Banking-Apps
(z.B. Trade Republic, Comdirect, Scalable Capital, ING) aus. Extrahiere alle sichtbaren
Wertpapier-Positionen als JSON-Array:
[
    {"ticker": "z.B. AAPL oder ISIN", "name": "Name des Wertpapiers", "quantity": 0.0, "kaufpreis": 0.0}
]
"kaufpreis" ist der Kaufpreis/Einstandskurs pro Stück falls sichtbar, sonst der aktuelle Kurs.
Wenn ein Wert nicht erkennbar ist: null setzen.
Antworte NUR mit dem JSON-Array, kein anderer Text."""

SYSTEM_PROMPT = """Du bist ein kritischer, unabhängiger Finanzanalyst.
Du gibst keine Anlageberatung sondern zeigst Fakten und Risiken auf.
Alle Empfehlungen sind Informationen, keine Handlungsanweisungen.
Der Nutzer trifft alle Entscheidungen selbst."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _ask(user_content: str, system: str = SYSTEM_PROMPT, max_tokens: int = None, tools: list = None) -> str:
    """
    Zentraler Claude-Aufruf. Gibt bei Erfolg den Antworttext zurück,
    bei jedem Fehler (fehlender Key, API-Ausfall, Timeout, ...) None –
    der Aufrufer bleibt dadurch immer funktionsfähig (nur ohne KI-Text).
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY fehlt – KI-Analyse übersprungen (degraded mode)")
        return None
    try:
        kwargs = dict(
            model=LLM_MODEL,
            max_tokens=max_tokens or LLM_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        if tools:
            kwargs["tools"] = tools
        response = _client().messages.create(**kwargs)
        text_bloecke = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        return "\n".join(text_bloecke).strip() or None
    except Exception as e:
        print(f"⚠️  KI-Analyse fehlgeschlagen: {e} (degraded mode)")
        return None


# ─────────────────────────────────────────────
# PORTFOLIO-ANALYSE
# ─────────────────────────────────────────────

def analyze_portfolio(user_id: int) -> dict:
    """Klumpenrisiko-Analyse und allgemeine Beobachtungen zum Gesamtportfolio."""
    import portfolio as portfolio_module
    import rebalancing as rebalancing_module

    summary = portfolio_module.get_portfolio_summary(user_id)
    positionen = portfolio_module.get_positions(user_id)
    deviations = rebalancing_module.calculate_deviations(user_id)

    top_positionen = sorted(positionen, key=lambda p: -(p["market_value"] or 0))[:10]
    positionen_text = "\n".join(
        f"- {p['ticker']} ({p['asset_class'] or 'unklassifiziert'}): "
        f"{p['market_value']:.2f} {p['currency']} "
        f"({p['market_value'] / summary['gesamtvermoegen'] * 100 if summary['gesamtvermoegen'] else 0:.1f}% des Portfolios)"
        for p in top_positionen
    )
    abweichungen_text = "\n".join(
        f"- {d['asset_class']}: Ist {d['ist_pct']*100:.1f}% / Ziel {d['ziel_pct']*100:.1f}%"
        for d in deviations
    )

    prompt = f"""Analysiere folgendes Portfolio auf Klumpenrisiken (Einzelposition, Assetklasse, Region/Sektor
soweit aus den Tickern erkennbar) und gib 2-4 konkrete, sachliche Beobachtungen.

Gesamtvermögen: {summary['gesamtvermoegen']:.2f} EUR
Unrealisierter Gewinn/Verlust: {summary['unrealized_pnl']:.2f} EUR
Anzahl Positionen: {summary['positions_count']}

Top-Positionen nach Wert:
{positionen_text or '(keine Positionen)'}

Ist/Soll-Abweichungen je Assetklasse:
{abweichungen_text or '(keine Zielgewichtung hinterlegt)'}"""

    text = _ask(prompt)
    return {
        "verfuegbar": text is not None,
        "text": text or "KI-Analyse aktuell nicht verfügbar. Reine Zahlen siehe Dashboard-Tabs Übersicht/Rebalancing.",
        "summary": summary,
    }


def analyze_rebalancing(proposal: dict) -> str:
    """Erzeugt eine sachliche Begründung für einen Rebalancing-Vorschlag (proposal-dict aus rebalancing.py)."""
    prompt = f"""Hier ist ein automatisch erstellter Rebalancing-Vorschlag (Typ: {proposal.get('typ')}).
Erkläre in 3-5 Sätzen sachlich, warum eine Anpassung sinnvoll sein könnte und welche Risiken
bestehen, wenn NICHT rebalanced wird. Keine konkrete Kauf-/Verkaufsempfehlung, nur Einordnung.

Rohdaten (JSON):
{json.dumps(proposal, ensure_ascii=False, default=str, indent=2)}"""

    text = _ask(prompt)
    return text or "KI-Begründung aktuell nicht verfügbar – siehe regelbasierte Begründung."


def estimate_real_estate_value(immobilie: dict) -> dict:
    """
    KI-gestützte Wertschätzung einer Immobilie via Web-Suche (Claude Web-Search-Tool).
    `immobilie` ist ein Dict mit u.a. adresse, wohnflaeche_qm, kaufpreis, kaufjahr.
    Bei fehlendem Tool-Zugriff oder API-Fehler: degraded mode, kein Absturz.
    """
    prompt = f"""Schätze den aktuellen Marktwert folgender Immobilie anhand öffentlich verfügbarer
Informationen zu vergleichbaren Angeboten in der Umgebung (Web-Suche nutzen falls verfügbar).
Gib eine Preisspanne (von-bis) in EUR an, nenne 2-3 Quellen/Vergleichswerte falls gefunden,
und weise explizit darauf hin, dass es sich um eine grobe Schätzung ohne Vor-Ort-Besichtigung handelt.

Adresse: {immobilie.get('adresse')}
Wohnfläche: {immobilie.get('wohnflaeche_qm')} qm
Kaufpreis: {immobilie.get('kaufpreis')} EUR ({immobilie.get('kaufjahr')})
Letzter bekannter Schätzwert: {immobilie.get('letzter_schaetzwert')} EUR"""

    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    text = _ask(prompt, max_tokens=1536, tools=tools)

    if text is None:
        # Fallback ohne Web-Suche versuchen (z.B. falls das Tool nicht verfügbar ist)
        text = _ask(prompt, max_tokens=1024)

    return {
        "verfuegbar": text is not None,
        "text": text or "KI-Schätzung aktuell nicht verfügbar. Bitte letzten Schätzwert manuell aktualisieren.",
    }


def analyze_kredit_vertrag(file_bytes: bytes, file_type: str) -> dict:
    """
    Liest einen hochgeladenen Immobilienkredit-Vertrag (PDF oder Bild) per KI aus
    und gibt die erkannten Eckdaten als dict zurück (siehe KREDIT_VERTRAG_SYSTEM_PROMPT
    für die Struktur). Bei fehlendem API-Key, nicht lesbarem Dokument oder ungültiger
    KI-Antwort: degraded mode, gibt ein leeres dict zurück statt abzustürzen.
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY fehlt – Kreditvertrags-Analyse übersprungen (degraded mode)")
        return {}

    try:
        if file_type == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            if not text.strip():
                print("⚠️  Kein Text im PDF gefunden (degraded mode)")
                return {}
            user_content = f"Vertragstext:\n\n{text[:15000]}"
        else:
            media_type = file_type if file_type in ("image/jpeg", "image/png") else "image/jpeg"
            b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Extrahiere die Kreditvertrags-Daten aus diesem Bild als JSON."},
            ]

        antwort = _ask(user_content, system=KREDIT_VERTRAG_SYSTEM_PROMPT, max_tokens=1024)
        if antwort is None:
            return {}

        bereinigt = antwort.strip()
        if bereinigt.startswith("```"):
            bereinigt = bereinigt.strip("`")
            if bereinigt.lower().startswith("json"):
                bereinigt = bereinigt[4:]
        return json.loads(bereinigt.strip())
    except Exception as e:
        print(f"⚠️  Kreditvertrags-Analyse fehlgeschlagen: {e} (degraded mode)")
        return {}


def analyze_portfolio_screenshot(file_bytes: bytes, file_type: str) -> list:
    """
    Liest einen Screenshot einer Broker-/Banking-App per KI aus und gibt die erkannten
    Positionen als Liste von dicts zurück (ticker, name, quantity, kaufpreis). Bei
    fehlendem API-Key, nicht lesbarem Bild oder ungültiger KI-Antwort: degraded mode,
    gibt eine leere Liste zurück statt abzustürzen.
    """
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY fehlt – Screenshot-Analyse übersprungen (degraded mode)")
        return []

    try:
        media_type = file_type if file_type in ("image/jpeg", "image/png") else "image/jpeg"
        b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": "Extrahiere alle Positionen aus diesem Portfolio-Screenshot als JSON-Array."},
        ]

        antwort = _ask(user_content, system=PORTFOLIO_SCREENSHOT_SYSTEM_PROMPT, max_tokens=1536)
        if antwort is None:
            return []

        bereinigt = antwort.strip()
        if bereinigt.startswith("```"):
            bereinigt = bereinigt.strip("`")
            if bereinigt.lower().startswith("json"):
                bereinigt = bereinigt[4:]
        ergebnis = json.loads(bereinigt.strip())
        return ergebnis if isinstance(ergebnis, list) else []
    except Exception as e:
        print(f"⚠️  Screenshot-Analyse fehlgeschlagen: {e} (degraded mode)")
        return []


def generate_quarterly_report(user_id: int) -> dict:
    """Vollständiger Quartalsbericht: Performance, Steuerstand, Rebalancing, KI-Einordnung."""
    import portfolio as portfolio_module
    import tax_engine
    import rebalancing as rebalancing_module

    summary = portfolio_module.get_portfolio_summary(user_id)
    performance = portfolio_module.get_performance(user_id, "3M")
    deviations = rebalancing_module.calculate_deviations(user_id)
    freistellung_rest = tax_engine.get_remaining_freistellung(user_id)
    loss_harvesting = tax_engine.find_tax_loss_harvesting(user_id)

    prompt = f"""Erstelle einen kompakten Quartalsbericht (auf Deutsch, ca. 200-300 Wörter) für ein
privates Portfolio. Struktur: 1) Kurzfazit, 2) Performance-Einordnung, 3) Auffälligkeiten bei der
Allokation, 4) Steuerliche Hinweise. Sachlich, keine Anlageberatung.

Gesamtvermögen: {summary['gesamtvermoegen']:.2f} EUR
Performance letztes Quartal: {performance['pnl_pct']:.2f}% (TWR: {performance['twr_pct']:.2f}%, MDD: {performance['mdd_pct']:.2f}%)
Abweichungen (Top 5): {json.dumps(deviations[:5], ensure_ascii=False, default=str)}
Verbleibender Freistellungsauftrag: {freistellung_rest:.2f} EUR
Tax-Loss-Harvesting-Kandidaten: {len(loss_harvesting)}"""

    text = _ask(prompt, max_tokens=1536)
    return {
        "verfuegbar": text is not None,
        "text": text or "KI-Quartalsbericht aktuell nicht verfügbar – Rohdaten siehe unten.",
        "summary": summary,
        "performance": performance,
        "deviations": deviations,
        "freistellung_rest": freistellung_rest,
        "loss_harvesting": loss_harvesting,
    }


def answer_portfolio_question(user_id: int, frage: str) -> str:
    """Chat mit der Portfolio-KI – beantwortet Fragen auf Basis der aktuellen Portfoliodaten."""
    import portfolio as portfolio_module
    import rebalancing as rebalancing_module

    summary = portfolio_module.get_portfolio_summary(user_id)
    positionen = portfolio_module.get_positions(user_id)
    deviations = rebalancing_module.calculate_deviations(user_id)

    kontext = f"""Portfoliokontext für die folgende Frage:
Gesamtvermögen: {summary['gesamtvermoegen']:.2f} EUR
Assetklassen-Verteilung: {json.dumps(summary['asset_breakdown'], ensure_ascii=False)}
Anzahl Positionen: {len(positionen)}
Abweichungen von Zielgewichtung: {json.dumps([{k: v for k, v in d.items() if k != 'asset_class_id'} for d in deviations], ensure_ascii=False, default=str)}

Frage des Nutzers: {frage}"""

    text = _ask(kontext, max_tokens=1024)
    return text or "KI-Antwort aktuell nicht verfügbar. Bitte später erneut versuchen."
