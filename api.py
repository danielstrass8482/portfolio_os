"""
api.py – FastAPI-Schnittstelle für das React-Frontend (portfolio_react).
Dünne Schicht über den bestehenden Modulen (portfolio.py / tax_engine.py /
rebalancing.py / trading_bot_connector.py / llm_analyst.py / database.py) –
enthält selbst keine Geschäftslogik, nur Request/Response-Mapping.

Läuft parallel zum bestehenden Streamlit-Dashboard (Port 8502), das bis zur
vollständigen React-Umstellung weiterläuft. Port 8503.
"""

from datetime import date, datetime
from typing import Optional

import yfinance as yf
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

import portfolio as portfolio_module
import tax_engine
import rebalancing
import trading_bot_connector
import llm_analyst
import kontoauszug_analyzer
from config import validate_config, BASE_URL
from database import (
    get_session, engine, PosUser, PosRealEstate, PosFamilyGoal, PosGoal,
    PosPortfolio, PosPosition, PosTransaction, PosAssetClass, PosBuchung,
    PosTargetWeight, get_or_create_user, save_buchungen, add_kategorisierungsregel,
)

app = FastAPI(title="Portfolio-OS API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


KATEGORIEN = [
    "Wohnen", "Lebensmittel", "Mobilität", "Restaurant", "Abonnements",
    "Gesundheit", "Versicherung", "Sparen", "Gehalt", "Sonstiges",
]


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _vix_status() -> dict:
    """Leichtgewichtiger VIX-Lookup direkt via yfinance (kein Cross-Repo-Import
    von trading_bot – Portfolio-OS hat keine eigene VIX-Funktion). Degraded
    mode bei Fehler statt Absturz."""
    try:
        vix = float(yf.Ticker("^VIX").fast_info.get("lastPrice"))
        return {"verfuegbar": True, "vix": round(vix, 1), "ok": vix <= 30}
    except Exception:
        return {"verfuegbar": False, "vix": None, "ok": None}


def _erstes_ziel(user_id: int) -> Optional[dict]:
    """Erstes persönliches Ziel eines Nutzers samt Fortschritt – analog zur
    'Meine Ziele'-Sektion in dashboard.py, aber nur das erste Ziel für die
    Übersicht-KPI-Karte."""
    with get_session() as session:
        user = session.get(PosUser, user_id)
        ziel = session.query(PosGoal).filter_by(user_id=user_id).order_by(PosGoal.id).first()
        if not ziel or not user:
            return None
        return {
            "id": ziel.id,
            "name": ziel.name,
            "zielbetrag": ziel.zielbetrag,
            "typ": ziel.typ,
        }


def _user_or_404(user_id: int) -> PosUser:
    with get_session() as session:
        user = session.get(PosUser, user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"Nutzer {user_id} nicht gefunden")
        return {"id": user.id, "name": user.name, "rolle": user.rolle}


# ─────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────

@app.get("/api/users")
def list_users():
    with get_session() as session:
        return [
            {"id": u.id, "name": u.name, "email": u.email, "rolle": u.rolle}
            for u in session.query(PosUser).all()
        ]


@app.post("/api/users")
def create_user(payload: dict):
    with get_session() as session:
        user = get_or_create_user(
            session, payload["name"], payload.get("email"), rolle=payload.get("rolle", "member"),
        )
        return {"id": user.id}


@app.put("/api/users/{user_id}")
def update_user(user_id: int, payload: dict):
    with get_session() as session:
        user = session.get(PosUser, user_id)
        if not user:
            raise HTTPException(status_code=404, detail=f"Nutzer {user_id} nicht gefunden")
        if "name" in payload:
            user.name = payload["name"]
        if "email" in payload:
            user.email = payload["email"] or None
        if "rolle" in payload:
            user.rolle = payload["rolle"]
        return {"id": user.id}


# ─────────────────────────────────────────────
# ÜBERSICHT
# ─────────────────────────────────────────────

@app.get("/api/overview")
def overview(user_id: Optional[int] = None, family: bool = False):
    """family=true aggregiert über alle Nutzer (Trading-Bot-Wert wird dabei nur
    EINMAL gezählt, nicht pro Nutzer – siehe get_total_wealth-Docstring)."""
    if family:
        with get_session() as session:
            user_ids = [u.id for u in session.query(PosUser).all()]
        gesamt = {
            "gesamtvermoegen": 0.0, "unrealized_pnl": 0.0,
            "positions_count": 0, "portfolios_count": 0, "asset_breakdown": {},
        }
        for uid in user_ids:
            s = portfolio_module.get_total_wealth(uid, include_trading_bot=False)
            gesamt["gesamtvermoegen"] += s["gesamtvermoegen"]
            gesamt["unrealized_pnl"] += s["unrealized_pnl"]
            gesamt["positions_count"] += s["positions_count"]
            gesamt["portfolios_count"] += s["portfolios_count"]
            for klass, wert in s["asset_breakdown"].items():
                gesamt["asset_breakdown"][klass] = gesamt["asset_breakdown"].get(klass, 0.0) + wert
        bot_info = trading_bot_connector.get_bot_account_value_eur()
        if bot_info["total_eur"]:
            gesamt["gesamtvermoegen"] += bot_info["total_eur"]
        summary = gesamt
        ziel = None
    else:
        if user_id is None:
            raise HTTPException(status_code=400, detail="user_id erforderlich wenn family=false")
        _user_or_404(user_id)
        summary = portfolio_module.get_total_wealth(user_id)
        ziel = _erstes_ziel(user_id)

    kosten_basis = summary["gesamtvermoegen"] - summary["unrealized_pnl"]
    rendite_pct = (summary["unrealized_pnl"] / kosten_basis * 100) if kosten_basis else 0.0

    with get_session() as session:
        open_positions = portfolio_module.get_positions(user_id) if user_id else []

    return {
        "gesamtvermoegen": summary["gesamtvermoegen"],
        "unrealized_pnl": summary["unrealized_pnl"],
        "rendite_pct": rendite_pct,
        "positions_count": summary["positions_count"],
        "portfolios_count": summary["portfolios_count"],
        "asset_breakdown": summary["asset_breakdown"],
        "ziel": ziel,
        "vix": _vix_status(),
        "open_positions": [p for p in open_positions if p["quantity"]],
    }


# ─────────────────────────────────────────────
# POSITIONEN
# ─────────────────────────────────────────────

@app.get("/api/positions")
def positions(user_id: int):
    _user_or_404(user_id)
    depot = portfolio_module.get_positions(user_id)
    bot_detail = trading_bot_connector.get_bot_positions_detail()
    return {"depot": depot, "bot": bot_detail}


@app.post("/api/positions/refresh-prices")
def refresh_prices():
    n = portfolio_module.update_prices()
    return {"aktualisiert": n}


@app.put("/api/positions/{position_id}")
def edit_position(position_id: int, payload: dict):
    asset_class_id = payload.get("asset_class_id")
    portfolio_module.update_position(
        position_id,
        display_name=payload.get("display_name"),
        ticker=payload.get("ticker"),
        asset_class_id=asset_class_id,
        quantity=payload.get("quantity"),
        avg_buy_price=payload.get("avg_buy_price"),
    )
    return {"ok": True}


@app.delete("/api/positions/{position_id}")
def remove_position(position_id: int):
    portfolio_module.delete_position(position_id)
    return {"ok": True}


@app.get("/api/tax-preview")
def tax_preview(position_id: int, verkauf_preis: float, quantity: Optional[float] = None):
    return tax_engine.get_tax_preview(position_id, verkauf_preis, quantity)


# ─────────────────────────────────────────────
# STEUER
# ─────────────────────────────────────────────

@app.get("/api/tax")
def tax(user_id: int):
    _user_or_404(user_id)
    return {
        "freistellung_rest": tax_engine.get_remaining_freistellung(user_id),
        "harvesting": tax_engine.find_tax_loss_harvesting(user_id),
        "jahresuebersicht": tax_engine.generate_jahresuebersicht(user_id, date.today().year),
    }


# ─────────────────────────────────────────────
# TRADING BOT
# ─────────────────────────────────────────────

@app.get("/api/trading-bot")
def trading_bot_overview():
    account = trading_bot_connector.get_bot_account_value_eur()
    config = trading_bot_connector.get_bot_config_all()
    return {"account": account, "config": config}


@app.get("/api/trading-bot/performance")
def trading_bot_performance():
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT log_date, portfolio_value, trades_count "
                "FROM daily_log ORDER BY log_date ASC"
            )).fetchall()
        return [
            {"datum": str(r[0]), "portfolio_value": float(r[1]), "trades_count": r[2]}
            for r in rows
        ]
    except Exception:
        return []


@app.get("/api/trading-bot/trades")
def trading_bot_trades(limit: int = 100):
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, created_at, ticker, direction, status, entry_price, "
                "exit_price, stop_loss, take_profit, quantity, pnl_usd, pnl_pct, mode "
                "FROM trades ORDER BY created_at DESC LIMIT :limit"
            ), {"limit": limit}).fetchall()
        cols = ["id", "created_at", "ticker", "direction", "status", "entry_price",
                "exit_price", "stop_loss", "take_profit", "quantity", "pnl_usd", "pnl_pct", "mode"]
        return [dict(zip(cols, [str(v) if hasattr(v, "isoformat") else v for v in r])) for r in rows]
    except Exception:
        return []


@app.put("/api/trading-bot/config")
def update_trading_bot_config(werte: dict):
    trading_bot_connector.set_bot_config(werte)
    return trading_bot_connector.get_bot_config_all()


@app.get("/api/bot-config")
def bot_config():
    return trading_bot_connector.get_bot_config_all()


@app.put("/api/bot-config/{key}")
def update_bot_config_key(key: str, payload: dict):
    trading_bot_connector.set_bot_config({key: payload["value"]})
    return trading_bot_connector.get_bot_config_all()


# ─────────────────────────────────────────────
# IMMOBILIE
# ─────────────────────────────────────────────

@app.get("/api/real-estate")
def real_estate(user_id: int):
    _user_or_404(user_id)
    with get_session() as session:
        immobilien = session.query(PosRealEstate).filter_by(user_id=user_id).all()
        result = []
        for im in immobilien:
            eigenkapital = (im.letzter_schaetzwert or 0.0) - (im.restschuld or 0.0)
            ltv_pct = (
                (im.restschuld / im.letzter_schaetzwert * 100)
                if im.letzter_schaetzwert else None
            )
            result.append({
                "id": im.id,
                "adresse": im.adresse,
                "kaufpreis_gesamt": im.kaufpreis_gesamt,
                "kaufjahr": im.kaufjahr,
                "letzter_schaetzwert": im.letzter_schaetzwert,
                "restschuld": im.restschuld,
                "eigenkapital": eigenkapital,
                "ltv_pct": ltv_pct,
                "monatliche_rate": im.monatliche_rate,
                "mieteinnahmen": im.mieteinnahmen,
                "kredit_zinssatz": im.kredit_zinssatz,
                "zinsbindung_bis": str(im.zinsbindung_bis) if im.zinsbindung_bis else None,
            })
        return result


_REAL_ESTATE_DATE_FIELDS = ("kaufdatum", "vermietung_start", "zinsbindung_bis")


@app.post("/api/real-estate")
def create_real_estate(payload: dict):
    from database import save_real_estate
    user_id = payload.pop("user_id")
    for feld in _REAL_ESTATE_DATE_FIELDS:
        if payload.get(feld):
            payload[feld] = datetime.strptime(payload[feld], "%Y-%m-%d").date()
    real_estate_id = save_real_estate(user_id, **payload)
    return {"id": real_estate_id}


@app.delete("/api/real-estate/{real_estate_id}")
def remove_real_estate(real_estate_id: int):
    from database import delete_real_estate
    delete_real_estate(real_estate_id)
    return {"ok": True}


# ─────────────────────────────────────────────
# FAMILIE
# ─────────────────────────────────────────────

@app.get("/api/family")
def family():
    with get_session() as session:
        depots = [
            {"user_id": u.id, "name": u.name,
             **portfolio_module.get_portfolio_summary(u.id)}
            for u in session.query(PosUser).all()
        ]
        ziele = [
            {"id": z.id, "name": z.name, "fortschritt_pct": z.fortschritt_pct,
             "aktuell_betrag": z.aktuell_betrag, "ziel_betrag": z.ziel_betrag,
             "zieldatum": str(z.zieldatum) if z.zieldatum else None}
            for z in session.query(PosFamilyGoal).all()
        ]
    return {"depots": depots, "ziele": ziele}


# ─────────────────────────────────────────────
# HAUSHALTSBUCH
# ─────────────────────────────────────────────

@app.get("/api/haushaltsbuch")
def haushaltsbuch(user_id: int):
    _user_or_404(user_id)
    with get_session() as session:
        buchungen = (
            session.query(PosBuchung).filter_by(user_id=user_id)
            .order_by(PosBuchung.datum.desc()).all()
        )
    return {
        "buchungen": [
            {"id": b.id, "datum": str(b.datum), "betrag": b.betrag, "empfaenger": b.empfaenger,
             "verwendungszweck": b.verwendungszweck, "kategorie": b.kategorie, "typ": b.typ}
            for b in buchungen
        ],
        "kategorien": KATEGORIEN,
    }


@app.put("/api/haushaltsbuch/{buchung_id}")
def update_buchung(buchung_id: int, payload: dict):
    with get_session() as session:
        buchung = session.get(PosBuchung, buchung_id)
        if not buchung:
            raise HTTPException(status_code=404, detail=f"Buchung {buchung_id} nicht gefunden")
        kategorie = payload["kategorie"]
        buchung.kategorie = kategorie
        user_id = buchung.user_id
        empfaenger = buchung.empfaenger
    if payload.get("immer_so_kategorisieren") and empfaenger:
        add_kategorisierungsregel(user_id, empfaenger, kategorie)
    return {"ok": True}


@app.post("/api/haushaltsbuch/upload")
async def haushaltsbuch_upload(user_id: int = Form(...), files: list[UploadFile] = File(...)):
    return await _kontoauszug_import(user_id, files)


# ─────────────────────────────────────────────
# KI-ANALYSE (alles live LLM-Aufrufe – bewusst als POST/Action, nicht beim
# Laden der Seite; Antwortzeit kann mehrere Sekunden betragen)
# ─────────────────────────────────────────────

@app.get("/api/ki-analyse/klumpenrisiko")
def klumpenrisiko(user_id: int, schwelle_pct: float = 20.0):
    """Rein rechnerische Konzentrationsprüfung (keine LLM-Latenz) – größte
    Positionen als Anteil am Gesamtwert."""
    pos = [p for p in portfolio_module.get_positions(user_id) if p["market_value"]]
    gesamt = sum(p["market_value"] for p in pos)
    if not gesamt:
        return {"positionen": [], "warnung": False}
    top = sorted(
        [{"name": p["name"], "anteil_pct": p["market_value"] / gesamt * 100} for p in pos],
        key=lambda p: -p["anteil_pct"],
    )[:5]
    return {"positionen": top, "warnung": any(p["anteil_pct"] > schwelle_pct for p in top)}


@app.post("/api/ki-analyse/portfolio")
def ki_analyse_portfolio(payload: dict):
    return llm_analyst.analyze_portfolio(payload["user_id"])


@app.post("/api/ki-analyse/quarterly-report")
def ki_quarterly_report(payload: dict):
    return llm_analyst.generate_quarterly_report(payload["user_id"])


@app.post("/api/ki-analyse/ask")
def ki_ask(payload: dict):
    antwort = llm_analyst.answer_portfolio_question(payload["user_id"], payload["frage"])
    return {"antwort": antwort}


# ─────────────────────────────────────────────
# VERWALTUNG (bewusst auf die 3 im Redesign vorgesehenen Karten begrenzt:
# Portfolio anlegen, Transaktion, Kontoauszug/Screenshot-Import – nicht die
# volle CRUD-Oberfläche des Streamlit-Tabs)
# ─────────────────────────────────────────────

@app.get("/api/portfolios")
def list_portfolios(user_id: int):
    from database import PosPortfolio
    with get_session() as session:
        return [
            {"id": p.id, "name": p.name, "typ": p.typ, "broker": p.broker}
            for p in session.query(PosPortfolio).filter_by(user_id=user_id).all()
        ]


@app.post("/api/portfolios")
def create_portfolio(payload: dict):
    from database import PosPortfolio
    with get_session() as session:
        pf = PosPortfolio(
            user_id=payload["user_id"], name=payload["name"], typ=payload["typ"],
            broker=payload.get("broker"), is_kinderdepot=payload.get("is_kinderdepot", False),
        )
        session.add(pf)
        session.flush()
        return {"id": pf.id}


@app.put("/api/portfolios/{portfolio_id}")
def edit_portfolio(portfolio_id: int, payload: dict):
    portfolio_module.update_portfolio(
        portfolio_id, name=payload.get("name"), typ=payload.get("typ"),
        broker=payload.get("broker"), is_kinderdepot=payload.get("is_kinderdepot"),
    )
    return {"ok": True}


@app.delete("/api/portfolios/{portfolio_id}")
def remove_portfolio(portfolio_id: int):
    portfolio_module.delete_portfolio(portfolio_id)
    return {"ok": True}


@app.get("/api/asset-classes")
def list_asset_classes():
    with get_session() as session:
        return [
            {"id": ac.id, "name": ac.name, "slug": ac.slug}
            for ac in session.query(PosAssetClass).all()
        ]


@app.get("/api/ticker-search")
def ticker_search(q: str):
    return portfolio_module.resolve_ticker(q)


@app.post("/api/transactions")
def create_transaction(payload: dict):
    datum = datetime.strptime(payload["datum"], "%Y-%m-%d").date()
    return portfolio_module.add_transaction(
        portfolio_id=payload["portfolio_id"], typ=payload["typ"], ticker=payload["ticker"],
        quantity=payload["quantity"], price=payload["price"], datum=datum,
        fees=payload.get("fees", 0.0), asset_class_id=payload.get("asset_class_id"),
    )


@app.put("/api/transactions/{transaction_id}")
def edit_transaction(transaction_id: int, payload: dict):
    datum = datetime.strptime(payload["datum"], "%Y-%m-%d").date() if payload.get("datum") else None
    portfolio_module.update_transaction(
        transaction_id, typ=payload.get("typ"), quantity=payload.get("quantity"),
        price=payload.get("price"), datum=datum, fees=payload.get("fees"),
    )
    return {"ok": True}


@app.delete("/api/transactions/{transaction_id}")
def remove_transaction(transaction_id: int):
    portfolio_module.delete_transaction(transaction_id)
    return {"ok": True}


@app.post("/api/target-weights")
def set_target_weight(payload: dict):
    user_id = payload["user_id"]
    asset_class_id = payload["asset_class_id"]
    target_pct = payload["target_pct"]
    min_pct = payload.get("min_pct", max(0.0, target_pct - 0.05))
    max_pct = payload.get("max_pct", min(1.0, target_pct + 0.05))
    with get_session() as session:
        existing = session.query(PosTargetWeight).filter_by(
            user_id=user_id, asset_class_id=asset_class_id
        ).first()
        if existing:
            existing.target_pct, existing.min_pct, existing.max_pct = target_pct, min_pct, max_pct
        else:
            session.add(PosTargetWeight(
                user_id=user_id, asset_class_id=asset_class_id,
                target_pct=target_pct, min_pct=min_pct, max_pct=max_pct,
            ))
    return {"ok": True}


async def _kontoauszug_import(user_id: int, files: list[UploadFile]) -> dict:
    """Analysiert Kontoauszüge per KI und speichert die erkannten Buchungen
    (Haushaltsbuch). Vorher wurde hier nur analysiert, nie gespeichert – die
    Buchungen verschwanden nach dem Request wieder."""
    file_bytes = [(f.filename, await f.read()) for f in files]
    result = kontoauszug_analyzer.analyze_kontoauszuege(file_bytes)
    if result.get("verfuegbar") and result.get("buchungen"):
        result["gespeichert"] = save_buchungen(user_id, result["buchungen"])
    return result


@app.post("/api/kontoauszug-import")
async def kontoauszug_import(user_id: int = Form(...), files: list[UploadFile] = File(...)):
    return await _kontoauszug_import(user_id, files)


@app.post("/api/screenshot-import")
async def screenshot_import(user_id: int = Form(...), file: UploadFile = File(...)):
    """Liest einen Portfolio-Screenshot per Claude Vision aus (siehe llm_analyst.py)
    und gibt die erkannten Positionen zur Bestätigung durch den Nutzer zurück –
    speichert bewusst NICHT automatisch (wie /api/kontoauszug-import)."""
    _user_or_404(user_id)
    file_bytes = await file.read()
    positionen = llm_analyst.analyze_portfolio_screenshot(file_bytes, file.content_type or "image/jpeg")
    return {"positionen": positionen}


# ─────────────────────────────────────────────
# HEALTH / CONFIG
# ─────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True, "warnungen": validate_config(), "base_url": BASE_URL}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8503)
