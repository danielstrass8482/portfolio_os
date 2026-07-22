"""
dashboard.py – Streamlit-Dashboard für Portfolio-OS.
7 Tabs: Übersicht, Positionen, Rebalancing, Steuer, Immobilie, Familie, KI-Analyse.

Datenzugriff läuft ausschließlich über portfolio.py / tax_engine.py /
rebalancing.py / llm_analyst.py – dashboard.py enthält keine eigene
Geschäftslogik, nur Darstellung und einfache Verwaltungsformulare
(Nutzer/Portfolio/Position anlegen), die für den Bootstrap nötig sind.
"""

from datetime import date, datetime

import streamlit as st
import pandas as pd
import plotly.express as px

from config import validate_config, BASE_URL
from database import (
    init_db, get_session, get_or_create_user,
    PosUser, PosPortfolio, PosAssetClass, PosTargetWeight,
    PosRealEstate, PosFamilyGoal,
)
import portfolio as portfolio_module
import tax_engine
import rebalancing
import llm_analyst

# ── PAGE CONFIG ────────────────────────────────────────────────────
st.set_page_config(
    page_title="Portfolio-OS",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_db()

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
# BOOTSTRAP: NUTZERAUSWAHL
# ─────────────────────────────────────────────

def _alle_nutzer():
    with get_session() as session:
        return [{"id": u.id, "name": u.name, "rolle": u.rolle} for u in session.query(PosUser).all()]


nutzer = _alle_nutzer()

st.sidebar.title("💼 Portfolio-OS")

if not nutzer:
    st.sidebar.warning("Noch kein Nutzer angelegt.")
    with st.sidebar.form("neuer_erstnutzer"):
        name = st.text_input("Name")
        email = st.text_input("E-Mail (optional)")
        if st.form_submit_button("Nutzer anlegen") and name:
            with get_session() as session:
                get_or_create_user(session, name, email, rolle="admin")
            st.rerun()
    st.stop()

nutzer_namen = [n["name"] for n in nutzer]
familien_modus = st.sidebar.toggle("👨‍👩‍👧 Familien-Portfolio (alle Nutzer)", value=False)

if not familien_modus:
    gewaehlter_name = st.sidebar.selectbox("Portfolio von", nutzer_namen)
    aktiver_user = next(n for n in nutzer if n["name"] == gewaehlter_name)
    aktive_user_ids = [aktiver_user["id"]]
else:
    aktiver_user = None
    aktive_user_ids = [n["id"] for n in nutzer]

warnungen = validate_config()
if warnungen:
    with st.sidebar.expander("⚠️ Konfigurationshinweise"):
        for w in warnungen:
            st.caption(w)

if st.sidebar.button("🔄 Kurse aktualisieren"):
    with st.spinner("Aktualisiere Kurse via yfinance..."):
        n = portfolio_module.update_prices()
    st.sidebar.success(f"{n} Position(en) aktualisiert")


# ─────────────────────────────────────────────
# VERWALTUNG (Bootstrap-Formulare für Grunddaten)
# ─────────────────────────────────────────────

with st.sidebar.expander("⚙️ Verwaltung"):
    with get_session() as session:
        asset_classes = session.query(PosAssetClass).all()
        ac_options = {ac.name: ac.id for ac in asset_classes}
        portfolios_all = session.query(PosPortfolio).filter(
            PosPortfolio.user_id.in_(aktive_user_ids)
        ).all()
        pf_options = {f"{p.name} ({p.typ})": p.id for p in portfolios_all}

    st.markdown("**Neues Portfolio**")
    with st.form("neues_portfolio"):
        pf_name = st.text_input("Name", key="pf_name")
        pf_typ = st.selectbox("Typ", ["depot", "krypto", "immobilie", "konto"], key="pf_typ")
        pf_broker = st.text_input("Broker (optional)", key="pf_broker")
        if st.form_submit_button("Anlegen") and pf_name and not familien_modus:
            with get_session() as session:
                session.add(PosPortfolio(user_id=aktiver_user["id"], name=pf_name, broker=pf_broker, typ=pf_typ))
            st.rerun()

    st.markdown("**Transaktion erfassen**")
    with st.form("neue_transaktion"):
        tx_pf = st.selectbox("Portfolio", list(pf_options.keys()) if pf_options else ["(erst Portfolio anlegen)"])
        tx_typ = st.selectbox("Typ", ["kauf", "verkauf", "dividende", "sparrate"])
        tx_ticker = st.text_input("Ticker/ISIN")
        tx_qty = st.number_input("Menge", min_value=0.0, step=1.0)
        tx_price = st.number_input("Preis", min_value=0.0, step=0.01)
        tx_fees = st.number_input("Gebühren", min_value=0.0, step=0.01)
        tx_datum = st.date_input("Datum", value=date.today())
        if st.form_submit_button("Buchen") and pf_options and tx_ticker and tx_qty > 0:
            try:
                portfolio_module.add_transaction(
                    pf_options[tx_pf], tx_typ, tx_ticker.upper(), tx_qty, tx_price, tx_datum, tx_fees
                )
                st.success("Transaktion gebucht")
            except Exception as e:
                st.error(f"Fehler: {e}")

    st.markdown("**CSV-Import**")
    with st.form("csv_import"):
        csv_pf = st.selectbox("Portfolio ", list(pf_options.keys()) if pf_options else ["(erst Portfolio anlegen)"], key="csv_pf")
        csv_broker = st.selectbox("Broker", ["comdirect", "tr"])
        csv_file = st.file_uploader("CSV-Datei", type=["csv"])
        if st.form_submit_button("Importieren") and pf_options and csv_file:
            import tempfile, os
            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                tmp.write(csv_file.getvalue())
                tmp_path = tmp.name
            try:
                ergebnis = portfolio_module.import_csv(pf_options[csv_pf], tmp_path, csv_broker)
                st.success(f"{ergebnis['imported']} importiert, {ergebnis['skipped']} übersprungen")
            except Exception as e:
                st.error(f"Fehler: {e}")
            finally:
                os.unlink(tmp_path)

    if not familien_modus:
        st.markdown("**Ziel-Gewichtung**")
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

        st.markdown("**Immobilie anlegen**")
        with st.form("neue_immobilie"):
            im_adresse = st.text_input("Adresse")
            im_kaufpreis = st.number_input("Kaufpreis", min_value=0.0, step=1000.0)
            im_kaufjahr = st.number_input("Kaufjahr", min_value=1950, max_value=date.today().year, value=date.today().year)
            im_qm = st.number_input("Wohnfläche (qm)", min_value=0.0, step=1.0)
            im_ek = st.number_input("Eigenkapital", min_value=0.0, step=1000.0)
            im_restschuld = st.number_input("Restschuld", min_value=0.0, step=1000.0)
            im_rate = st.number_input("Monatliche Rate", min_value=0.0, step=10.0)
            im_miete = st.number_input("Monatliche Mieteinnahmen", min_value=0.0, step=10.0)
            if st.form_submit_button("Anlegen") and im_adresse:
                with get_session() as session:
                    session.add(PosRealEstate(
                        user_id=aktiver_user["id"], adresse=im_adresse, kaufpreis=im_kaufpreis,
                        kaufjahr=int(im_kaufjahr), wohnflaeche_qm=im_qm, eigenkapital=im_ek,
                        restschuld=im_restschuld, monatliche_rate=im_rate, mieteinnahmen=im_miete,
                        letzter_schaetzwert=im_kaufpreis, letztes_update=datetime.utcnow(),
                    ))
                st.rerun()

    st.markdown("**Familienziel anlegen**")
    with st.form("neues_ziel"):
        z_name = st.text_input("Name (z.B. Notgroschen, Kinderstudium)")
        z_betrag = st.number_input("Zielbetrag", min_value=0.0, step=100.0)
        z_aktuell = st.number_input("Aktueller Stand", min_value=0.0, step=100.0)
        z_datum = st.date_input("Zieldatum", value=None)
        if st.form_submit_button("Anlegen") and z_name:
            with get_session() as session:
                session.add(PosFamilyGoal(name=z_name, ziel_betrag=z_betrag, aktuell_betrag=z_aktuell, zieldatum=z_datum))
            st.rerun()


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


st.title("💼 Portfolio-OS")
st.caption(f"{'Familien-Portfolio' if familien_modus else aktiver_user['name']} · {BASE_URL}")

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "📊 Übersicht", "📋 Positionen", "⚖️ Rebalancing", "🧾 Steuer",
    "🏠 Immobilie", "👨‍👩‍👧‍👦 Familie", "🤖 KI-Analyse",
])

# ─────────────────────────────────────────────
# TAB 1 – ÜBERSICHT
# ─────────────────────────────────────────────
with tab1:
    summary = _kombinierte_summary(aktive_user_ids) if familien_modus else portfolio_module.get_portfolio_summary(aktive_user_ids[0])

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f'<div class="kpi-card"><div class="kpi-label">Gesamtvermögen</div>'
                    f'<div class="kpi-value neutral">{summary["gesamtvermoegen"]:,.2f} €</div></div>', unsafe_allow_html=True)
    with c2:
        klasse = "positive" if summary["unrealized_pnl"] >= 0 else "negative"
        st.markdown(f'<div class="kpi-card"><div class="kpi-label">Unrealisierter G/V</div>'
                    f'<div class="kpi-value {klasse}">{summary["unrealized_pnl"]:+,.2f} €</div></div>', unsafe_allow_html=True)
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
            st.info("Noch keine Positionen erfasst – siehe ⚙️ Verwaltung in der Sidebar.")

    with col_top:
        st.subheader("Top-Gewinner / Top-Verlierer")
        alle_pos = []
        for uid in aktive_user_ids:
            alle_pos.extend(portfolio_module.get_positions(uid))
        bewertbar = [p for p in alle_pos if p["unrealized_pnl_pct"] is not None]
        if bewertbar:
            top_gewinner = sorted(bewertbar, key=lambda p: -p["unrealized_pnl_pct"])[:3]
            top_verlierer = sorted(bewertbar, key=lambda p: p["unrealized_pnl_pct"])[:3]
            st.caption("🏆 Gewinner")
            for p in top_gewinner:
                st.markdown(f"**{p['ticker']}** — {p['unrealized_pnl_pct']:+.1f}%")
            st.caption("📉 Verlierer")
            for p in top_verlierer:
                st.markdown(f"**{p['ticker']}** — {p['unrealized_pnl_pct']:+.1f}%")
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
                        text=f"{z['name']}: {z['fortschritt_pct']:.0f}% ({z['aktuell_betrag']:,.0f}/{z['ziel_betrag']:,.0f} €)")
            if z["fortschritt_pct"] >= 100:
                st.success(f"🎉 Ziel „{z['name']}“ erreicht!")
    else:
        st.caption("Keine Familienziele hinterlegt (siehe ⚙️ Verwaltung).")


# ─────────────────────────────────────────────
# TAB 2 – POSITIONEN
# ─────────────────────────────────────────────
with tab2:
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
            "ticker", "name", "asset_class", "portfolio_name", "quantity",
            "avg_buy_price", "current_price", "market_value", "unrealized_pnl", "unrealized_pnl_pct",
        ]].rename(columns={
            "ticker": "Ticker", "name": "Name", "asset_class": "Assetklasse", "portfolio_name": "Depot",
            "quantity": "Menge", "avg_buy_price": "Ø-Kaufpreis", "current_price": "Kurs",
            "market_value": "Wert", "unrealized_pnl": "G/V", "unrealized_pnl_pct": "G/V %",
        })
        st.dataframe(anzeige, use_container_width=True, hide_index=True)

        st.markdown("**Steuervorschau** _(Streamlit unterstützt kein echtes Hover – daher als Auswahl)_")
        ticker_wahl = st.selectbox("Position wählen", gefiltert["ticker"].tolist())
        gewaehlte_pos = next(p for p in alle_pos if p["ticker"] == ticker_wahl)
        if st.button("Steuervorschau anzeigen"):
            try:
                preview = tax_engine.get_tax_preview(gewaehlte_pos["id"], gewaehlte_pos["current_price"] or gewaehlte_pos["avg_buy_price"])
                st.json(preview)
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
            st.info("Keine Ziel-Gewichtung hinterlegt (siehe ⚙️ Verwaltung).")

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
            from database import PosTaxConfig
            cfg = session.query(PosTaxConfig).filter_by(user_id=uid).first()
            freibetrag = cfg.freistellungsauftrag if cfg else 0.0
            genutzt = cfg.freistellungsgenutzt if cfg else 0.0

        st.subheader("Freistellungsauftrag")
        st.progress(min(1.0, genutzt / freibetrag) if freibetrag else 0.0,
                    text=f"{genutzt:,.2f} / {freibetrag:,.2f} € genutzt (Rest: {rest:,.2f} €)")

        st.subheader(f"Realisierte Gewinne/Verluste {date.today().year} (YTD)")
        uebersicht = tax_engine.generate_jahresuebersicht(uid, date.today().year)
        c1, c2, c3 = st.columns(3)
        c1.metric("Gewinne", f"{uebersicht['realisierte_gewinne']:,.2f} €")
        c2.metric("Verluste", f"{uebersicht['realisierte_verluste']:,.2f} €")
        c3.metric("Gezahlte Steuer", f"{uebersicht['steuer_gezahlt']:,.2f} €")

        st.subheader("Tax-Loss-Harvesting-Kandidaten")
        kandidaten = tax_engine.find_tax_loss_harvesting(uid)
        if kandidaten:
            st.dataframe(pd.DataFrame(kandidaten), use_container_width=True, hide_index=True)
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
                for label, wert in [
                    ("Nutzer", aktiver_user["name"]),
                    ("Realisierte Gewinne", f"{uebersicht_jahr['realisierte_gewinne']:.2f} EUR"),
                    ("Realisierte Verluste", f"{uebersicht_jahr['realisierte_verluste']:.2f} EUR"),
                    ("Netto-Ergebnis", f"{uebersicht_jahr['netto_ergebnis']:.2f} EUR"),
                    ("Gezahlte Steuer", f"{uebersicht_jahr['steuer_gezahlt']:.2f} EUR"),
                    ("Freistellungsauftrag", f"{uebersicht_jahr['freistellungsauftrag']:.2f} EUR"),
                    ("Freistellung genutzt (aktuell)", f"{uebersicht_jahr['freistellung_genutzt_aktuell']:.2f} EUR"),
                    ("Verlusttopf (aktuell)", f"{uebersicht_jahr['verlusttopf_aktuell']:.2f} EUR"),
                ]:
                    pdf.cell(0, 8, f"{label}: {wert}", ln=True)
                pdf_bytes = bytes(pdf.output())
                st.download_button("⬇️ Download PDF", data=pdf_bytes,
                                    file_name=f"jahresuebersicht_{int(jahr_wahl)}.pdf", mime="application/pdf")
            except Exception as e:
                st.error(f"PDF-Erstellung fehlgeschlagen: {e}")


# ─────────────────────────────────────────────
# TAB 5 – IMMOBILIE
# ─────────────────────────────────────────────
with tab5:
    with get_session() as session:
        immobilien = session.query(PosRealEstate).filter(PosRealEstate.user_id.in_(aktive_user_ids)).all()
        immobilien_data = [{
            "id": i.id, "adresse": i.adresse, "kaufpreis": i.kaufpreis, "kaufjahr": i.kaufjahr,
            "eigenkapital": i.eigenkapital, "restschuld": i.restschuld, "monatliche_rate": i.monatliche_rate,
            "mieteinnahmen": i.mieteinnahmen, "letzter_schaetzwert": i.letzter_schaetzwert,
            "letztes_update": i.letztes_update,
        } for i in immobilien]

    if not immobilien_data:
        st.info("Keine Immobilie hinterlegt (siehe ⚙️ Verwaltung).")
    else:
        for im in immobilien_data:
            st.subheader(im["adresse"])
            schaetzwert = im["letzter_schaetzwert"] or im["kaufpreis"]
            ltv = (im["restschuld"] / schaetzwert) if schaetzwert else 0.0
            # Eigenkapitalrendite (Cash-on-Cash, vereinfacht): jährlicher Cashflow / eingesetztes Eigenkapital
            jahres_cashflow = (im["mieteinnahmen"] - im["monatliche_rate"]) * 12
            ek_rendite = (jahres_cashflow / im["eigenkapital"]) if im["eigenkapital"] else 0.0

            c1, c2, c3 = st.columns(3)
            c1.metric("Letzter Schätzwert", f"{schaetzwert:,.0f} €")
            c2.metric("LTV-Ratio", f"{ltv*100:.1f}%")
            c3.metric("Eigenkapitalrendite (Cash-on-Cash)", f"{ek_rendite*100:.1f}%")

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
                {"name": p.name, "typ": p.typ, "broker": p.broker}
                for p in session.query(PosPortfolio).filter_by(user_id=n["id"]).all()
            ]
        for p in portfolios_n:
            eintrag = {"Nutzer": n["name"], "Depot": p["name"], "Typ": p["typ"], "Broker": p["broker"]}
            if "kind" in p["name"].lower():
                kinder_zeilen.append(eintrag)
            else:
                zeilen.append(eintrag)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Depots (Erwachsene)**")
        st.dataframe(pd.DataFrame(zeilen) if zeilen else pd.DataFrame(columns=["Nutzer", "Depot", "Typ", "Broker"]),
                     use_container_width=True, hide_index=True)
    with c2:
        st.markdown("**Kinderdepots** _(Depotname enthält „Kind“)_")
        st.dataframe(pd.DataFrame(kinder_zeilen) if kinder_zeilen else pd.DataFrame(columns=["Nutzer", "Depot", "Typ", "Broker"]),
                     use_container_width=True, hide_index=True)

    st.subheader("Vermögen je Nutzer")
    uebersicht_nutzer = []
    for n in nutzer:
        s = portfolio_module.get_portfolio_summary(n["id"])
        uebersicht_nutzer.append({"Nutzer": n["name"], "Rolle": n["rolle"], "Vermögen": s["gesamtvermoegen"]})
    st.dataframe(pd.DataFrame(uebersicht_nutzer), use_container_width=True, hide_index=True)

    st.subheader("Gemeinsame Ziele")
    with get_session() as session:
        ziele = [
            {"name": z.name, "fortschritt_pct": z.fortschritt_pct}
            for z in session.query(PosFamilyGoal).all()
        ]
    if ziele:
        for z in ziele:
            st.progress(min(1.0, z["fortschritt_pct"] / 100), text=f"{z['name']}: {z['fortschritt_pct']:.0f}%")
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
