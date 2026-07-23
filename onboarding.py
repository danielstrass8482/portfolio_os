"""
onboarding.py – Onboarding-Wizard für neue Nutzer.

8 Schritte: Profil, Risikoprofil, Ziele, Sparplan-Rechner, Assetklassen,
Zielgewichtung, Positions-Import, Fertig. Wird von dashboard.py aufgerufen
solange pos_users.onboarding_completed = False ist (siehe show_onboarding()).
Navigation über st.session_state["onboarding_step"] (1-8).
"""

import tempfile
from datetime import date

import streamlit as st

from database import (
    get_session, get_asset_class_by_slug,
    PosUser, PosPortfolio, PosGoal, PosInvestmentPreference, PosTargetWeight,
)
import portfolio as portfolio_module
import llm_analyst


def _fmt_eur(wert, nachkommastellen=0):
    if wert is None:
        return "–"
    return f"{wert:,.{nachkommastellen}f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def _eur_input(label, value, key, step=1000.0, container=None, help=None):
    """Einheitliches Eingabefeld für Geldbeträge – siehe dashboard._eur_input (gleiches Muster,
    hier dupliziert statt importiert, um keinen Zyklus onboarding.py <-> dashboard.py zu erzeugen)."""
    return (container or st).number_input(
        label, min_value=0.0, step=step, value=float(value or 0.0), key=key,
        placeholder="z.B. 100.000",
        help=help or "Eingabe in Euro, z.B. 500000 für 500.000 €",
    )


# ─────────────────────────────────────────────
# STAMMDATEN
# ─────────────────────────────────────────────

STEP_LABELS = ["Profil", "Risiko", "Ziele", "Rechner", "Assetklassen", "Gewichtung", "Import", "Fertig"]

RISIKO_FRAGEN = [
    ("Was machst du wenn dein Portfolio 20% fällt?", [
        "Ich verkaufe um weitere Verluste zu vermeiden",
        "Ich warte ab und hoffe auf Erholung",
        "Ich kaufe nach – günstige Gelegenheit!",
    ]),
    ("Wann brauchst du das Geld frühestens?", [
        "In weniger als 3 Jahren",
        "In 3 bis 10 Jahren",
        "In mehr als 10 Jahren",
    ]),
    ("Hast du ein finanzielles Polster für Notfälle?", [
        "Nein, ich lebe von Monat zu Monat",
        "Ja, etwa 3 Monatsgehälter",
        "Ja, mehr als 6 Monatsgehälter",
    ]),
    ("Wie viel Erfahrung hast du mit Geldanlage?", [
        "Kaum Erfahrung – ich fange gerade an",
        "Etwas Erfahrung – ich kenne die Basics",
        "Erfahren – ich kenne Aktien, ETFs, Risiken gut",
    ]),
    ("Was ist dir wichtiger?", [
        "Sicherheit – ich akzeptiere weniger Rendite",
        "Balance zwischen Sicherheit und Rendite",
        "Maximale Rendite – ich akzeptiere höhere Schwankungen",
    ]),
]

RISIKO_PROFILE = {
    "konservativ": {"label": "Konservativ", "rendite": 0.04},
    "ausgewogen":  {"label": "Ausgewogen",  "rendite": 0.06},
    "wachstum":    {"label": "Wachstum",    "rendite": 0.08},
    "aggressiv":   {"label": "Aggressiv",   "rendite": 0.10},
}

RISIKO_BESCHREIBUNG = {
    "konservativ": (
        "Du bist ein konservativer Anleger 🛡️\n\n"
        "Sicherheit steht für dich im Vordergrund – Kapitalerhalt vor Rendite.\n\n"
        "Empfehlung: Fokus auf Anleihen, Tagesgeld und breit gestreute ETFs."
    ),
    "ausgewogen": (
        "Du bist ein ausgewogener Anleger 📊\n\n"
        "Du willst dein Vermögen wachsen lassen, aber nicht nachts schlecht schlafen.\n\n"
        "Empfehlung: Mix aus ETFs, etwas Einzelaktien und einem Sicherheitspuffer."
    ),
    "wachstum": (
        "Du bist ein wachstumsorientierter Anleger 🚀\n\n"
        "Du akzeptierst spürbare Schwankungen für höhere langfristige Rendite.\n\n"
        "Empfehlung: Schwerpunkt auf ETFs und Einzelaktien, wenig Cash-Reserve."
    ),
    "aggressiv": (
        "Du bist ein aggressiver Anleger 🔥\n\n"
        "Maximale Rendite ist dein Ziel, Schwankungen stören dich kaum.\n\n"
        "Empfehlung: Hoher Aktien-/ETF-Anteil, Raum für spekulative Beimischungen."
    ),
}

ZIEL_VORLAGEN = [
    ("🏖️ Altersvorsorge", "rente", "Altersvorsorge"),
    ("🏠 Immobilie", "immobilie", "Immobilie"),
    ("🎓 Kinderstudium", "studium", "Kinderstudium"),
    ("✈️ Sabbatical", "sonstiges", "Sabbatical"),
]
ZIEL_ICONS = {"rente": "🏖️", "immobilie": "🏠", "studium": "🎓", "sonstiges": "✈️"}

ASSETKLASSEN_OPTIONEN = [
    ("etf", "📈 ETFs", "Breit diversifiziert, kostengünstig", True),
    ("stocks", "🏢 Einzelaktien", "Gezielt in Unternehmen investieren", True),
    ("anleihen", "📋 Anleihen", "Festverzinslich, konservativ", False),
    ("gold", "🥇 Gold/Rohstoffe", "Inflationsschutz", True),
    ("tagesgeld", "💵 Tagesgeld/Geldmarkt", "Liquiditätsreserve", True),
    ("krypto", "₿ Krypto", "Hochspekulativ", False),
    ("immobilie", "🏠 Immobilie", "Sachwert", True),
]
ASSETKLASSEN_LABELS = {k: label for k, label, *_ in ASSETKLASSEN_OPTIONEN}

# Mapping onboarding-Assetklassen-Key → Slug in pos_asset_classes (siehe config.DEFAULT_ASSET_CLASSES)
ASSETKLASSE_SLUG_MAP = {
    "etf": "etf", "stocks": "einzelaktie", "anleihen": "anleihe", "gold": "gold-rohstoff",
    "tagesgeld": "tagesgeld", "krypto": "krypto", "immobilie": "immobilie",
}
SLUG_ZU_ASSETKLASSE = {v: k for k, v in ASSETKLASSE_SLUG_MAP.items()}

BLACKLIST_OPTIONEN = [
    ("waffen", "Waffen/Rüstung"), ("tabak", "Tabak/Alkohol"), ("fossil", "Fossil/Öl"),
    ("pharma", "Pharma"), ("gluecksspiel", "Glücksspiel"), ("krypto_unternehmen", "Krypto-Unternehmen"),
]

GEWICHTUNG_VORSCHLAEGE = {
    "konservativ": {"etf": 40, "anleihen": 30, "tagesgeld": 20, "gold": 10},
    "ausgewogen":  {"etf": 55, "stocks": 15, "tagesgeld": 10, "gold": 5, "immobilie": 15},
    "wachstum":    {"etf": 60, "stocks": 25, "gold": 5, "tagesgeld": 10},
    "aggressiv":   {"etf": 50, "stocks": 40, "krypto": 5, "gold": 5},
}


def ensure_100(weights: dict) -> dict:
    """
    Normalisiert eine Gewichtung so, dass sie exakt 100 ergibt – gleicht sowohl
    Rundungsfehler als auch echte Abweichungen aus (z.B. KI-Antwort, die trotz
    Anweisung nicht exakt 100 ergibt, oder ein statischer Vorschlag, dessen Summe
    nach dem Herausfiltern nicht gewählter Assetklassen nicht mehr 100 ergibt).
    Restdifferenz nach dem Runden wandert auf die letzte Assetklasse.
    """
    total = sum(weights.values())
    if total == 0:
        return weights
    normalized = {k: round(v / total * 100, 1) for k, v in weights.items()}
    diff = 100 - sum(normalized.values())
    if diff != 0:
        last_key = list(normalized.keys())[-1]
        normalized[last_key] = round(normalized[last_key] + diff, 1)
    return normalized


def _risikoprofil_von_score(score: int) -> str:
    if score <= 2:
        return "konservativ"
    if score <= 5:
        return "ausgewogen"
    if score <= 8:
        return "wachstum"
    return "aggressiv"


def projiziertes_kapital(sparrate_monat: float, rendite_jahr: float, jahre: int, startkapital: float = 0.0) -> float:
    """Zinseszins-Hochrechnung: monatliche Sparrate + einmaliges Startkapital über `jahre` Jahre."""
    monate = jahre * 12
    monatsrendite = rendite_jahr / 12
    if monatsrendite == 0:
        einzahlungen_wert = sparrate_monat * monate
    else:
        einzahlungen_wert = sparrate_monat * (((1 + monatsrendite) ** monate - 1) / monatsrendite)
    start_wert = startkapital * (1 + monatsrendite) ** monate
    return einzahlungen_wert + start_wert


def benoetigte_sparrate(zielbetrag: float, rendite_jahr: float, jahre: int) -> float:
    """Inverse der Zinseszins-Formel: welche monatliche Sparrate wird für `zielbetrag` benötigt?"""
    monate = jahre * 12
    if monate <= 0:
        return zielbetrag
    r = rendite_jahr / 12
    if r == 0:
        return zielbetrag / monate
    faktor = ((1 + r) ** monate - 1) / r
    return zielbetrag / faktor if faktor else zielbetrag / monate


def _stelle_default_portfolio_sicher(user_id: int) -> int:
    """Gibt die id eines (beliebigen) Portfolios des Nutzers zurück – legt bei Bedarf ein Standarddepot an."""
    with get_session() as session:
        pf = session.query(PosPortfolio).filter_by(user_id=user_id).first()
        if pf:
            return pf.id
        neues_pf = PosPortfolio(user_id=user_id, name="Hauptdepot", typ="depot")
        session.add(neues_pf)
        session.flush()
        return neues_pf.id


# ─────────────────────────────────────────────
# FORTSCHRITTSBALKEN
# ─────────────────────────────────────────────

def _progress_bar(step: int):
    cols = st.columns(len(STEP_LABELS))
    for i, label in enumerate(STEP_LABELS, start=1):
        marker = "●" if i <= step else "○"
        text = f"**{marker} {label}**" if i == step else f"{marker} {label}"
        cols[i - 1].markdown(f"<div style='text-align:center; font-size:0.82rem;'>{text}</div>",
                              unsafe_allow_html=True)
    st.progress(step / len(STEP_LABELS))
    st.divider()


# ─────────────────────────────────────────────
# SCHRITT 1 – PERSÖNLICHES PROFIL
# ─────────────────────────────────────────────

def _step_profil(user_id: int):
    st.header("Lass uns dich kennenlernen 👋")
    st.caption("Diese Informationen helfen uns, personalisierte Empfehlungen zu erstellen.")

    with get_session() as session:
        user = session.get(PosUser, user_id)
        vorname_default = user.name
        alter_default = user.alter_jahre or 30
        familienstand_default = user.familienstand or "Ledig"
        sparrate_default = user.monatliche_sparrate or 0.0
        horizont_default = user.anlagehorizont_jahre or 20

    familienstand_optionen = ["Ledig", "Verheiratet", "Mit Partner", "Geschieden", "Verwitwet"]

    vorname = st.text_input("Vorname", value=vorname_default, key="ob_vorname")
    alter = st.number_input("Alter (Jahre)", min_value=16, max_value=110, value=int(alter_default), key="ob_alter")
    familienstand = st.selectbox(
        "Familienstand", familienstand_optionen,
        index=familienstand_optionen.index(familienstand_default) if familienstand_default in familienstand_optionen else 0,
        key="ob_familienstand")
    sparrate = _eur_input(
        "Monatlich zum Investieren verfügbar (€)", sparrate_default, "ob_sparrate", step=100.0,
        help="Eingabe in Euro, z.B. 500 für 500 €")
    horizont = st.slider("Anlagehorizont (Jahre)", 1, 50, int(horizont_default), key="ob_horizont")

    if st.button("Weiter →", key="ob_step1_weiter", type="primary"):
        if not vorname:
            st.error("Bitte einen Vornamen angeben.")
        else:
            with get_session() as session:
                user = session.get(PosUser, user_id)
                user.name = vorname
                user.alter_jahre = int(alter)
                user.familienstand = familienstand
                user.monatliche_sparrate = sparrate
                user.anlagehorizont_jahre = int(horizont)
            st.session_state.onboarding_step = 2
            st.rerun()


# ─────────────────────────────────────────────
# SCHRITT 2 – RISIKOPROFIL
# ─────────────────────────────────────────────

def _step_risiko(user_id: int):
    st.header("Dein Risikoprofil 🎯")
    st.caption("5 kurze Fragen – kein Richtig oder Falsch.")

    antworten = []
    for i, (frage, optionen) in enumerate(RISIKO_FRAGEN):
        st.markdown(f"**Frage {i + 1}: {frage}**")
        wahl = st.radio(frage, optionen, key=f"ob_risiko_f{i}", label_visibility="collapsed")
        antworten.append(optionen.index(wahl))
        st.write("")

    score = sum(antworten)
    profil_key = _risikoprofil_von_score(score)
    profil = RISIKO_PROFILE[profil_key]

    st.divider()
    st.subheader(f"Score: {score}/10")
    st.info(RISIKO_BESCHREIBUNG[profil_key])
    st.caption(f"Erwartete Rendite (Annahme für Hochrechnungen): {profil['rendite'] * 100:.0f}% p.a.")

    col_zurueck, col_weiter = st.columns(2)
    if col_zurueck.button("← Zurück", key="ob_step2_zurueck"):
        st.session_state.onboarding_step = 1
        st.rerun()
    if col_weiter.button("Weiter →", key="ob_step2_weiter", type="primary"):
        with get_session() as session:
            user = session.get(PosUser, user_id)
            user.risikoscore = score
            user.risikoprofil = profil_key
        st.session_state.onboarding_step = 3
        st.rerun()


# ─────────────────────────────────────────────
# SCHRITT 3 – ZIELE DEFINIEREN
# ─────────────────────────────────────────────

def _step_ziele(user_id: int):
    st.header("Deine Ziele 🏆")
    st.caption("Was möchtest du mit deinen Investments erreichen?")

    with get_session() as session:
        vorhandene_ziele = [
            {"id": g.id, "name": g.name, "typ": g.typ, "zielbetrag": g.zielbetrag,
             "zeitraum_jahre": g.zeitraum_jahre, "prioritaet": g.prioritaet}
            for g in session.query(PosGoal).filter_by(user_id=user_id).order_by(PosGoal.id).all()
        ]

    st.markdown("**Vorlage wählen oder eigenes Ziel anlegen:**")
    vc = st.columns(len(ZIEL_VORLAGEN) + 1)
    for i, (label, typ, name) in enumerate(ZIEL_VORLAGEN):
        if vc[i].button(label, key=f"ob_ziel_vorlage_{typ}"):
            st.session_state["ob_neues_ziel_name"] = name
            st.session_state["ob_neues_ziel_typ"] = typ
    if vc[-1].button("+ Eigenes Ziel", key="ob_ziel_vorlage_eigen"):
        st.session_state["ob_neues_ziel_name"] = ""
        st.session_state["ob_neues_ziel_typ"] = "sonstiges"

    if "ob_neues_ziel_typ" in st.session_state:
        with st.form("ob_neues_ziel_form", clear_on_submit=True):
            st.markdown(f"**Neues Ziel** {ZIEL_ICONS.get(st.session_state['ob_neues_ziel_typ'], '🎯')}")
            zname = st.text_input("Name des Ziels", value=st.session_state.get("ob_neues_ziel_name", ""))
            zbetrag = _eur_input("Zielbetrag (€)", 100000.0, "ob_ziel_zbetrag", step=1000.0)
            zjahre = st.number_input("In wie vielen Jahren?", min_value=1, max_value=60, value=20)
            zprio = st.radio("Priorität", ["Haupt", "Neben"], horizontal=True)
            if st.form_submit_button("Ziel hinzufügen"):
                if not zname:
                    st.error("Bitte einen Namen angeben.")
                else:
                    with get_session() as session:
                        session.add(PosGoal(
                            user_id=user_id, name=zname,
                            typ=st.session_state.get("ob_neues_ziel_typ", "sonstiges"),
                            zielbetrag=zbetrag, zeitraum_jahre=int(zjahre),
                            prioritaet="haupt" if zprio == "Haupt" else "neben",
                        ))
                    del st.session_state["ob_neues_ziel_typ"]
                    st.session_state.pop("ob_neues_ziel_name", None)
                    st.rerun()

    st.divider()
    if vorhandene_ziele:
        st.markdown("**Deine Ziele:**")
        for z in vorhandene_ziele:
            with st.container(border=True):
                cz1, cz2 = st.columns([5, 1])
                icon = ZIEL_ICONS.get(z["typ"], "🎯")
                cz1.markdown(
                    f"{icon} **{z['name']}** ({z['prioritaet']}) – Ziel: {_fmt_eur(z['zielbetrag'])} "
                    f"in {z['zeitraum_jahre']} Jahren"
                )
                if cz2.button("🗑️", key=f"ob_ziel_del_{z['id']}"):
                    with get_session() as session:
                        session.query(PosGoal).filter_by(id=z["id"]).delete()
                    st.rerun()
    else:
        st.info("Noch kein Ziel angelegt. Bitte mindestens ein Ziel hinzufügen, um fortzufahren.")

    col_zurueck, col_weiter = st.columns(2)
    if col_zurueck.button("← Zurück", key="ob_step3_zurueck"):
        st.session_state.onboarding_step = 2
        st.rerun()
    if col_weiter.button("Weiter →", key="ob_step3_weiter", type="primary"):
        if not vorhandene_ziele:
            st.error("Bitte mindestens ein Ziel anlegen.")
        else:
            st.session_state.onboarding_step = 4
            st.rerun()


# ─────────────────────────────────────────────
# SCHRITT 4 – SPARPLAN-RECHNER
# ─────────────────────────────────────────────

def _step_rechner(user_id: int):
    st.header("Erreichst du deine Ziele? 📈")
    st.caption("Teile deine monatliche Sparrate auf deine Ziele auf – die Hochrechnung "
               "nutzt je Ziel NUR den ihm zugewiesenen Anteil.")

    with get_session() as session:
        user = session.get(PosUser, user_id)
        basis_sparrate = user.monatliche_sparrate or 0.0
        basis_rendite = RISIKO_PROFILE.get(user.risikoprofil, RISIKO_PROFILE["ausgewogen"])["rendite"]
        ziele = [
            {"id": g.id, "name": g.name, "typ": g.typ, "zielbetrag": g.zielbetrag,
             "zeitraum_jahre": g.zeitraum_jahre, "erwartete_rendite": g.erwartete_rendite,
             "sparrate_anteil_pct": g.sparrate_anteil_pct}
            for g in session.query(PosGoal).filter_by(user_id=user_id).order_by(PosGoal.id).all()
        ]

    st.markdown(f"**Verfügbare monatliche Sparrate: {_fmt_eur(basis_sparrate)}**")

    default_anteil = round(100 / len(ziele)) if ziele else 0

    aktualisierte_renditen = {}
    aktualisierte_anteile = {}
    summe_anteil = 0.0

    for z in ziele:
        icon = ZIEL_ICONS.get(z["typ"], "🎯")
        anteil_key = f"ob_rechner_anteil_{z['id']}"
        rendite_key = f"ob_rechner_rendite_{z['id']}"

        anteil_default = z["sparrate_anteil_pct"] if z["sparrate_anteil_pct"] is not None else default_anteil
        anteil_pct = st.session_state.get(anteil_key, anteil_default)
        rendite_pct = st.session_state.get(rendite_key, round((z["erwartete_rendite"] or basis_rendite) * 100, 1))

        sparrate_ziel = basis_sparrate * anteil_pct / 100
        endkapital = projiziertes_kapital(sparrate_ziel, rendite_pct / 100, z["zeitraum_jahre"])
        eingezahlt = sparrate_ziel * z["zeitraum_jahre"] * 12
        rendite_anteil = endkapital - eingezahlt

        with st.container(border=True):
            st.markdown(f"**{icon} {z['name']} – Ziel: {_fmt_eur(z['zielbetrag'])}**")
            st.write(f"Bei {anteil_pct:.0f}% deiner Sparrate ({_fmt_eur(sparrate_ziel)}/Monat) und {rendite_pct:.1f}% p.a.:")

            rc1, rc2, rc3 = st.columns(3)
            rc1.metric("Erreichtes Kapital", _fmt_eur(endkapital))
            rc2.metric("Davon eingezahlt", _fmt_eur(eingezahlt))
            rc3.metric("Davon Rendite", _fmt_eur(rendite_anteil))

            if endkapital >= z["zielbetrag"]:
                st.success("✅ Ziel erreicht")
            else:
                benoetigte_sparrate_abs = benoetigte_sparrate(z["zielbetrag"], rendite_pct / 100, z["zeitraum_jahre"])
                benoetigter_anteil_pct = (benoetigte_sparrate_abs / basis_sparrate * 100) if basis_sparrate else None
                hinweis = f"⚠️ Ziel mit {anteil_pct:.0f}% Anteil knapp verfehlt – benötigt wären "
                if benoetigter_anteil_pct is not None:
                    hinweis += f"{benoetigter_anteil_pct:.0f}% deiner Sparrate ({_fmt_eur(benoetigte_sparrate_abs)}/Monat)"
                else:
                    hinweis += f"{_fmt_eur(benoetigte_sparrate_abs)}/Monat"
                st.warning(hinweis)

            st.slider("Anteil dieser Sparrate (%)", 0, 100, value=int(round(anteil_pct)),
                       step=1, key=anteil_key)
            st.slider("Rendite anpassen (%)", 4.0, 10.0, value=float(rendite_pct), step=0.5, key=rendite_key)
            st.caption(
                "⚠️ Hochrechnung basiert auf historischen Durchschnittswerten. "
                "Tatsächliche Renditen können stark abweichen. Keine Anlageberatung."
            )
            aktueller_anteil = st.session_state.get(anteil_key, anteil_pct)
            aktualisierte_anteile[z["id"]] = aktueller_anteil
            aktualisierte_renditen[z["id"]] = st.session_state.get(rendite_key, rendite_pct) / 100
            summe_anteil += aktueller_anteil

    st.divider()
    if summe_anteil > 100:
        st.error(f"⚠️ Gesamtanteil überschreitet 100% (aktuell: {summe_anteil:.0f}%)")
    else:
        restbetrag = basis_sparrate * (100 - summe_anteil) / 100
        st.info(f"Zugewiesen: {summe_anteil:.0f}% · Noch nicht zugewiesen: {_fmt_eur(restbetrag)}")

    col_zurueck, col_weiter = st.columns(2)
    if col_zurueck.button("← Zurück", key="ob_step4_zurueck"):
        st.session_state.onboarding_step = 3
        st.rerun()
    if col_weiter.button("Weiter →", key="ob_step4_weiter", type="primary"):
        with get_session() as session:
            for goal_id, rendite in aktualisierte_renditen.items():
                g = session.get(PosGoal, goal_id)
                if g:
                    g.erwartete_rendite = rendite
                    g.sparrate_anteil_pct = aktualisierte_anteile.get(goal_id)
        st.session_state.onboarding_step = 5
        st.rerun()


# ─────────────────────────────────────────────
# SCHRITT 5 – ASSETKLASSEN
# ─────────────────────────────────────────────

def _step_assetklassen(user_id: int):
    st.header("In was investierst du? 💼")
    st.caption("Wähle alle Assetklassen die du nutzt oder nutzen möchtest.")

    with get_session() as session:
        pref = session.query(PosInvestmentPreference).filter_by(user_id=user_id).first()
        vorhandene = set(pref.aktive_assetklassen) if pref and pref.aktive_assetklassen else None

    ausgewaehlt = []
    for key, label, beschreibung, default in ASSETKLASSEN_OPTIONEN:
        checked_default = (key in vorhandene) if vorhandene is not None else default
        if st.checkbox(f"{label} – {beschreibung}", value=checked_default, key=f"ob_asset_{key}"):
            ausgewaehlt.append(key)

    etf_fokus = etf_ausschuettend = None
    if "etf" in ausgewaehlt:
        with st.expander("📈 ETF-Präferenzen", expanded=True):
            etf_fokus = st.radio("Geografischer Fokus", ["World", "EM", "Europa", "Sektoren", "Mehrere"],
                                  horizontal=True, key="ob_etf_fokus")
            etf_ausschuettend = st.radio("Ausschüttend oder Thesaurierend?",
                                          ["Thesaurierend", "Ausschüttend"],
                                          horizontal=True, key="ob_etf_ausschuettung") == "Ausschüttend"
            st.radio("Domizil-Präferenz", ["Irland (empfohlen)", "USA", "Andere"], index=0, key="ob_etf_domizil")

    aktien_strategie = None
    blacklist = []
    if "stocks" in ausgewaehlt:
        with st.expander("🏢 Einzelaktien-Präferenzen", expanded=True):
            aktien_strategie = st.radio("Strategie", ["Dividende", "Wachstum", "Beides"],
                                         horizontal=True, key="ob_aktien_strategie")
            st.markdown("**Ausschlusskriterien (Blacklist)**")
            for bkey, blabel in BLACKLIST_OPTIONEN:
                if st.checkbox(blabel, key=f"ob_blacklist_{bkey}"):
                    blacklist.append(bkey)

    col_zurueck, col_weiter = st.columns(2)
    if col_zurueck.button("← Zurück", key="ob_step5_zurueck"):
        st.session_state.onboarding_step = 4
        st.rerun()
    if col_weiter.button("Weiter →", key="ob_step5_weiter", type="primary"):
        if not ausgewaehlt:
            st.error("Bitte mindestens eine Assetklasse auswählen.")
        else:
            with get_session() as session:
                pref = session.query(PosInvestmentPreference).filter_by(user_id=user_id).first()
                if pref is None:
                    pref = PosInvestmentPreference(user_id=user_id)
                    session.add(pref)
                pref.aktive_assetklassen = ausgewaehlt
                pref.etf_fokus = etf_fokus
                pref.etf_ausschuettend = bool(etf_ausschuettend)
                pref.aktien_strategie = aktien_strategie
                pref.blacklist = blacklist
            st.session_state.onboarding_step = 6
            st.rerun()


# ─────────────────────────────────────────────
# SCHRITT 6 – ZIELGEWICHTUNG
# ─────────────────────────────────────────────

def _step_gewichtung(user_id: int):
    st.header("Wie soll dein Portfolio aufgeteilt sein? ⚖️")

    with get_session() as session:
        user = session.get(PosUser, user_id)
        profil_key = user.risikoprofil or "ausgewogen"
        pref = session.query(PosInvestmentPreference).filter_by(user_id=user_id).first()
        gewaehlte_klassen = (pref.aktive_assetklassen if pref and pref.aktive_assetklassen
                              else list(ASSETKLASSE_SLUG_MAP.keys()))
        vorhandene_gewichte = {
            tw.asset_class.slug: round(tw.target_pct * 100)
            for tw in session.query(PosTargetWeight).filter_by(user_id=user_id).all()
        }

    vorschlag = GEWICHTUNG_VORSCHLAEGE.get(profil_key, GEWICHTUNG_VORSCHLAEGE["ausgewogen"])
    # ensure_100() ist hier nötig, weil das Herausfiltern nicht gewählter Assetklassen
    # (z.B. "gold" abgewählt) die Summe des statischen Vorschlags unter 100 drücken würde.
    vorschlag_gefiltert = ensure_100({k: v for k, v in vorschlag.items() if k in gewaehlte_klassen})

    st.caption(
        f"KI-Vorschlag basierend auf deinem Risikoprofil "
        f"({RISIKO_PROFILE.get(profil_key, RISIKO_PROFILE['ausgewogen'])['label']}) und gewählten Assetklassen. "
        "Die Summe ergibt immer exakt 100%."
    )
    if st.button("🤖 KI-Vorschlag übernehmen", key="ob_gewicht_uebernehmen"):
        with st.spinner("KI erstellt Zielgewichtung..."):
            ki_gewichte = llm_analyst.suggest_target_weights(profil_key, gewaehlte_klassen)
        gewichtung = ensure_100(ki_gewichte) if ki_gewichte else vorschlag_gefiltert
        for k in gewaehlte_klassen:
            st.session_state[f"ob_gewicht_{k}"] = int(round(gewichtung.get(k, 0)))
        st.rerun()

    summe = 0
    gewichte = {}
    for k in gewaehlte_klassen:
        label = ASSETKLASSEN_LABELS.get(k, k)
        slug = ASSETKLASSE_SLUG_MAP[k]
        default_wert = vorhandene_gewichte.get(slug, vorschlag_gefiltert.get(k, 0))
        wert = st.slider(label, 0, 100, int(st.session_state.get(f"ob_gewicht_{k}", default_wert)),
                          key=f"ob_gewicht_{k}")
        gewichte[k] = wert
        summe += wert

    if summe == 100:
        st.success(f"Summe: {summe}% ✅")
    else:
        st.warning(f"Bitte auf 100% anpassen (aktuell: {summe}%)")

    col_zurueck, col_weiter = st.columns(2)
    if col_zurueck.button("← Zurück", key="ob_step6_zurueck"):
        st.session_state.onboarding_step = 5
        st.rerun()
    if col_weiter.button("Weiter →", key="ob_step6_weiter", type="primary"):
        if summe != 100:
            st.error(f"Summe muss 100% ergeben (aktuell: {summe}%).")
        else:
            with get_session() as session:
                for k, wert in gewichte.items():
                    slug = ASSETKLASSE_SLUG_MAP[k]
                    ac = get_asset_class_by_slug(session, slug)
                    if ac is None:
                        continue
                    existing = session.query(PosTargetWeight).filter_by(user_id=user_id, asset_class_id=ac.id).first()
                    if existing:
                        existing.target_pct = wert / 100
                    else:
                        session.add(PosTargetWeight(user_id=user_id, asset_class_id=ac.id, target_pct=wert / 100))
            st.session_state.onboarding_step = 7
            st.rerun()


# ─────────────────────────────────────────────
# SCHRITT 7 – POSITIONEN IMPORTIEREN
# ─────────────────────────────────────────────

def _step_import(user_id: int):
    st.header("Deine bestehenden Positionen 📂")
    st.caption("Wie möchtest du deine aktuellen Investments eingeben?")

    modus = st.session_state.get("ob_import_modus")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.caption("Comdirect, Trade Republic")
        if st.button("📄 CSV-Import", key="ob_import_csv_btn", use_container_width=True):
            st.session_state["ob_import_modus"] = "csv"
            st.rerun()
    with c2:
        st.caption("KI liest aus deiner App")
        if st.button("📸 Screenshot", key="ob_import_screenshot_btn", use_container_width=True):
            st.session_state["ob_import_modus"] = "screenshot"
            st.rerun()
    with c3:
        st.caption("Position für Position")
        if st.button("✏️ Manuell", key="ob_import_manuell_btn", use_container_width=True):
            st.session_state["ob_import_modus"] = "manuell"
            st.rerun()
    with c4:
        st.caption("Erst mal nur Dashboard sehen")
        if st.button("⏭️ Später", key="ob_import_spaeter_btn", use_container_width=True):
            st.session_state.onboarding_step = 8
            st.rerun()

    st.divider()

    if modus == "csv":
        st.subheader("📄 CSV-Import")
        broker = st.selectbox("Broker", ["comdirect", "tr"],
                               format_func=lambda b: "Comdirect" if b == "comdirect" else "Trade Republic",
                               key="ob_csv_broker")
        csv_file = st.file_uploader("CSV-Datei", type=["csv"], key="ob_csv_upload")
        if csv_file is not None and st.button("Importieren", key="ob_csv_importieren_btn"):
            try:
                portfolio_id = _stelle_default_portfolio_sicher(user_id)
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                    tmp.write(csv_file.getvalue())
                    tmp_path = tmp.name
                ergebnis = portfolio_module.import_csv(portfolio_id, tmp_path, broker)
                st.success(f"{ergebnis['imported']} Transaktion(en) importiert, {ergebnis['skipped']} übersprungen.")
            except Exception as e:
                st.error(f"Import fehlgeschlagen: {e}")

    elif modus == "screenshot":
        st.subheader("📸 Screenshot-Import")
        img_file = st.file_uploader("Screenshot (JPG/PNG)", type=["jpg", "jpeg", "png"], key="ob_screenshot_upload")
        if img_file is not None and st.button("KI-Analyse starten", key="ob_screenshot_analyse_btn"):
            with st.spinner("KI liest Positionen aus dem Screenshot..."):
                erkannt = llm_analyst.analyze_portfolio_screenshot(img_file.getvalue(), img_file.type)
            st.session_state["ob_screenshot_erkannt"] = erkannt
            if not erkannt:
                st.error("Es konnten keine Positionen erkannt werden. Bitte manuell erfassen.")

        erkannt = st.session_state.get("ob_screenshot_erkannt")
        if erkannt:
            st.success(f"{len(erkannt)} Position(en) erkannt – bitte prüfen:")
            for pos in erkannt:
                st.write(f"- {pos.get('ticker') or pos.get('name')}: {pos.get('quantity')} Stück @ {pos.get('kaufpreis')} €")
            if st.button("Alle importieren", key="ob_screenshot_importieren_btn"):
                portfolio_id = _stelle_default_portfolio_sicher(user_id)
                importiert = 0
                for pos in erkannt:
                    ticker_roh = pos.get("ticker") or pos.get("name")
                    if not ticker_roh or not pos.get("quantity"):
                        continue
                    try:
                        kandidaten = portfolio_module.resolve_ticker(str(ticker_roh))
                        ticker = kandidaten[0]["symbol"] if kandidaten else portfolio_module.normalize_ticker(str(ticker_roh))
                        portfolio_module.add_transaction(
                            portfolio_id, "kauf", ticker, float(pos.get("quantity") or 0),
                            float(pos.get("kaufpreis") or 0), date.today(), 0.0,
                        )
                        importiert += 1
                    except Exception:
                        continue
                st.success(f"{importiert} Position(en) importiert.")
                st.session_state.pop("ob_screenshot_erkannt", None)

    elif modus == "manuell":
        st.subheader("✏️ Position manuell erfassen")
        with st.form("ob_manuelle_position", clear_on_submit=True):
            m_ticker = st.text_input("Ticker (z.B. AAPL, SAP.DE)")
            m_qty = st.number_input("Anzahl", min_value=0.0, step=1.0)
            m_preis = st.number_input("Kaufpreis (€)", min_value=0.0, step=0.01,
                                       help="Preis pro Stück in Euro, z.B. 123.45")
            if st.form_submit_button("Position hinzufügen"):
                if not m_ticker or m_qty <= 0:
                    st.error("Bitte Ticker und Anzahl angeben.")
                else:
                    try:
                        portfolio_id = _stelle_default_portfolio_sicher(user_id)
                        kandidaten = portfolio_module.resolve_ticker(m_ticker)
                        ticker = kandidaten[0]["symbol"] if kandidaten else portfolio_module.normalize_ticker(m_ticker)
                        portfolio_module.add_transaction(portfolio_id, "kauf", ticker, m_qty, m_preis, date.today(), 0.0)
                        st.success(f"Position {ticker} hinzugefügt.")
                    except Exception as e:
                        st.error(f"Fehler: {e}")

    st.divider()
    col_zurueck, col_weiter = st.columns(2)
    if col_zurueck.button("← Zurück", key="ob_step7_zurueck"):
        st.session_state.onboarding_step = 6
        st.rerun()
    if col_weiter.button("Weiter →", key="ob_step7_weiter", type="primary"):
        st.session_state.onboarding_step = 8
        st.rerun()


# ─────────────────────────────────────────────
# SCHRITT 8 – FERTIG
# ─────────────────────────────────────────────

def _step_fertig(user_id: int):
    st.header("Du bist startklar! 🎉")

    with get_session() as session:
        user = session.get(PosUser, user_id)
        ziele = [{"typ": g.typ, "name": g.name, "zielbetrag": g.zielbetrag, "zeitraum_jahre": g.zeitraum_jahre}
                  for g in session.query(PosGoal).filter_by(user_id=user_id).all()]
        gewichte = [{"slug": gw.asset_class.slug, "target_pct": gw.target_pct}
                    for gw in session.query(PosTargetWeight).filter_by(user_id=user_id).all()]
        profil_key = user.risikoprofil or "ausgewogen"
        horizont = user.anlagehorizont_jahre
        sparrate = user.monatliche_sparrate

    with st.container(border=True):
        st.markdown("**Dein Profil**")
        st.write(f"Risikoprofil: {RISIKO_PROFILE.get(profil_key, RISIKO_PROFILE['ausgewogen'])['label']}")
        st.write(f"Anlagehorizont: {horizont} Jahre")
        st.write(f"Monatliche Sparrate: {_fmt_eur(sparrate)}")

        st.markdown("**Deine Ziele**")
        if ziele:
            for z in ziele:
                icon = ZIEL_ICONS.get(z["typ"], "🎯")
                st.write(f"{icon} {z['name']}: {_fmt_eur(z['zielbetrag'])} in {z['zeitraum_jahre']} Jahren")
        else:
            st.caption("Keine Ziele hinterlegt.")

        st.markdown("**Deine Zielgewichtung**")
        if gewichte:
            st.write(" | ".join(
                f"{ASSETKLASSEN_LABELS.get(SLUG_ZU_ASSETKLASSE.get(g['slug'], g['slug']), g['slug'])} "
                f"{g['target_pct'] * 100:.0f}%"
                for g in gewichte
            ))
        else:
            st.caption("Keine Zielgewichtung hinterlegt.")

    col_zurueck, col_fertig = st.columns([1, 3])
    if col_zurueck.button("← Zurück", key="ob_step8_zurueck"):
        st.session_state.onboarding_step = 7
        st.rerun()
    if col_fertig.button("Zum Dashboard →", key="ob_fertig_btn", use_container_width=True, type="primary"):
        with get_session() as session:
            user = session.get(PosUser, user_id)
            user.onboarding_completed = True
        for key in list(st.session_state.keys()):
            if key.startswith("ob_"):
                del st.session_state[key]
        st.session_state.pop("onboarding_step", None)
        st.rerun()


# ─────────────────────────────────────────────
# ORCHESTRIERUNG
# ─────────────────────────────────────────────

_SCHRITTE = {
    1: _step_profil, 2: _step_risiko, 3: _step_ziele, 4: _step_rechner,
    5: _step_assetklassen, 6: _step_gewichtung, 7: _step_import, 8: _step_fertig,
}


def show_onboarding(user_id: int):
    """Rendert den Onboarding-Wizard für user_id. dashboard.py ruft dies auf und
    setzt danach st.stop(), solange pos_users.onboarding_completed = False ist."""
    if "onboarding_step" not in st.session_state:
        st.session_state.onboarding_step = 1
    step = st.session_state.onboarding_step

    _progress_bar(step)
    _SCHRITTE[step](user_id)
