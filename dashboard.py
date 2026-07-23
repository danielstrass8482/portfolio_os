"""
dashboard.py – Streamlit-Dashboard für Portfolio-OS.
8 Tabs: Übersicht, Positionen, Rebalancing, Steuer, Immobilie, Familie,
KI-Analyse, Verwaltung. Keine Sidebar – die Nutzer-/Familienauswahl steht
oberhalb der Tabs (wird von allen Tabs benötigt), alle Anlege-/Bearbeiten-/
Löschen-Formulare stecken im Tab „⚙️ Verwaltung“.

Datenzugriff läuft ausschließlich über portfolio.py / tax_engine.py /
rebalancing.py / llm_analyst.py – dashboard.py enthält keine eigene
Geschäftslogik, nur Darstellung und Formulare.
"""

import os
import tempfile
from datetime import date, datetime

import streamlit as st
import pandas as pd
import plotly.express as px

from config import validate_config, BASE_URL
from database import (
    init_db, get_session, get_or_create_user, save_real_estate,
    PosUser, PosPortfolio, PosAssetClass, PosTargetWeight,
    PosRealEstate, PosFamilyGoal, PosTaxConfig, PosTransaction,
)
import portfolio as portfolio_module
import tax_engine
import rebalancing
import llm_analyst

def fmt_eur(wert, nachkommastellen=2):
    if wert is None:
        return "–"
    return f"{wert:,.{nachkommastellen}f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def fmt_zahl(wert, nachkommastellen=2):
    if wert is None:
        return "–"
    return f"{wert:,.{nachkommastellen}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_menge(menge):
    if menge is None:
        return "–"
    if menge == int(menge):
        return str(int(menge))
    return f"{menge:.4f}".replace(".", ",")


def _tabellen_safe(fn):
    """Wrappt fmt_eur/fmt_zahl/fmt_menge für pandas Styler.format() – NaN
    (z.B. aus noch nie aktualisierten current_price-Werten) wird von pandas
    aus None erzeugt und würde sonst nicht durch die 'wert is None'-Prüfung
    der Formatierer abgefangen."""
    return lambda w: fn(None if pd.isna(w) else w)


PORTFOLIO_TYPEN = ["depot", "krypto", "immobilie", "konto"]

# ── PAGE CONFIG ────────────────────────────────────────────────────
st.set_page_config(
    page_title="Portfolio-OS",
    page_icon="💼",
    layout="wide",
)

init_db()

# Preise einmal pro Browser-Session automatisch aktualisieren (nicht bei jedem
# Rerun – yfinance-Abfragen sind zu langsam, um sie bei jeder Nutzerinteraktion
# erneut auszuführen). Zusätzlich gibt es den manuellen Button im Positionen-Tab.
if "preise_beim_start_aktualisiert" not in st.session_state:
    with st.spinner("Aktualisiere Kurse (einmalig beim Start)..."):
        portfolio_module.update_prices()
    st.session_state.preise_beim_start_aktualisiert = True

# ── STYLING (gleiches dunkles Theme wie der Trading Bot) ───────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap');
  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
  .block-container { padding: 2rem 2.5rem 2rem 2.5rem; max-width: 1400px; }

  .kpi-card {
    background: #0f1117; border: 1px solid #1e2130; border-radius: 10px;
    padding: 1.2rem 1.5rem; margin-bottom: 0.5rem;
  }
  .kpi-label {
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase; color: #6b7280; margin-bottom: 0.4rem;
  }
  .kpi-value {
    font-family: 'JetBrains Mono', monospace; font-size: 1.8rem;
    font-weight: 600; color: #f9fafb; line-height: 1.1;
  }
  .kpi-value.positive { color: #34d399; }
  .kpi-value.negative { color: #f87171; }
  .kpi-value.neutral  { color: #60a5fa; }

  .badge {
    display: inline-block; padding: 0.2rem 0.6rem; border-radius: 4px;
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.05em;
  }
  .badge-gruen    { background: #064e3b; color: #34d399; }
  .badge-hellgelb { background: #4a3b0a; color: #fde68a; }
  .badge-gelb     { background: #4a3b0a; color: #fbbf24; }
  .badge-rot      { background: #450a0a; color: #f87171; }
</style>
""", unsafe_allow_html=True)

AMPEL_LABEL = {"gruen": "🟢 im Ziel", "hellgelb": "🟡 leicht abweichend", "gelb": "🟠 Alert", "rot": "🔴 Verkauf prüfen"}


# ─────────────────────────────────────────────
# BESTÄTIGUNGSDIALOGE (Portfolio/Position löschen)
# ─────────────────────────────────────────────

@st.dialog("Portfolio löschen?")
def _dialog_portfolio_loeschen(portfolio_id: int, name: str):
    st.warning(f"Portfolio „{name}“ wirklich löschen? Geht nur, wenn keine Positionen mehr enthalten sind.")
    col_ja, col_nein = st.columns(2)
    if col_ja.button("Ja, endgültig löschen", key="confirm_delete_pf"):
        try:
            portfolio_module.delete_portfolio(portfolio_id)
            st.success("Portfolio gelöscht")
        except Exception as e:
            st.error(f"Fehler: {e}")
        else:
            st.rerun()
    if col_nein.button("Abbrechen", key="cancel_delete_pf"):
        st.rerun()


@st.dialog("Position löschen?")
def _dialog_position_loeschen(position_id: int, anzeigename: str):
    st.warning(f"Position „{anzeigename}“ inkl. ALLER Transaktionen wirklich löschen? Das kann nicht rückgängig gemacht werden.")
    col_ja, col_nein = st.columns(2)
    if col_ja.button("Ja, endgültig löschen", key="confirm_delete_pos"):
        portfolio_module.delete_position(position_id)
        st.success("Position gelöscht")
        st.rerun()
    if col_nein.button("Abbrechen", key="cancel_delete_pos"):
        st.rerun()


# ─────────────────────────────────────────────
# BOOTSTRAP: NUTZER / KONTEXT (ersetzt die frühere Sidebar)
# ─────────────────────────────────────────────

def _alle_nutzer():
    with get_session() as session:
        return [{"id": u.id, "name": u.name, "email": u.email, "rolle": u.rolle} for u in session.query(PosUser).all()]


nutzer = _alle_nutzer()

st.title("💼 Portfolio-OS")

if not nutzer:
    st.warning("Noch kein Nutzer angelegt.")
    with st.form("neuer_erstnutzer"):
        name = st.text_input("Name")
        email = st.text_input("E-Mail (optional)")
        if st.form_submit_button("Nutzer anlegen") and name:
            with get_session() as session:
                get_or_create_user(session, name, email, rolle="admin")
            st.rerun()
    st.stop()

nutzer_namen = [n["name"] for n in nutzer]

kontext_col1, kontext_col2, kontext_col3 = st.columns([2, 2, 3])
with kontext_col1:
    familien_modus = st.toggle("👨‍👩‍👧 Familien-Portfolio (alle Nutzer)", value=False)
with kontext_col2:
    if not familien_modus:
        gewaehlter_name = st.selectbox("Portfolio von", nutzer_namen, label_visibility="collapsed")
    else:
        st.caption("Alle Nutzer aktiv")

if not familien_modus:
    aktiver_user = next(n for n in nutzer if n["name"] == gewaehlter_name)
    aktive_user_ids = [aktiver_user["id"]]
else:
    aktiver_user = None
    aktive_user_ids = [n["id"] for n in nutzer]

with kontext_col3:
    st.caption(f"{'Familien-Portfolio' if familien_modus else aktiver_user['name']} · {BASE_URL}")

warnungen = validate_config()
if warnungen:
    with st.expander("⚠️ Konfigurationshinweise"):
        for w in warnungen:
            st.caption(w)


def _kombinierte_summary(user_ids: list) -> dict:
    """Aggregiert get_portfolio_summary über mehrere Nutzer (für den Familien-Modus)."""
    gesamt = {"gesamtvermoegen": 0.0, "unrealized_pnl": 0.0, "positions_count": 0,
              "portfolios_count": 0, "asset_breakdown": {}}
    for uid in user_ids:
        s = portfolio_module.get_portfolio_summary(uid)
        gesamt["gesamtvermoegen"] += s["gesamtvermoegen"]
        gesamt["unrealized_pnl"] += s["unrealized_pnl"]
        gesamt["positions_count"] += s["positions_count"]
        gesamt["portfolios_count"] += s["portfolios_count"]
        for klass, wert in s["asset_breakdown"].items():
            gesamt["asset_breakdown"][klass] = gesamt["asset_breakdown"].get(klass, 0.0) + wert
    return gesamt


tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
    "📊 Übersicht", "📋 Positionen", "⚖️ Rebalancing", "🧾 Steuer",
    "🏠 Immobilie", "👨‍👩‍👧‍👦 Familie", "🤖 KI-Analyse", "⚙️ Verwaltung",
])

# ─────────────────────────────────────────────
# TAB 1 – ÜBERSICHT
# ─────────────────────────────────────────────
with tab1:
    summary = _kombinierte_summary(aktive_user_ids) if familien_modus else portfolio_module.get_portfolio_summary(aktive_user_ids[0])

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f'<div class="kpi-card"><div class="kpi-label">Gesamtvermögen</div>'
                    f'<div class="kpi-value neutral">{fmt_eur(summary["gesamtvermoegen"])}</div></div>', unsafe_allow_html=True)
    with c2:
        klasse = "positive" if summary["unrealized_pnl"] >= 0 else "negative"
        vz = "+" if summary["unrealized_pnl"] >= 0 else ""
        st.markdown(f'<div class="kpi-card"><div class="kpi-label">Unrealisierter G/V</div>'
                    f'<div class="kpi-value {klasse}">{vz}{fmt_eur(summary["unrealized_pnl"])}</div></div>', unsafe_allow_html=True)
    with c3:
        st.markdown(f'<div class="kpi-card"><div class="kpi-label">Positionen</div>'
                    f'<div class="kpi-value neutral">{summary["positions_count"]}</div></div>', unsafe_allow_html=True)
    with c4:
        st.markdown(f'<div class="kpi-card"><div class="kpi-label">Depots</div>'
                    f'<div class="kpi-value neutral">{summary["portfolios_count"]}</div></div>', unsafe_allow_html=True)

    st.markdown("&nbsp;", unsafe_allow_html=True)
    col_donut, col_top = st.columns([1, 1])

    with col_donut:
        st.subheader("Verteilung nach Assetklasse")
        if summary["asset_breakdown"]:
            df_alloc = pd.DataFrame(
                {"Assetklasse": list(summary["asset_breakdown"].keys()),
                 "Wert": list(summary["asset_breakdown"].values())}
            )
            fig = px.pie(df_alloc, names="Assetklasse", values="Wert", hole=0.6,
                         color_discrete_sequence=px.colors.qualitative.Set2)
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                               font_color="#f9fafb", legend=dict(orientation="h", y=-0.1))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Noch keine Positionen erfasst – siehe Tab ⚙️ Verwaltung.")

    with col_top:
        st.subheader("Top-Gewinner / Top-Verlierer")
        alle_pos = []
        for uid in aktive_user_ids:
            alle_pos.extend(portfolio_module.get_positions(uid))
        bewertbar = [p for p in alle_pos if p["unrealized_pnl_pct"] is not None]
        if bewertbar:
            gewinner_pool = [p for p in bewertbar if p["unrealized_pnl_pct"] > 0]
            verlierer_pool = [p for p in bewertbar if p["unrealized_pnl_pct"] < 0]
            top_gewinner = sorted(gewinner_pool, key=lambda p: -p["unrealized_pnl_pct"])[:3]
            top_verlierer = sorted(verlierer_pool, key=lambda p: p["unrealized_pnl_pct"])[:3]

            st.caption("🏆 Gewinner")
            if top_gewinner:
                for p in top_gewinner:
                    st.markdown(f"**{p['name']}** — +{fmt_zahl(p['unrealized_pnl_pct'], 1)}%")
            else:
                st.info("Keine Gewinner heute")

            st.caption("📉 Verlierer")
            if top_verlierer:
                for p in top_verlierer:
                    st.markdown(f"**{p['name']}** — {fmt_zahl(p['unrealized_pnl_pct'], 1)}%")
            else:
                st.caption("Keine Verlierer heute")
        else:
            st.info("Noch keine bewerteten Positionen (Kurse aktualisieren).")

    st.subheader("Zielfortschritt")
    with get_session() as session:
        ziele = [
            {"name": z.name, "fortschritt_pct": z.fortschritt_pct,
             "aktuell_betrag": z.aktuell_betrag, "ziel_betrag": z.ziel_betrag}
            for z in session.query(PosFamilyGoal).all()
        ]
    if ziele:
        for z in ziele:
            st.progress(min(1.0, z["fortschritt_pct"] / 100),
                        text=f"{z['name']}: {fmt_zahl(z['fortschritt_pct'], 0)}% "
                             f"({fmt_zahl(z['aktuell_betrag'], 0)}/{fmt_zahl(z['ziel_betrag'], 0)} €)")
            if z["fortschritt_pct"] >= 100:
                st.success(f"🎉 Ziel „{z['name']}“ erreicht!")
    else:
        st.caption("Keine Familienziele hinterlegt (siehe Tab ⚙️ Verwaltung).")


# ─────────────────────────────────────────────
# TAB 2 – POSITIONEN
# ─────────────────────────────────────────────
with tab2:
    col_head, col_btn = st.columns([4, 1])
    with col_head:
        st.subheader("Alle Positionen")
    with col_btn:
        if st.button("🔄 Preise aktualisieren", key="preise_tab2_btn"):
            with st.spinner("Aktualisiere Kurse via yfinance..."):
                n = portfolio_module.update_prices()
            st.success(f"{n} Position(en) aktualisiert")
            st.rerun()

    alle_pos = []
    for uid in aktive_user_ids:
        alle_pos.extend(portfolio_module.get_positions(uid))

    if not alle_pos:
        st.info("Noch keine Positionen erfasst.")
    else:
        df = pd.DataFrame(alle_pos)
        col_f1, col_f2 = st.columns(2)
        broker_filter = col_f1.multiselect("Broker", sorted(df["broker"].dropna().unique().tolist()))
        klasse_filter = col_f2.multiselect("Assetklasse", sorted(df["asset_class"].dropna().unique().tolist()))

        gefiltert = df.copy()
        if broker_filter:
            gefiltert = gefiltert[gefiltert["broker"].isin(broker_filter)]
        if klasse_filter:
            gefiltert = gefiltert[gefiltert["asset_class"].isin(klasse_filter)]

        anzeige = gefiltert[[
            "name", "asset_class", "portfolio_name", "quantity",
            "avg_buy_price", "current_price", "market_value", "unrealized_pnl", "unrealized_pnl_pct",
        ]].rename(columns={
            "name": "Wertpapier", "asset_class": "Assetklasse", "portfolio_name": "Depot",
            "quantity": "Menge", "avg_buy_price": "Ø-Kaufpreis", "current_price": "Kurs",
            "market_value": "Wert", "unrealized_pnl": "G/V", "unrealized_pnl_pct": "G/V %",
        })

        # G/V = (aktueller_kurs - avg_kaufpreis) * menge, G/V% analog –
        # grün bei Gewinn, rot bei Verlust (gleiche Farben wie die KPI-Karten).
        def _pnl_farbe(wert):
            if pd.isna(wert):
                return ""
            farbe = "#34d399" if wert >= 0 else "#f87171"
            return f"color: {farbe}; font-weight: 600"

        def _vz_eur(w):
            if pd.isna(w):
                return "–"
            return ("+" if w >= 0 else "") + fmt_eur(w)

        def _vz_pct(w):
            if pd.isna(w):
                return "–"
            return ("+" if w >= 0 else "") + fmt_zahl(w, 1) + "%"

        styled = (
            anzeige.style
            .map(_pnl_farbe, subset=["G/V", "G/V %"])
            .format({
                "Menge": _tabellen_safe(fmt_menge),
                "Ø-Kaufpreis": _tabellen_safe(fmt_eur),
                "Kurs": _tabellen_safe(fmt_eur),
                "Wert": _tabellen_safe(fmt_eur),
                "G/V": _vz_eur,
                "G/V %": _vz_pct,
            })
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

        st.markdown("**Steuervorschau**")
        steuer_pos_options = {f"{row['name']} ({row['portfolio_name']})": row.to_dict()
                               for _, row in gefiltert.iterrows()}
        steuer_pos_wahl = st.selectbox("Position wählen", list(steuer_pos_options.keys()))
        gewaehlte_pos = steuer_pos_options[steuer_pos_wahl]
        if st.button("Steuervorschau anzeigen"):
            try:
                verkaufspreis = gewaehlte_pos["current_price"] or gewaehlte_pos["avg_buy_price"]
                preview = tax_engine.get_tax_preview(gewaehlte_pos["id"], verkaufspreis)

                with get_session() as _s:
                    _pf = _s.get(PosPortfolio, gewaehlte_pos["portfolio_id"])
                    steuer_user_id = _pf.user_id if _pf else None
                rest_vor = tax_engine.get_remaining_freistellung(steuer_user_id) if steuer_user_id else 0.0
                rest_nach = max(0.0, rest_vor - preview.get("freistellung_verrechnet", 0.0))

                brutto_erloes = preview["quantity"] * verkaufspreis
                einkaufswert = preview["quantity"] * (gewaehlte_pos["avg_buy_price"] or 0.0)
                gewinn = preview["brutto_gewinn"]
                gv_label = "Unrealisierter Gewinn" if gewinn >= 0 else "Unrealisierter Verlust"

                if gewinn <= 0:
                    hinweis = (
                        "✅ Keine Steuer fällig – du realisierst einen Verlust.\n"
                        "Dieser Verlust wird in deinen Verlusttopf eingebucht und kann\n"
                        "zukünftige Gewinne steuerlich ausgleichen."
                    )
                elif preview["steuer"] == 0:
                    hinweis = (
                        "✅ Keine Steuer fällig – der Gewinn wird vollständig über Verlusttopf "
                        "und/oder Freistellungsauftrag abgedeckt."
                    )
                else:
                    hinweis = (
                        f"💰 Geschätzte Steuer: {fmt_eur(preview['steuer'])}\n"
                        f"Netto-Erlös nach Steuer: {fmt_eur(preview['netto_erloes'])}"
                    )

                text = (
                    f"📊 Steuervorschau: {gewaehlte_pos['name']}\n\n"
                    f"Wenn du alle {fmt_menge(preview['quantity'])} Anteile heute zum aktuellen "
                    f"Kurs von {fmt_eur(verkaufspreis)} verkaufst:\n\n"
                    f"Bruttoerlös:              {fmt_eur(brutto_erloes)}\n"
                    f"Einkaufswert:             {fmt_eur(einkaufswert)}\n"
                    f"{gv_label}:{' ' * max(1, 15 - len(gv_label))}{fmt_eur(gewinn)}\n\n"
                    f"{hinweis}\n\n"
                    f"Verbleibender Freistellungsauftrag: {fmt_eur(rest_nach)}"
                )
                st.text(text)
            except Exception as e:
                st.error(f"Fehler: {e}")


# ─────────────────────────────────────────────
# TAB 3 – REBALANCING
# ─────────────────────────────────────────────
with tab3:
    if familien_modus:
        st.info("Rebalancing wird je Nutzer einzeln berechnet – bitte oben einen Nutzer auswählen.")
    else:
        uid = aktiver_user["id"]
        st.subheader("Aktuelle Abweichungen von der Zielgewichtung")
        deviations = rebalancing.calculate_deviations(uid)
        if deviations:
            for d in deviations:
                badge = AMPEL_LABEL.get(d["status"], d["status"])
                st.markdown(
                    f'<span class="badge badge-{d["status"]}">{badge}</span>&nbsp;&nbsp;'
                    f'**{d["asset_class"]}** — Ist {d["ist_pct"]*100:.1f}% / Ziel {d["ziel_pct"]*100:.1f}% '
                    f'(Δ {d["abweichung_pct"]*100:+.1f} Punkte)',
                    unsafe_allow_html=True,
                )
        else:
            st.info("Keine Ziel-Gewichtung hinterlegt (siehe Tab ⚙️ Verwaltung).")

        st.subheader("Offene Vorschläge")
        offene = [p for p in rebalancing.get_rebalancing_history(uid) if p["status"] == "pending"]
        if offene:
            for p in offene:
                with st.expander(f"Vorschlag #{p['id']} – {p['erstellt_am']:%d.%m.%Y}"):
                    st.write(p["begruendung"])
                    if p["ki_analyse"]:
                        st.caption(f"KI-Einordnung: {p['ki_analyse']}")
                    col_ok, col_no = st.columns(2)
                    if col_ok.button("✅ Bestätigen", key=f"ok_{p['id']}"):
                        rebalancing.confirm_proposal(p["id"], "confirmed")
                        st.rerun()
                    if col_no.button("❌ Ablehnen", key=f"no_{p['id']}"):
                        rebalancing.confirm_proposal(p["id"], "rejected")
                        st.rerun()
        else:
            st.caption("Keine offenen Vorschläge.")

        if st.button("🔎 Neuen Vorschlag erstellen (Schwellwert-Check)"):
            rebalancing.create_rebalancing_proposal(uid, "schwellwert")
            st.rerun()

        st.subheader("Rebalancing-Historie")
        historie = rebalancing.get_rebalancing_history(uid)
        if historie:
            df_hist = pd.DataFrame([{
                "Datum": h["erstellt_am"], "Status": h["status"],
                "Begründung": (h["begruendung"] or "")[:120],
            } for h in historie])
            st.dataframe(df_hist, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────
# TAB 4 – STEUER
# ─────────────────────────────────────────────
with tab4:
    if familien_modus:
        st.info("Steuerdaten werden je Nutzer einzeln berechnet – bitte oben einen Nutzer auswählen.")
    else:
        uid = aktiver_user["id"]
        rest = tax_engine.get_remaining_freistellung(uid)
        with get_session() as session:
            cfg = session.query(PosTaxConfig).filter_by(user_id=uid).first()
            freibetrag = cfg.freistellungsauftrag if cfg else 0.0
            genutzt = cfg.freistellungsgenutzt if cfg else 0.0

        st.subheader("Freistellungsauftrag")
        st.progress(min(1.0, genutzt / freibetrag) if freibetrag else 0.0,
                    text=f"{fmt_zahl(genutzt)} / {fmt_zahl(freibetrag)} € genutzt (Rest: {fmt_zahl(rest)} €)")

        st.subheader(f"Realisierte Gewinne/Verluste {date.today().year} (YTD)")
        uebersicht = tax_engine.generate_jahresuebersicht(uid, date.today().year)
        c1, c2, c3 = st.columns(3)
        c1.metric("Gewinne", fmt_eur(uebersicht['realisierte_gewinne']))
        c2.metric("Verluste", fmt_eur(uebersicht['realisierte_verluste']))
        c3.metric("Gezahlte Steuer", fmt_eur(uebersicht['steuer_gezahlt']))

        st.subheader("Tax-Loss-Harvesting-Kandidaten")
        kandidaten = tax_engine.find_tax_loss_harvesting(uid)
        if kandidaten:
            df_tlh = pd.DataFrame(kandidaten).drop(columns=["position_id"]).rename(columns={
                "ticker": "Name der Position",
                "quantity": "Anzahl",
                "avg_buy_price": "Ø-Kaufpreis",
                "current_price": "Aktueller Kurs",
                "unrealisierter_verlust": "Unrealisierter Verlust",
                "geschaetzte_steuerersparnis": "Geschätzte Steuerersparnis",
            })
            styled_tlh = df_tlh.style.format({
                "Anzahl": _tabellen_safe(fmt_menge),
                "Ø-Kaufpreis": _tabellen_safe(fmt_eur),
                "Aktueller Kurs": _tabellen_safe(fmt_eur),
                "Unrealisierter Verlust": _tabellen_safe(fmt_eur),
                "Geschätzte Steuerersparnis": _tabellen_safe(fmt_eur),
            })
            st.dataframe(styled_tlh, use_container_width=True, hide_index=True)
        else:
            st.caption("Keine Positionen im Minus.")

        st.subheader("Jahresübersicht")
        jahr_wahl = st.number_input("Jahr", min_value=2000, max_value=date.today().year, value=date.today().year)
        if st.button("📄 Jahresübersicht als PDF vorbereiten"):
            uebersicht_jahr = tax_engine.generate_jahresuebersicht(uid, int(jahr_wahl))
            try:
                from fpdf import FPDF
                pdf = FPDF()
                pdf.add_page()
                pdf.set_font("Helvetica", "B", 16)
                pdf.cell(0, 10, f"Jahresuebersicht {int(jahr_wahl)}", ln=True)
                pdf.set_font("Helvetica", "", 11)
                # "EUR" statt "€" hier bewusst beibehalten: FPDFs Kernschrift
                # Helvetica unterstützt kein €-Glyph ohne Unicode-Font-Einbettung.
                for label, wert in [
                    ("Nutzer", aktiver_user["name"]),
                    ("Realisierte Gewinne", f"{fmt_zahl(uebersicht_jahr['realisierte_gewinne'])} EUR"),
                    ("Realisierte Verluste", f"{fmt_zahl(uebersicht_jahr['realisierte_verluste'])} EUR"),
                    ("Netto-Ergebnis", f"{fmt_zahl(uebersicht_jahr['netto_ergebnis'])} EUR"),
                    ("Gezahlte Steuer", f"{fmt_zahl(uebersicht_jahr['steuer_gezahlt'])} EUR"),
                    ("Freistellungsauftrag", f"{fmt_zahl(uebersicht_jahr['freistellungsauftrag'])} EUR"),
                    ("Freistellung genutzt (aktuell)", f"{fmt_zahl(uebersicht_jahr['freistellung_genutzt_aktuell'])} EUR"),
                    ("Verlusttopf (aktuell)", f"{fmt_zahl(uebersicht_jahr['verlusttopf_aktuell'])} EUR"),
                ]:
                    pdf.cell(0, 8, f"{label}: {wert}", ln=True)
                pdf_bytes = bytes(pdf.output())
                st.download_button("⬇️ Download PDF", data=pdf_bytes,
                                    file_name=f"jahresuebersicht_{int(jahr_wahl)}.pdf", mime="application/pdf")
            except Exception as e:
                st.error(f"PDF-Erstellung fehlgeschlagen: {e}")


# ─────────────────────────────────────────────
# IMMOBILIEN-HELFER (Abschreibung & Finanzierung)
# ─────────────────────────────────────────────
ABSCHREIBUNGSARTEN = [
    "Keine Abschreibung",
    "Standard AfA (2% p.a.)",
    "Denkmalschutz Sonderabschreibung (§7i EStG)",
    "Energetische Sanierung (§35c EStG)",
    "Selbst genutzt (keine AfA)",
]
AFA_STANDARDSAETZE = {
    "Standard AfA (2% p.a.)": 2.0,
    "Denkmalschutz Sonderabschreibung (§7i EStG)": 9.0,
    "Energetische Sanierung (§35c EStG)": 14.0,
}
STEUERSATZ_SCHAETZUNG = 0.42  # geschätzter Grenzsteuersatz für die AfA-Ersparnis-Anzeige


def _parse_iso_date(wert):
    """Wandelt einen 'YYYY-MM-DD'-String (oder None/ungültig) der KI-Antwort in ein date um."""
    if not wert:
        return None
    try:
        return datetime.strptime(wert, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _immobilie_erweiterte_felder(key_prefix: str) -> dict:
    """
    Rendert die Sektionen 'Abschreibung' und 'Finanzierung & Vermietung' für die
    Immobilien-Erfassung und gibt die eingegebenen Werte als dict zurück. Wird
    bewusst AUSSERHALB eines st.form aufgerufen, damit der Abschreibungssatz-
    Vorschlag beim Wechsel der Abschreibungsart sofort (per Rerun) aktualisiert wird.
    """
    st.markdown("**Abschreibung**")
    afa_art = st.selectbox("Abschreibungsart", ABSCHREIBUNGSARTEN, key=f"{key_prefix}_afa_art")
    felder = {"abschreibungsart": afa_art}
    if afa_art in ("Keine Abschreibung", "Selbst genutzt (keine AfA)"):
        felder["abschreibungsbasis"] = 0.0
        felder["abschreibungssatz"] = 0.0
    else:
        c1, c2 = st.columns(2)
        felder["abschreibungsbasis"] = c1.number_input(
            "Abschreibungsbasis (€)", min_value=0.0, step=1000.0, key=f"{key_prefix}_afa_basis")
        felder["abschreibungssatz"] = c2.number_input(
            "Abschreibungssatz (%)", min_value=0.0, step=0.1,
            value=AFA_STANDARDSAETZE.get(afa_art, 2.0), key=f"{key_prefix}_afa_satz")
    felder["kaufdatum"] = st.date_input("Kaufdatum", value=None, key=f"{key_prefix}_kaufdatum")

    st.markdown("**Finanzierung & Vermietung**")
    felder["vermietung_start"] = st.date_input(
        "Vermietung gestartet am (optional)", value=None, key=f"{key_prefix}_vermietung_start")
    c1, c2 = st.columns(2)
    felder["kredit_gesamtbetrag"] = c1.number_input(
        "Kredit Gesamtbetrag (€)", min_value=0.0, step=1000.0, key=f"{key_prefix}_kredit_gesamt")
    felder["kredit_abgerufen"] = c2.number_input(
        "Davon bereits abgerufen (€)", min_value=0.0, step=1000.0, key=f"{key_prefix}_kredit_abgerufen")
    c3, c4 = st.columns(2)
    felder["kredit_zinssatz"] = c3.number_input(
        "Zinssatz (% p.a.)", min_value=0.0, step=0.1, key=f"{key_prefix}_zinssatz")
    felder["kredit_laufzeit_jahre"] = int(c4.number_input(
        "Laufzeit (Jahre)", min_value=0, step=1, key=f"{key_prefix}_laufzeit"))
    felder["zinsbindung_bis"] = st.date_input(
        "Zinsbindung bis (optional)", value=None, key=f"{key_prefix}_zinsbindung")
    felder["vorfaelligkeitsgebuehr_pct"] = st.number_input(
        "Vorfälligkeitsentschädigung im Vertrag (% des Restdarlehens)",
        min_value=0.0, step=0.1, key=f"{key_prefix}_vorfaelligkeit")
    felder["finanzierungskosten"] = st.number_input(
        "Finanzierungskosten p.a. (€)", min_value=0.0, step=100.0, key=f"{key_prefix}_finanzierungskosten")
    return felder


# ─────────────────────────────────────────────
# TAB 5 – IMMOBILIE
# ─────────────────────────────────────────────
with tab5:
    with get_session() as session:
        immobilien = session.query(PosRealEstate).filter(PosRealEstate.user_id.in_(aktive_user_ids)).all()
        immobilien_data = [{
            "id": i.id, "user_id": i.user_id, "adresse": i.adresse, "kaufpreis": i.kaufpreis, "kaufjahr": i.kaufjahr,
            "eigenkapital": i.eigenkapital, "restschuld": i.restschuld, "monatliche_rate": i.monatliche_rate,
            "mieteinnahmen": i.mieteinnahmen, "letzter_schaetzwert": i.letzter_schaetzwert,
            "letztes_update": i.letztes_update,
            "vermietung_start": i.vermietung_start, "kredit_gesamtbetrag": i.kredit_gesamtbetrag,
            "kredit_abgerufen": i.kredit_abgerufen, "kredit_zinssatz": i.kredit_zinssatz,
            "kredit_laufzeit_jahre": i.kredit_laufzeit_jahre, "vorfaelligkeitsgebuehr_pct": i.vorfaelligkeitsgebuehr_pct,
            "zinsbindung_bis": i.zinsbindung_bis, "abschreibungsart": i.abschreibungsart,
            "abschreibungsbasis": i.abschreibungsbasis, "abschreibungssatz": i.abschreibungssatz,
            "kaufdatum": i.kaufdatum, "finanzierungskosten": i.finanzierungskosten,
        } for i in immobilien]

    if not immobilien_data:
        if familien_modus:
            st.info("Keine Immobilie hinterlegt. Bitte oben einen Nutzer auswählen, um eine anzulegen.")
        else:
            st.info("Noch keine Immobilie hinterlegt – hier direkt anlegen:")
            im5_adresse = st.text_input("Adresse", key="im5_adresse")
            im5_kaufpreis = st.number_input("Kaufpreis (€)", min_value=0.0, step=1000.0, key="im5_kaufpreis")
            im5_kaufjahr = st.number_input(
                "Kaufjahr", min_value=1950, max_value=date.today().year, value=date.today().year, key="im5_kaufjahr")
            im5_qm = st.number_input("Wohnfläche (qm)", min_value=0.0, step=1.0, key="im5_qm")
            im5_ek = st.number_input("Eigenkapital (€)", min_value=0.0, step=1000.0, key="im5_ek")
            im5_restschuld = st.number_input("Restschuld (€)", min_value=0.0, step=1000.0, key="im5_restschuld")
            im5_rate = st.number_input("Monatliche Rate (€)", min_value=0.0, step=10.0, key="im5_rate")
            im5_miete = st.number_input("Mieteinnahmen (€, optional)", min_value=0.0, step=10.0, key="im5_miete")

            st.divider()
            im5_erweitert = _immobilie_erweiterte_felder("im5")
            st.divider()

            if st.button("Speichern", key="im5_speichern_btn"):
                if not im5_adresse:
                    st.error("Bitte eine Adresse angeben.")
                else:
                    try:
                        save_real_estate(
                            user_id=aktiver_user["id"],
                            adresse=im5_adresse, kaufpreis=im5_kaufpreis,
                            kaufjahr=int(im5_kaufjahr), wohnflaeche_qm=im5_qm, eigenkapital=im5_ek,
                            restschuld=im5_restschuld, monatliche_rate=im5_rate, mieteinnahmen=im5_miete,
                            letzter_schaetzwert=im5_kaufpreis, letztes_update=datetime.utcnow(),
                            **im5_erweitert,
                        )
                        st.success("Immobilie gespeichert!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Fehler beim Speichern: {e}")
    else:
        for im in immobilien_data:
            st.subheader(im["adresse"])
            schaetzwert = im["letzter_schaetzwert"] or im["kaufpreis"]
            ltv = (im["restschuld"] / schaetzwert) if schaetzwert else 0.0
            # Eigenkapitalrendite (Cash-on-Cash, vereinfacht): jährlicher Cashflow / eingesetztes Eigenkapital
            jahres_cashflow = (im["mieteinnahmen"] - im["monatliche_rate"]) * 12
            ek_rendite = (jahres_cashflow / im["eigenkapital"]) if im["eigenkapital"] else 0.0

            c1, c2, c3 = st.columns(3)
            c1.metric("Letzter Schätzwert", fmt_eur(schaetzwert, 0))
            c2.metric("LTV-Ratio", f"{fmt_zahl(ltv*100, 1)}%")
            c3.metric("Eigenkapitalrendite (Cash-on-Cash)", f"{fmt_zahl(ek_rendite*100, 1)}%")

            # ---- Selbstnutzung / Vermietungsbeginn ------------------------
            if im["kaufjahr"]:
                if im["vermietung_start"]:
                    vermiet_jahr = im["vermietung_start"].year
                    eigennutzung_jahre = max(0, vermiet_jahr - im["kaufjahr"])
                    st.caption(
                        f"📅 Selbstnutzungsphase: Gekauft {im['kaufjahr']} – Vermietung ab {vermiet_jahr} "
                        f"({eigennutzung_jahre} Jahre Eigennutzung)"
                    )
                else:
                    st.caption(f"📅 Gekauft {im['kaufjahr']} – noch keine Vermietung erfasst")

            # ---- Finanzierung (Feature 2) ----------------------------------
            if im["kredit_gesamtbetrag"]:
                fc1, fc2, fc3 = st.columns(3)
                offener_kredit = (im["kredit_gesamtbetrag"] or 0.0) - (im["kredit_abgerufen"] or 0.0)
                fc1.metric("Noch nicht abgerufener Kredit", fmt_eur(offener_kredit, 0))
                vorfaelligkeit = (im["restschuld"] or 0.0) * (im["vorfaelligkeitsgebuehr_pct"] or 0.0) / 100
                fc2.metric("Geschätzte Vorfälligkeitsentschädigung", fmt_eur(vorfaelligkeit, 0))
                if im["zinsbindung_bis"]:
                    tage_bis_ablauf = (im["zinsbindung_bis"] - date.today()).days
                    jahre_bis_ablauf = round(tage_bis_ablauf / 365.0, 1)
                    fc3.metric("Zinsbindung läuft ab in", f"{fmt_zahl(jahre_bis_ablauf, 1)} Jahren")
                else:
                    fc3.metric("Zinsbindung läuft ab in", "–")

            # ---- Abschreibung (Feature 2) -----------------------------------
            if im["abschreibungsart"] not in (None, "Keine", "Keine Abschreibung", "Selbst genutzt (keine AfA)"):
                jaehrliche_afa = (im["abschreibungsbasis"] or 0.0) * (im["abschreibungssatz"] or 0.0) / 100
                steuerersparnis_jahr = jaehrliche_afa * STEUERSATZ_SCHAETZUNG
                gesamte_steuerersparnis = (im["abschreibungsbasis"] or 0.0) * STEUERSATZ_SCHAETZUNG
                effektiver_kaufpreis = im["kaufpreis"] - gesamte_steuerersparnis

                st.markdown("**Abschreibung (AfA)**")
                ac1, ac2, ac3 = st.columns(3)
                ac1.metric("Jährliche Abschreibung", fmt_eur(jaehrliche_afa, 0))
                ac2.metric("Steuerersparnis p.a.", fmt_eur(steuerersparnis_jahr, 0))
                ac3.metric("Gesamte Steuerersparnis", fmt_eur(gesamte_steuerersparnis, 0))
                st.caption(
                    f"Effektiver Kaufpreis nach Steuerersparnis: {fmt_eur(effektiver_kaufpreis, 0)} · "
                    "Geschätzt bei 42% Grenzsteuersatz – bitte mit Steuerberater abstimmen."
                )

                if im["abschreibungsart"] == "Denkmalschutz Sonderabschreibung (§7i EStG)":
                    start_jahr = im["kaufdatum"].year if im["kaufdatum"] else im["kaufjahr"]
                    if start_jahr:
                        jahre_seit_kauf = date.today().year - start_jahr
                        if jahre_seit_kauf < 8:
                            st.caption(f"Phase 1: 9% p.a. (Jahr 1-8) → noch {8 - jahre_seit_kauf} Jahre")
                            st.caption(f"Phase 2: 7% p.a. (Jahr 9-12) → ab Jahr {start_jahr + 8}")
                        elif jahre_seit_kauf < 12:
                            st.caption("Phase 1: 9% p.a. (Jahr 1-8) → abgeschlossen")
                            st.caption(f"Phase 2: 7% p.a. (Jahr 9-12) → noch {12 - jahre_seit_kauf} Jahre")
                        else:
                            st.caption("Sonderabschreibungsphase abgeschlossen (Jahr 1-12)")

            if st.button("🤖 KI-Schätzung anfordern", key=f"ki_schaetzung_{im['id']}"):
                with st.spinner("Fordere KI-Schätzung an (Web-Suche)..."):
                    ergebnis = llm_analyst.estimate_real_estate_value(im)
                st.info(ergebnis["text"])

            st.caption(
                "Historische Wertentwicklung (vereinfacht: nur Kaufpreis → letzter Schätzwert, "
                "da keine separate Bewertungshistorie in pos_real_estate geführt wird)."
            )
            verlauf = pd.DataFrame({
                "Datum": [date(im["kaufjahr"] or date.today().year, 1, 1),
                          (im["letztes_update"] or datetime.utcnow()).date()],
                "Wert": [im["kaufpreis"], schaetzwert],
            })
            fig = px.line(verlauf, x="Datum", y="Wert", markers=True)
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#f9fafb")
            st.plotly_chart(fig, use_container_width=True)

            # ---- Kreditvertrag hochladen & KI auslesen (Feature 3) ---------
            with st.expander("📄 Kreditvertrag hochladen"):
                st.caption("KI liest den Vertrag aus und trägt die Daten automatisch ein")
                kv_file = st.file_uploader(
                    "Kreditvertrag (PDF, JPG, PNG)", type=["pdf", "jpg", "jpeg", "png"],
                    key=f"kv_upload_{im['id']}")
                if kv_file is not None and st.button("KI-Analyse starten", key=f"kv_analyse_btn_{im['id']}"):
                    with st.spinner("KI analysiert Kreditvertrag..."):
                        erkannt = llm_analyst.analyze_kredit_vertrag(kv_file.getvalue(), kv_file.type)
                    if not erkannt:
                        st.error("Kreditvertrag konnte nicht ausgelesen werden. Bitte Werte manuell eintragen.")
                    else:
                        st.session_state[f"kv_erkannt_{im['id']}"] = erkannt

                erkannt = st.session_state.get(f"kv_erkannt_{im['id']}")
                if erkannt:
                    st.success("KI hat folgende Werte erkannt – bitte prüfen und ggf. korrigieren:")
                    kc1, kc2 = st.columns(2)
                    kv_gesamt = kc1.number_input(
                        "Kredit Gesamtbetrag (€)", min_value=0.0, step=1000.0,
                        value=float(erkannt.get("kredit_gesamtbetrag") or 0.0), key=f"kv_gesamt_{im['id']}")
                    kv_abgerufen = kc2.number_input(
                        "Davon abgerufen (€)", min_value=0.0, step=1000.0,
                        value=float(erkannt.get("kredit_abgerufen") or 0.0), key=f"kv_abgerufen_{im['id']}")
                    kc3, kc4 = st.columns(2)
                    kv_zins = kc3.number_input(
                        "Zinssatz (% p.a.)", min_value=0.0, step=0.1,
                        value=float(erkannt.get("zinssatz") or 0.0), key=f"kv_zins_{im['id']}")
                    kv_laufzeit = kc4.number_input(
                        "Laufzeit (Jahre)", min_value=0, step=1,
                        value=int(erkannt.get("laufzeit_jahre") or 0), key=f"kv_laufzeit_{im['id']}")
                    kv_zinsbindung = st.date_input(
                        "Zinsbindung bis", value=_parse_iso_date(erkannt.get("zinsbindung_bis")),
                        key=f"kv_zinsbindung_{im['id']}")
                    kv_vorfaelligkeit = st.number_input(
                        "Vorfälligkeitsentschädigung (% des Restdarlehens)", min_value=0.0, step=0.1,
                        value=float(erkannt.get("vorfaelligkeitsgebuehr_pct") or 0.0),
                        key=f"kv_vorfaelligkeit_{im['id']}")

                    info_bits = [
                        f"{label}: {erkannt[feld]}"
                        for label, feld in [
                            ("Bank", "bank"), ("Darlehensnehmer", "darlehensnehmer"),
                            ("Objekt", "objekt_adresse"), ("Abschluss", "abschluss_datum"),
                            ("Besonderheiten", "besonderheiten"),
                        ]
                        if erkannt.get(feld)
                    ]
                    if info_bits:
                        st.caption(" · ".join(info_bits))

                    if st.button("✅ Alle Werte übernehmen", key=f"kv_uebernehmen_{im['id']}"):
                        try:
                            save_real_estate(
                                user_id=im["user_id"],
                                real_estate_id=im["id"],
                                kredit_gesamtbetrag=kv_gesamt,
                                kredit_abgerufen=kv_abgerufen,
                                kredit_zinssatz=kv_zins,
                                kredit_laufzeit_jahre=int(kv_laufzeit),
                                zinsbindung_bis=kv_zinsbindung,
                                vorfaelligkeitsgebuehr_pct=kv_vorfaelligkeit,
                            )
                            st.session_state.pop(f"kv_erkannt_{im['id']}", None)
                            st.success("Kreditdaten übernommen!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fehler beim Speichern: {e}")

            st.divider()


# ─────────────────────────────────────────────
# TAB 6 – FAMILIE
# ─────────────────────────────────────────────
with tab6:
    st.subheader("Alle Depots aggregiert")
    zeilen = []
    kinder_zeilen = []
    for n in nutzer:
        with get_session() as session:
            portfolios_n = [
                {"name": p.name, "typ": p.typ, "broker": p.broker, "is_kinderdepot": p.is_kinderdepot}
                for p in session.query(PosPortfolio).filter_by(user_id=n["id"]).all()
            ]
        for p in portfolios_n:
            eintrag = {"Nutzer": n["name"], "Depot": p["name"], "Typ": p["typ"], "Broker": p["broker"]}
            if p["is_kinderdepot"]:
                kinder_zeilen.append(eintrag)
            else:
                zeilen.append(eintrag)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Depots (Erwachsene)**")
        st.dataframe(pd.DataFrame(zeilen) if zeilen else pd.DataFrame(columns=["Nutzer", "Depot", "Typ", "Broker"]),
                     use_container_width=True, hide_index=True)
    with c2:
        st.markdown("**Kinderdepots**")
        st.dataframe(pd.DataFrame(kinder_zeilen) if kinder_zeilen else pd.DataFrame(columns=["Nutzer", "Depot", "Typ", "Broker"]),
                     use_container_width=True, hide_index=True)

    st.subheader("Vermögen je Nutzer")
    uebersicht_nutzer = []
    for n in nutzer:
        s = portfolio_module.get_portfolio_summary(n["id"])
        uebersicht_nutzer.append({"Nutzer": n["name"], "Vermögen": s["gesamtvermoegen"]})
    df_verm = pd.DataFrame(uebersicht_nutzer)
    st.dataframe(df_verm.style.format({"Vermögen": _tabellen_safe(fmt_eur)}), use_container_width=True, hide_index=True)

    st.subheader("Gemeinsame Ziele")
    with get_session() as session:
        ziele = [
            {"name": z.name, "fortschritt_pct": z.fortschritt_pct}
            for z in session.query(PosFamilyGoal).all()
        ]
    if ziele:
        for z in ziele:
            st.progress(min(1.0, z["fortschritt_pct"] / 100), text=f"{z['name']}: {fmt_zahl(z['fortschritt_pct'], 0)}%")
    else:
        st.caption("Keine gemeinsamen Ziele hinterlegt.")


# ─────────────────────────────────────────────
# TAB 7 – KI-ANALYSE
# ─────────────────────────────────────────────
with tab7:
    if familien_modus:
        st.info("KI-Analyse läuft je Nutzer – bitte oben einen Nutzer auswählen.")
    else:
        uid = aktiver_user["id"]

        st.subheader("Klumpenrisiko-Analyse")
        if st.button("Analyse anfordern"):
            with st.spinner("Claude analysiert das Portfolio..."):
                ergebnis = llm_analyst.analyze_portfolio(uid)
            st.write(ergebnis["text"])
            if not ergebnis["verfuegbar"]:
                st.caption("(degraded mode – KI nicht erreichbar)")

        st.subheader("Quartalsbericht")
        if st.button("Quartalsbericht anfordern"):
            with st.spinner("Erstelle Quartalsbericht..."):
                report = llm_analyst.generate_quarterly_report(uid)
            st.write(report["text"])

        st.subheader("Frage an die Portfolio-KI")
        if "chat_verlauf" not in st.session_state:
            st.session_state.chat_verlauf = []

        frage = st.text_input("Deine Frage")
        if st.button("Fragen") and frage:
            with st.spinner("Claude denkt nach..."):
                antwort = llm_analyst.answer_portfolio_question(uid, frage)
            st.session_state.chat_verlauf.append((frage, antwort))

        for f, a in reversed(st.session_state.chat_verlauf):
            st.markdown(f"**Du:** {f}")
            st.markdown(f"**KI:** {a}")
            st.divider()

        st.caption(
            "Hinweis: Die KI empfiehlt, keine Anlageberatung. Alle Entscheidungen trifft der Nutzer selbst."
        )


# ─────────────────────────────────────────────
# TAB 8 – VERWALTUNG
# ─────────────────────────────────────────────
with tab8:
    with get_session() as session:
        asset_classes = session.query(PosAssetClass).all()
        ac_options = {ac.name: ac.id for ac in asset_classes}
        portfolios_all = [
            {"id": p.id, "name": p.name, "typ": p.typ, "broker": p.broker, "is_kinderdepot": p.is_kinderdepot}
            for p in session.query(PosPortfolio).filter(PosPortfolio.user_id.in_(aktive_user_ids)).all()
        ]
    pf_options = {f"{p['name']} ({p['typ']})": p for p in portfolios_all}

    # ---- Nutzer verwalten -----------------------------------------
    st.subheader("Nutzer verwalten")
    st.dataframe(pd.DataFrame(nutzer)[["name", "email", "rolle"]], use_container_width=True, hide_index=True)
    with st.form("verwaltung_neuer_nutzer"):
        n_name = st.text_input("Name", key="verw_nutzer_name")
        n_email = st.text_input("E-Mail (optional)", key="verw_nutzer_email")
        n_rolle = st.selectbox("Rolle", ["member", "admin"], key="verw_nutzer_rolle")
        if st.form_submit_button("Nutzer anlegen") and n_name:
            with get_session() as session:
                get_or_create_user(session, n_name, n_email, rolle=n_rolle)
            st.rerun()

    st.divider()

    # ---- Portfolios: anlegen / bearbeiten / löschen ----------------
    st.subheader("Portfolios verwalten")
    col_pf_neu, col_pf_edit = st.columns(2)

    with col_pf_neu:
        st.markdown("**Neues Portfolio**")
        with st.form("neues_portfolio"):
            pf_name = st.text_input("Name", key="pf_name")
            pf_typ = st.selectbox("Typ", PORTFOLIO_TYPEN, key="pf_typ")
            pf_broker = st.text_input("Broker (optional)", key="pf_broker")
            pf_kinderdepot = st.checkbox("Kinderdepot", key="pf_kinderdepot")
            if st.form_submit_button("Anlegen") and pf_name and not familien_modus:
                with get_session() as session:
                    session.add(PosPortfolio(user_id=aktiver_user["id"], name=pf_name, broker=pf_broker,
                                              typ=pf_typ, is_kinderdepot=pf_kinderdepot))
                st.rerun()

    with col_pf_edit:
        st.markdown("**Portfolio bearbeiten / löschen**")
        if pf_options:
            pf_wahl = st.selectbox("Portfolio", list(pf_options.keys()), key="pf_bearbeiten_wahl")
            gewaehltes_pf = pf_options[pf_wahl]
            with st.form("portfolio_bearbeiten"):
                neuer_name = st.text_input("Name", value=gewaehltes_pf["name"], key="pf_edit_name")
                neuer_typ = st.selectbox("Typ", PORTFOLIO_TYPEN,
                                          index=PORTFOLIO_TYPEN.index(gewaehltes_pf["typ"]), key="pf_edit_typ")
                neuer_broker = st.text_input("Broker", value=gewaehltes_pf["broker"] or "", key="pf_edit_broker")
                neuer_kinderdepot = st.checkbox("Kinderdepot", value=gewaehltes_pf["is_kinderdepot"] or False,
                                                 key="pf_edit_kinderdepot")
                if st.form_submit_button("Speichern"):
                    portfolio_module.update_portfolio(gewaehltes_pf["id"], name=neuer_name, typ=neuer_typ,
                                                        broker=neuer_broker, is_kinderdepot=neuer_kinderdepot)
                    st.success("Portfolio aktualisiert")
                    st.rerun()
            if st.button("🗑️ Portfolio löschen", key="pf_loeschen_btn"):
                _dialog_portfolio_loeschen(gewaehltes_pf["id"], gewaehltes_pf["name"])
        else:
            st.caption("Noch kein Portfolio angelegt.")

    st.divider()

    # ---- Transaktion erfassen (mit Ticker-Suche + Assetklasse) -----
    st.subheader("Transaktion erfassen")

    if "tx_kandidaten" not in st.session_state:
        st.session_state.tx_kandidaten = []
    if "tx_ticker_bestaetigt" not in st.session_state:
        st.session_state.tx_ticker_bestaetigt = None

    # Schritt 1: Ticker/ISIN eingeben und via yfinance auflösen lassen
    # (außerhalb des st.form, da Formulare in Streamlit erst beim Submit
    # neu rendern – für die Zwischenbestätigung brauchen wir sofortige Reruns).
    tx_ticker_eingabe = st.text_input("Ticker oder ISIN eingeben", key="tx_ticker_eingabe")
    if st.button("🔎 Ticker suchen", key="tx_ticker_suchen_btn"):
        with st.spinner("Suche Ticker via yfinance..."):
            kandidaten = portfolio_module.resolve_ticker(tx_ticker_eingabe)
        st.session_state.tx_kandidaten = kandidaten
        st.session_state.tx_ticker_bestaetigt = None
        if not kandidaten:
            st.warning(f"Kein Ticker für „{tx_ticker_eingabe}“ gefunden (auch nicht mit .DE-Suffix oder Volltextsuche).")

    # Schritt 2: Gefundenen Kandidaten dem Nutzer zur Bestätigung anzeigen
    if st.session_state.tx_kandidaten:
        optionen = {
            f"{k['symbol']} — {k['name']} ({k['exchange']})": k
            for k in st.session_state.tx_kandidaten
        }
        wahl = st.selectbox("Gefunden – bitte bestätigen", list(optionen.keys()), key="tx_kandidat_wahl")
        if st.button("✅ Ticker bestätigen", key="tx_ticker_bestaetigen_btn"):
            st.session_state.tx_ticker_bestaetigt = optionen[wahl]["symbol"]
            st.session_state.tx_kandidaten = []
            st.rerun()

    # Schritt 3: Erst NACH Bestätigung die eigentliche Buchungsmaske zeigen
    if st.session_state.tx_ticker_bestaetigt:
        st.success(f"Aktiver Ticker für die Buchung: **{st.session_state.tx_ticker_bestaetigt}**")
        with st.form("neue_transaktion"):
            tx_pf = st.selectbox("Portfolio", list(pf_options.keys()) if pf_options else ["(erst Portfolio anlegen)"])
            tx_typ = st.selectbox("Typ", ["kauf", "verkauf", "dividende", "sparrate"])
            tx_asset_class = st.selectbox("Assetklasse", list(ac_options.keys()) if ac_options else ["(keine vorhanden)"])
            tx_qty = st.number_input("Menge", min_value=0.0, step=1.0)
            tx_price = st.number_input("Preis", min_value=0.0, step=0.01)
            tx_fees = st.number_input("Gebühren", min_value=0.0, step=0.01)
            tx_datum = st.date_input("Datum", value=date.today())
            if st.form_submit_button("Buchen") and pf_options and tx_qty > 0:
                try:
                    portfolio_module.add_transaction(
                        pf_options[tx_pf]["id"], tx_typ, st.session_state.tx_ticker_bestaetigt,
                        tx_qty, tx_price, tx_datum, tx_fees,
                        asset_class_id=ac_options.get(tx_asset_class),
                    )
                    st.success("Transaktion gebucht")
                    st.session_state.tx_ticker_bestaetigt = None
                except Exception as e:
                    st.error(f"Fehler: {e}")
    else:
        st.caption("Bitte zuerst einen Ticker suchen und bestätigen.")

    st.divider()

    # ---- CSV-Import --------------------------------------------------
    st.subheader("CSV-Import")
    with st.form("csv_import"):
        csv_pf = st.selectbox("Portfolio ", list(pf_options.keys()) if pf_options else ["(erst Portfolio anlegen)"], key="csv_pf")
        csv_broker = st.selectbox("Broker", ["comdirect", "tr"])
        csv_file = st.file_uploader("CSV-Datei", type=["csv"])
        if st.form_submit_button("Importieren") and pf_options and csv_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                tmp.write(csv_file.getvalue())
                tmp_path = tmp.name
            try:
                ergebnis = portfolio_module.import_csv(pf_options[csv_pf]["id"], tmp_path, csv_broker)
                st.success(f"{ergebnis['imported']} importiert, {ergebnis['skipped']} übersprungen")
            except Exception as e:
                st.error(f"Fehler: {e}")
            finally:
                os.unlink(tmp_path)

    st.divider()

    # ---- Positionen bearbeiten / löschen / Transaktionen löschen -----
    st.subheader("Positionen bearbeiten / löschen")
    alle_pos_verwaltung = []
    for uid in aktive_user_ids:
        alle_pos_verwaltung.extend(portfolio_module.get_positions(uid))

    if alle_pos_verwaltung:
        pos_options = {f"{p['name']} ({p['portfolio_name']})": p for p in alle_pos_verwaltung}
        pos_wahl = st.selectbox("Position", list(pos_options.keys()), key="pos_loeschen_wahl")
        gewaehlte_pos = pos_options[pos_wahl]

        # Bei Positionswechsel im Dropdown alte "edit_*"-Widget-States verwerfen –
        # sonst zeigt Streamlit (Widget-State bleibt über den key hinweg bestehen
        # und ignoriert `value=` bei Reruns) für die neu gewählte Position weiterhin
        # die Formularwerte der zuvor bearbeiteten Position an.
        if st.session_state.get("last_edit_position") != gewaehlte_pos["id"]:
            st.session_state["last_edit_position"] = gewaehlte_pos["id"]
            for key in list(st.session_state.keys()):
                if key.startswith("edit_"):
                    del st.session_state[key]

        col_edit, col_del = st.columns(2)
        with col_edit:
            with st.expander("✏️ Bearbeiten"):
                with st.form("position_bearbeiten"):
                    edit_display_name = st.text_input(
                        "Anzeigename", value=gewaehlte_pos["display_name"] or "",
                        key=f"edit_name_{gewaehlte_pos['id']}")
                    edit_ticker = st.text_input(
                        "Ticker", value=gewaehlte_pos["ticker"], key=f"edit_ticker_{gewaehlte_pos['id']}")
                    ac_keys = list(ac_options.keys())
                    default_idx = ac_keys.index(gewaehlte_pos["asset_class"]) if gewaehlte_pos["asset_class"] in ac_keys else 0
                    edit_asset_class = st.selectbox(
                        "Assetklasse", ac_keys if ac_keys else ["(keine vorhanden)"],
                        index=default_idx, key=f"edit_assetclass_{gewaehlte_pos['id']}")
                    edit_quantity = st.number_input(
                        "Anzahl", min_value=0.0, step=1.0,
                        value=float(gewaehlte_pos["quantity"] or 0.0), key=f"edit_quantity_{gewaehlte_pos['id']}")
                    edit_avg_price = st.number_input(
                        "Ø-Kaufpreis", min_value=0.0, step=0.01,
                        value=float(gewaehlte_pos["avg_buy_price"] or 0.0), key=f"edit_price_{gewaehlte_pos['id']}")
                    if st.form_submit_button("Speichern"):
                        try:
                            portfolio_module.update_position(
                                gewaehlte_pos["id"],
                                display_name=edit_display_name,
                                ticker=edit_ticker,
                                asset_class_id=ac_options.get(edit_asset_class),
                                quantity=edit_quantity,
                                avg_buy_price=edit_avg_price,
                            )
                            st.success("Position aktualisiert")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Fehler: {e}")
        with col_del:
            if st.button("🗑️ Position löschen (inkl. aller Transaktionen)", key="pos_loeschen_btn"):
                _dialog_position_loeschen(gewaehlte_pos["id"], gewaehlte_pos["name"])

        with get_session() as session:
            txs = [
                {"id": t.id, "typ": t.typ, "datum": t.datum, "quantity": t.quantity, "price": t.price, "fees": t.fees}
                for t in session.query(PosTransaction)
                .filter_by(position_id=gewaehlte_pos["id"])
                .order_by(PosTransaction.datum.desc())
                .all()
            ]
        if txs:
            st.markdown(f"**Transaktionen von {gewaehlte_pos['name']}**")
            for t in txs:
                col_info, col_del = st.columns([5, 1])
                col_info.write(f"{t['datum']} · {t['typ']} · {fmt_menge(t['quantity'])} @ {fmt_eur(t['price'])} "
                                f"(Gebühren {fmt_eur(t['fees'])})")
                if col_del.button("🗑️", key=f"tx_del_{t['id']}"):
                    portfolio_module.delete_transaction(t["id"])
                    st.rerun()
        else:
            st.caption("Keine Transaktionen für diese Position.")
    else:
        st.caption("Keine Positionen vorhanden.")

    st.divider()

    # ---- Zielgewichtungen ---------------------------------------------
    if not familien_modus:
        st.subheader("Ziel-Gewichtung")
        with st.form("ziel_gewichtung"):
            tw_class = st.selectbox("Assetklasse", list(ac_options.keys()))
            tw_target = st.slider("Ziel-%", 0, 100, 20)
            tw_min = st.slider("Min-%", 0, 100, max(0, tw_target - 5))
            tw_max = st.slider("Max-%", 0, 100, min(100, tw_target + 5))
            if st.form_submit_button("Speichern"):
                with get_session() as session:
                    existing = session.query(PosTargetWeight).filter_by(
                        user_id=aktiver_user["id"], asset_class_id=ac_options[tw_class]
                    ).first()
                    if existing:
                        existing.target_pct, existing.min_pct, existing.max_pct = tw_target / 100, tw_min / 100, tw_max / 100
                    else:
                        session.add(PosTargetWeight(
                            user_id=aktiver_user["id"], asset_class_id=ac_options[tw_class],
                            target_pct=tw_target / 100, min_pct=tw_min / 100, max_pct=tw_max / 100,
                        ))
                st.success("Ziel-Gewichtung gespeichert")

        st.divider()

        # ---- Immobilie anlegen -----------------------------------------
        st.subheader("Immobilie anlegen")
        st.caption("Für Abschreibung, Finanzierung & Kreditvertrag-KI siehe Tab 🏠 Immobilie.")
        with st.form("neue_immobilie"):
            im_adresse = st.text_input("Adresse")
            im_kaufpreis = st.number_input("Kaufpreis", min_value=0.0, step=1000.0)
            im_kaufjahr = st.number_input("Kaufjahr", min_value=1950, max_value=date.today().year, value=date.today().year)
            im_qm = st.number_input("Wohnfläche (qm)", min_value=0.0, step=1.0)
            im_ek = st.number_input("Eigenkapital", min_value=0.0, step=1000.0)
            im_restschuld = st.number_input("Restschuld", min_value=0.0, step=1000.0)
            im_rate = st.number_input("Monatliche Rate", min_value=0.0, step=10.0)
            im_miete = st.number_input("Monatliche Mieteinnahmen", min_value=0.0, step=10.0)
            if st.form_submit_button("Anlegen"):
                if not im_adresse:
                    st.error("Bitte eine Adresse angeben.")
                else:
                    try:
                        save_real_estate(
                            user_id=aktiver_user["id"], adresse=im_adresse, kaufpreis=im_kaufpreis,
                            kaufjahr=int(im_kaufjahr), wohnflaeche_qm=im_qm, eigenkapital=im_ek,
                            restschuld=im_restschuld, monatliche_rate=im_rate, mieteinnahmen=im_miete,
                            letzter_schaetzwert=im_kaufpreis, letztes_update=datetime.utcnow(),
                        )
                        st.success("Immobilie gespeichert!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Fehler beim Speichern: {e}")

        st.divider()

    # ---- Familienziel anlegen -----------------------------------------
    st.subheader("Familienziel anlegen")
    with st.form("neues_ziel"):
        z_name = st.text_input("Name (z.B. Notgroschen, Kinderstudium)")
        z_betrag = st.number_input("Zielbetrag", min_value=0.0, step=100.0)
        z_aktuell = st.number_input("Aktueller Stand", min_value=0.0, step=100.0)
        z_datum = st.date_input("Zieldatum", value=None)
        if st.form_submit_button("Anlegen") and z_name:
            with get_session() as session:
                session.add(PosFamilyGoal(name=z_name, ziel_betrag=z_betrag, aktuell_betrag=z_aktuell, zieldatum=z_datum))
            st.rerun()
