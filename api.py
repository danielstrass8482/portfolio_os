"""
api.py – FastAPI-Schnittstelle für das React-Frontend (portfolio_react).
Dünne Schicht über den bestehenden Modulen (portfolio.py / tax_engine.py /
rebalancing.py / trading_bot_connector.py / llm_analyst.py / database.py) –
enthält selbst keine Geschäftslogik, nur Request/Response-Mapping.

Läuft parallel zum bestehenden Streamlit-Dashboard (Port 8502), das bis zur
vollständigen React-Umstellung weiterläuft. Port 8503.
"""

import os
import secrets
import tempfile
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import logging
import traceback

import yfinance as yf
from fastapi import (
    FastAPI, APIRouter, Depends, HTTPException, UploadFile, File, Form, Request,
    Response, Cookie, status, BackgroundTasks,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text

import portfolio as portfolio_module
import tax_engine
import rebalancing
import trading_bot_connector
import llm_analyst
import kontoauszug_analyzer
from notifier import send_email
from config import validate_config, BASE_URL, ALERT_EMAIL, FREISTELLUNGSAUFTRAG_DEFAULT
from database import (
    get_session, engine, init_db, PosUser, PosRealEstate, PosFamilyGoal, PosGoal,
    PosPortfolio, PosPosition, PosTransaction, PosAssetClass, PosBuchung,
    PosTargetWeight, PosTaxConfig, get_or_create_user, save_buchungen, add_kategorisierungsregel,
    encrypt_field, decrypt_field, log_admin_access, user_context,
)

# Idempotent – legt neu hinzugekommene Spalten/Tabellen an, falls die anderen
# Services (dashboard.py/main.py) noch nicht neugestartet wurden.
init_db()


# ─────────────────────────────────────────────
# LOG SANITIZING – verhindert dass Secrets (Passwörter, Tokens, ...) über
# logging.* in Logs landen. Greift NUR für Aufrufe über das logging-Modul
# (z.B. uvicorn-interne Logs, logger.error() unten) – die zahlreichen print()-
# Aufrufe im Rest der Codebase laufen an logging vorbei und bleiben davon
# unberührt.
# ─────────────────────────────────────────────
logger = logging.getLogger(__name__)


class SensitiveDataFilter(logging.Filter):
    SENSITIVE_KEYS = [
        "password", "token", "api_key", "secret",
        "authorization", "cookie", "jwt",
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        for key in self.SENSITIVE_KEYS:
            if key.lower() in msg.lower():
                record.msg = "[REDACTED - sensitive data]"
                record.args = ()  # sonst Format-Crash: alte %-Platzhalter passen nicht mehr zum neuen msg
                return True
        return True


logging.getLogger().addFilter(SensitiveDataFilter())

app = FastAPI(title="Portfolio-OS API")

# CORS einschränken (nur eigene Domain + lokaler Dev-Betrieb).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://portfolio.diestraesschens.de",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ─────────────────────────────────────────────
# EXCEPTION HANDLING – Stack Traces landen nur im Log, nie in der Response.
# Registrierte Handler für spezifischere Typen (HTTPException, RateLimitExceeded)
# greifen weiterhin zuerst – dieser Catch-all fängt nur unerwartete Bugs ab,
# die sonst als roher 500 + Traceback an den Client durchgereicht würden.
# ─────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {traceback.format_exc()}")
    return JSONResponse(status_code=500, content={"detail": "Interner Serverfehler"})


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(status_code=404, content={"detail": "Nicht gefunden"})


# ─────────────────────────────────────────────
# SSRF-SCHUTZ (Allowlist für vom Server aus angesteuerte externe Domains)
# ─────────────────────────────────────────────
ALLOWED_DOMAINS = [
    "api.alpaca.markets",
    "paper-api.alpaca.markets",
    "query1.finance.yahoo.com",
    "query2.finance.yahoo.com",
    "api.anthropic.com",
]


def validate_external_url(url: str) -> bool:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return any(parsed.netloc.endswith(d) for d in ALLOWED_DOMAINS)


# ─────────────────────────────────────────────
# AUTH (JWT)
# ─────────────────────────────────────────────
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY nicht gesetzt!")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24
COOKIE_MAX_AGE = ACCESS_TOKEN_EXPIRE_HOURS * 3600

# Argon2id ist aktueller Goldstandard (sicherer als bcrypt) – bcrypt bleibt als
# zweites Schema gelistet, NUR damit bereits bestehende bcrypt-Hashes (z.B.
# Daniels aktuelles Passwort) noch verifiziert werden können. deprecated="auto"
# markiert jeden Hash, der nicht mit dem ERSTEN Schema (argon2) erzeugt wurde,
# als upgrade-fällig – verify_password() nutzt verify_and_update(), das beim
# nächsten erfolgreichen Login automatisch und transparent auf Argon2id
# umhasht, ganz ohne dass das Klartext-Passwort dafür manuell bekannt sein muss.
pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated="auto",
    argon2__memory_cost=65536,  # 64MB
    argon2__time_cost=3,
    argon2__parallelism=4,
)
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Frontend-Domains, denen ein Passwort-Reset-Link ausgestellt werden darf
# (siehe _resolve_reset_base_url) – anders als CORS_allow_origins oben (nur
# portfolio_react) müssen hier auch trading_react/app.ai-tradingbot.de rein,
# da beide Frontends denselben Auth-Layer nutzen und die /reset-password-
# Seite jeweils im eigenen Frontend liegt. Der Request selbst kommt bei
# trading_react/portfolio_react NIE cross-origin an (nginx proxied /api/auth/*
# pfadbasiert same-origin durch), CORS greift hier also nicht – die Origin-
# Allowlist dient ausschließlich dazu, den im Origin/Referer-Header
# mitgeschickten Wert nicht ungeprüft in den E-Mail-Link zu übernehmen
# (sonst könnte ein manipulierter Header auf eine Phishing-Domain zeigen).
ALLOWED_RESET_ORIGINS = {
    "https://portfolio.diestraesschens.de",
    "https://trading.diestraesschens.de",
    "https://app.ai-tradingbot.de",
    "http://localhost:3000",
    "http://localhost:3001",
}

RESET_TOKEN_EXPIRE_MINUTES = 45  # kürzer als die 48h der Registrierungs-Freischaltung (sicherheitskritischer)

# Einmalig beim Modulimport erzeugter Dummy-Hash, gegen den forgot_password()
# bei NICHT existierender E-Mail verifiziert (statt gar nichts zu tun) – Argon2
# ist der klar dominante Zeitfaktor ggü. dem DB-Lookup, ein gleich teurer
# Dummy-Vergleich verhindert, dass die Antwortzeit verrät, ob die E-Mail
# registriert ist (siehe forgot_password-Docstring für den vollständigen
# Timing-Safety-Ansatz inkl. BackgroundTasks für den Mailversand).
_DUMMY_PASSWORD_HASH = pwd_context.hash(secrets.token_urlsafe(16))


def _resolve_reset_base_url(request: Request) -> str:
    """Wählt die Frontend-Basis-URL für den Reset-Link anhand des Origin-
    (bevorzugt) bzw. Referer-Headers (Fallback, z.B. bei manchen Browsern/
    Proxies ohne Origin auf einfachen POSTs) – nur gegen ALLOWED_RESET_ORIGINS
    geprüfte Werte werden übernommen, alles andere fällt auf das global
    konfigurierte BASE_URL zurück (portfolio_react)."""
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    raw = raw.rstrip("/")
    for allowed in ALLOWED_RESET_ORIGINS:
        if raw == allowed or raw.startswith(allowed + "/"):
            return allowed
    return BASE_URL


# Produkt-Scope bei Registrierung (2026-08-21): pos_users ist die gemeinsame
# Identitätstabelle für trading_bot UND portfolio_os (siehe PosUser-
# Modelkommentar in database.py) – bis eben bekam JEDE Registrierung über
# /api/auth/register automatisch vollen portfolio_os-Zugriff, unabhängig
# davon über welches Frontend sie kam. PORTFOLIO_OS_ORIGINS grenzt die
# tatsächliche portfolio_os-Domain gegen die Trading-Bot-Domains ab (gleiches
# Origin/Referer-Muster wie ALLOWED_RESET_ORIGINS/_resolve_reset_base_url
# oben). Aktuell hat NUR trading_react (trading.diestraesschens.de /
# app.ai-tradingbot.de) ein Registrierungsformular, das /api/auth/register
# aufruft (siehe trading_react/src/lib/auth.ts) – portfolio_react hat keine
# eigene Registrierungs-Seite (Verwaltung/Onboarding setzt einen bestehenden
# Login voraus). Diese Origin-Erkennung greift also aktuell praktisch nie in
# den portfolio_os-Zweig, ist aber die korrekte Vorbereitung falls das je
# einen eigenen Signup-Weg bekommt.
PORTFOLIO_OS_ORIGINS = {"https://portfolio.diestraesschens.de", "http://localhost:3000"}


def _is_portfolio_os_signup(request: Request) -> bool:
    """True nur wenn Origin/Referer eindeutig auf PORTFOLIO_OS_ORIGINS zeigt;
    fehlender/unbekannter Header fällt auf False zurück (sicherer Default –
    portfolio_os_access wird nie versehentlich vergeben, siehe Modulkommentar
    oben)."""
    raw = (request.headers.get("origin") or request.headers.get("referer") or "").rstrip("/")
    return any(raw == o or raw.startswith(o + "/") for o in PORTFOLIO_OS_ORIGINS)


MIN_PASSWORD_LENGTH = 8


def _require_strong_password(password: str) -> None:
    """Gemeinsame Mindestanforderung ans Passwort (Register/Reset/Change-
    Password) – ein Ort statt drei Kopien derselben Zahl, siehe register()/
    reset_password()/change_password()."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"Passwort mindestens {MIN_PASSWORD_LENGTH} Zeichen")


def verify_password(plain: str, hashed: str) -> tuple[bool, Optional[str]]:
    """Gibt (gültig, neuer_hash) zurück – neuer_hash ist gesetzt wenn der
    bestehende Hash nicht mit dem aktuellen Default-Schema (Argon2id) erzeugt
    wurde und automatisch aufgefrischt werden soll (siehe pwd_context oben)."""
    try:
        return pwd_context.verify_and_update(plain, hashed)
    except ValueError:
        return False, None


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(
    request: Request,
    token_cookie: Optional[str] = Cookie(default=None, alias="token"),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Nicht autorisiert",
        headers={"WWW-Authenticate": "Bearer"},
    )
    # Cookie bevorzugen (siehe Security Schritt 2: HttpOnly statt localStorage),
    # Bearer-Header als Fallback (z.B. für zukünftige Nicht-Browser-Clients).
    token = token_cookie
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        raise credentials_exception

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        raw_sub = payload.get("sub")
        if raw_sub is None:
            raise credentials_exception
        user_id = int(raw_sub)
    except (JWTError, ValueError):
        raise credentials_exception
    with get_session() as session:
        user = session.query(PosUser).filter_by(id=user_id).first()
        if not user:
            raise credentials_exception
        if user.status == "pending":
            raise HTTPException(403, "Dein Zugang wartet auf Freischaltung. Du wirst per E-Mail benachrichtigt.")
        if user.status == "rejected":
            raise HTTPException(403, "Dein Zugang wurde nicht freigeschaltet. Kontaktiere daniel.strass@gmx.de")
        return user


@app.post("/api/auth/login")
@limiter.limit("5/minute")
async def login(request: Request, response: Response, form_data: OAuth2PasswordRequestForm = Depends()):
    with get_session() as session:
        user = session.query(PosUser).filter_by(email=form_data.username).first()
        if not user or not user.password_hash:
            raise HTTPException(status_code=401, detail="E-Mail oder Passwort falsch")
        valid, new_hash = verify_password(form_data.password, user.password_hash)
        if not valid:
            raise HTTPException(status_code=401, detail="E-Mail oder Passwort falsch")
        if user.status == "pending":
            raise HTTPException(403, "Dein Zugang wartet auf Freischaltung. Du wirst per E-Mail benachrichtigt.")
        if user.status == "rejected":
            raise HTTPException(403, "Dein Zugang wurde nicht freigeschaltet. Kontaktiere daniel.strass@gmx.de")
        if new_hash:
            user.password_hash = new_hash  # transparentes Auffrischen auf Argon2id
        user.last_login = datetime.utcnow()
        session.commit()
        token = create_access_token({"sub": str(user.id)})

        # Token als HttpOnly Cookie setzen – JavaScript kann nicht zugreifen (XSS-Schutz).
        response.set_cookie(
            key="token",
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=COOKIE_MAX_AGE,
            path="/",
        )
        return {
            "message": "Login erfolgreich",
            "user": {"id": user.id, "name": user.name, "email": user.email, "rolle": user.rolle},
        }


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key="token", path="/")
    return {"message": "Logout erfolgreich"}


@app.get("/api/auth/me")
async def get_me(current_user=Depends(get_current_user)):
    return {
        "id": current_user.id, "name": current_user.name,
        "email": current_user.email, "rolle": current_user.rolle,
    }


@app.post("/api/auth/refresh")
async def refresh_token(response: Response, current_user=Depends(get_current_user)):
    token = create_access_token({"sub": str(current_user.id)})
    response.set_cookie(
        key="token", value=token, httponly=True, secure=True,
        samesite="lax", max_age=COOKIE_MAX_AGE, path="/",
    )
    return {"message": "Token aufgefrischt"}


# ─────────────────────────────────────────────
# REGISTRIERUNG MIT ADMIN-APPROVAL (öffentlich, kein Login nötig – siehe
# Feature 1/2: neue Nutzer landen mit status='pending' in der DB und können
# sich erst nach Freischaltung per E-Mail-Link einloggen)
# ─────────────────────────────────────────────

def send_approval_email(name: str, email: str, reason: str, token: str, user_id: int):
    approve_url = f"{BASE_URL}/api/auth/approve/{token}"
    reject_url = f"{BASE_URL}/api/auth/reject/{token}"

    subject = f"🔔 Neuer Registrierungsantrag: {name}"
    body = f"""Neuer Registrierungsantrag für den AI Trading Bot:

Name:    {name}
E-Mail:  {email}
Grund:   {reason or 'Kein Grund angegeben'}

Bitte freigeben oder ablehnen (Link gültig 48h):

✅ FREIGEBEN:
{approve_url}

❌ ABLEHNEN:
{reject_url}

---
AI Trading Bot Admin"""
    send_email(subject, body, to_email=ALERT_EMAIL)


def send_welcome_email(name: str, email: str):
    subject = "🎉 Dein Zugang wurde freigeschaltet!"
    body = f"""Hallo {name},

dein Zugang zum AI Trading Bot wurde freigeschaltet!

Du kannst dich jetzt einloggen und deinen Alpaca Account verbinden:
https://trading.diestraesschens.de

Viel Erfolg!"""
    send_email(subject, body, to_email=email)


def send_rejection_email(name: str, email: str):
    subject = "AI Trading Bot - Registrierungsantrag"
    body = f"""Hallo {name},

leider können wir deinen Zugang zum AI Trading Bot
aktuell nicht freischalten.

Bei Fragen: daniel.strass@gmx.de"""
    send_email(subject, body, to_email=email)


@app.post("/api/auth/register")
@limiter.limit("3/minute")
async def register(request: Request, body: dict):
    name = body.get("name", "").strip()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    reason = body.get("reason", "")

    if not name or not email or not password:
        raise HTTPException(400, "Name, E-Mail und Passwort erforderlich")
    _require_strong_password(password)

    with get_session() as session:
        existing = session.query(PosUser).filter_by(email=email).first()
        if existing:
            raise HTTPException(400, "E-Mail bereits registriert")

        token = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(hours=48)
        password_hash = pwd_context.hash(password)
        is_portfolio_os = _is_portfolio_os_signup(request)
        user = PosUser(
            name=name,
            email=email,
            password_hash=password_hash,
            rolle="member",
            status="pending",
            registration_reason=reason,
            approval_token=token,
            approval_token_expires=expires,
            trading_bot_access=not is_portfolio_os,
            portfolio_os_access=is_portfolio_os,
        )
        session.add(user)
        session.flush()
        send_approval_email(name, email, reason, token, user.id)

    return {"message": "Registrierung erfolgreich. Du wirst benachrichtigt sobald dein Zugang freigeschaltet ist."}


@app.get("/api/auth/approve/{token}")
async def approve_user(token: str):
    with get_session() as session:
        user = session.query(PosUser).filter_by(approval_token=token).first()
        if not user:
            raise HTTPException(404, "Token nicht gefunden")
        if user.approval_token_expires < datetime.utcnow():
            raise HTTPException(400, "Token abgelaufen")
        if user.status == "active":
            return {"message": "Nutzer bereits aktiv"}

        user.status = "active"
        user.approval_token = None
        name, email = user.name, user.email

    send_welcome_email(name, email)
    return {"message": f"✅ {name} wurde freigeschaltet!"}


@app.get("/api/auth/reject/{token}")
async def reject_user(token: str):
    with get_session() as session:
        user = session.query(PosUser).filter_by(approval_token=token).first()
        if not user:
            raise HTTPException(404, "Token nicht gefunden")

        user.status = "rejected"
        user.approval_token = None
        name, email = user.name, user.email

    send_rejection_email(name, email)
    return {"message": f"❌ {name} wurde abgelehnt."}


# ─────────────────────────────────────────────
# PASSWORT-RESET (öffentlich, kein Login nötig – analog zur Registrierungs-
# Freischaltung oben, aber bewusst mit eigenen Token-/Ablauf-Spalten
# (reset_token/reset_token_expires statt approval_token), da semantisch ein
# anderer Zweck: eine laufende Registrierung darf einen parallelen Passwort-
# Reset nicht invalidieren und umgekehrt, siehe PosUser-Docstring.
# ─────────────────────────────────────────────

GENERIC_FORGOT_PASSWORD_RESPONSE = {
    "message": "Falls diese E-Mail registriert ist, wurde ein Link zum Zurücksetzen versendet."
}


def send_password_reset_email(name: str, email: str, token: str, base_url: str):
    reset_url = f"{base_url}/reset-password?token={token}"
    subject = "Passwort zurücksetzen"
    body = f"""Hallo {name},

für deinen Account wurde ein Zurücksetzen des Passworts angefordert.

Falls du das warst, setze hier dein neues Passwort (Link gültig {RESET_TOKEN_EXPIRE_MINUTES} Minuten):
{reset_url}

Falls du das NICHT warst, kannst du diese E-Mail ignorieren – dein Passwort
bleibt unverändert, der Link verfällt von selbst.

---
AI Trading Bot / Portfolio-OS"""
    send_email(subject, body, to_email=email)


@app.post("/api/auth/forgot-password")
@limiter.limit("3/minute")
async def forgot_password(request: Request, body: dict, background_tasks: BackgroundTasks):
    """
    KEIN Enumeration-Leak: liefert für existierende UND nicht-existierende
    E-Mail-Adressen denselben Status-Code + Body (GENERIC_FORGOT_PASSWORD_
    RESPONSE) zurück. Timing-Safety hat zwei Bausteine:
      1. Anders als /api/auth/login braucht dieser Endpoint für eine
         EXISTIERENDE E-Mail von sich aus KEINEN Argon2-Vergleich (kein
         Passwort wird hier geprüft) – ohne Gegenmaßnahme wäre der Nicht-
         Existenz-Fall (reiner DB-Lookup) also sogar SCHNELLER als der
         Erfolgsfall (DB-Lookup + UPDATE), ein ebenso ausnutzbares Timing-
         Oracle wie andersherum. Der Dummy-Vergleich gegen einen fixen Hash
         (_DUMMY_PASSWORD_HASH) läuft daher UNABHÄNGIG vom Ergebnis für
         BEIDE Fälle (siehe unten, vor der if/else-Verzweigung) – Argon2 ist
         der klar dominante Zeitfaktor, der verbleibende SELECT-vs-SELECT+
         UPDATE-Unterschied fällt daneben nicht mehr messbar ins Gewicht.
      2. Der Mailversand (SMTP-Roundtrip, potenziell hunderte ms bis Sekunden
         bei Netzwerk-Hakeleien, siehe notifier.send_email-Fallback-Port-Logik)
         läuft über BackgroundTasks NACH der Response, nicht davor – sonst
         wäre der Erfolgsfall messbar langsamer als der Nicht-Existenz-Fall,
         unabhängig vom Argon2-Ausgleich oben.
    """
    email = (body.get("email") or "").strip().lower()
    if not email:
        return GENERIC_FORGOT_PASSWORD_RESPONSE

    base_url = _resolve_reset_base_url(request)

    with get_session() as session:
        user = session.query(PosUser).filter_by(email=email).first()
        # Läuft bewusst UNABHÄNGIG davon, ob `user` existiert (siehe Docstring
        # Punkt 1) - vor der Verzweigung, damit beide Zweige exakt denselben
        # Argon2-Aufwand tragen.
        pwd_context.verify(secrets.token_urlsafe(8), _DUMMY_PASSWORD_HASH)
        if not user:
            return GENERIC_FORGOT_PASSWORD_RESPONSE

        # Überschreibt einen ggf. noch offenen älteren reset_token – ein davor
        # ausgestellter Link wird dadurch automatisch ungültig (die Lookup-
        # Query in reset_password() findet ihn nicht mehr), erfüllt "zweite
        # Anfrage invalidiert die erste" ohne eigene Zusatzlogik.
        token = secrets.token_urlsafe(32)
        user.reset_token = token
        user.reset_token_expires = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_EXPIRE_MINUTES)
        name, target_email = user.name, user.email
        session.commit()

    background_tasks.add_task(send_password_reset_email, name, target_email, token, base_url)
    return GENERIC_FORGOT_PASSWORD_RESPONSE


@app.post("/api/auth/reset-password")
@limiter.limit("5/minute")
async def reset_password(request: Request, body: dict):
    token = body.get("token", "")
    new_password = body.get("password", "")
    if not token or not new_password:
        raise HTTPException(400, "Token und neues Passwort erforderlich")
    _require_strong_password(new_password)

    with get_session() as session:
        user = session.query(PosUser).filter_by(reset_token=token).first()
        if not user or not user.reset_token_expires or user.reset_token_expires < datetime.utcnow():
            raise HTTPException(400, "Link ungültig oder abgelaufen")

        user.password_hash = pwd_context.hash(new_password)
        # single-use: sofort ungültig machen, ein zweiter Versuch mit
        # demselben Token findet danach keine Zeile mehr (reset_token=NULL
        # matcht nie den nicht-leeren `token`-Parameter oben).
        user.reset_token = None
        user.reset_token_expires = None
        session.commit()

    return {"message": "Passwort erfolgreich geändert. Du kannst dich jetzt einloggen."}


@app.post("/api/auth/change-password")
@limiter.limit("5/minute")
async def change_password(request: Request, body: dict, current_user=Depends(get_current_user)):
    """
    Passwort ändern für einen BEREITS eingeloggten Nutzer (gültiges JWT
    vorausgesetzt, siehe get_current_user) – anders als forgot_password()/
    reset_password() oben kein Anonymitäts-/Enumeration-Thema (der Nutzer
    kennt seine eigene Existenz bereits), daher ein direktes 400 statt der
    generischen Reset-Antwort, wenn current_password nicht passt.

    current_user aus Depends(get_current_user) ist an eine bereits
    geschlossene Session gebunden (siehe get_current_user-Docstring/
    get_session()-Contextmanager) – Mutation daher wie bei connect_alpaca()
    über einen frischen Re-Query per current_user.id, nicht direkt auf dem
    Depends-Objekt.
    """
    current_password = body.get("current_password", "")
    new_password = body.get("new_password", "")
    if not current_password or not new_password:
        raise HTTPException(400, "Aktuelles und neues Passwort erforderlich")
    _require_strong_password(new_password)

    with get_session() as session:
        user = session.query(PosUser).filter_by(id=current_user.id).first()
        valid, _ = verify_password(current_password, user.password_hash)
        if not valid:
            raise HTTPException(400, "Aktuelles Passwort ist falsch")
        user.password_hash = pwd_context.hash(new_password)

    return {"message": "Passwort erfolgreich geändert."}


def require_portfolio_os_access(current_user=Depends(get_current_user)):
    """
    Zusätzlich zum gültigen JWT (get_current_user) muss der Nutzer
    portfolio_os_access=true haben, sonst 403 statt nur leerer/gefilterter
    Ergebnisse (siehe PosUser-Modelkommentar in database.py). Router-weite
    Dependency auf `protected` – NICHT auf `protected_shared` (dort bewusst
    weggelassen, siehe dortiger Kommentar).
    """
    if not current_user.portfolio_os_access:
        raise HTTPException(403, "Kein Zugriff auf Portfolio-OS für diesen Account")
    return current_user


async def _apply_user_context(current_user=Depends(get_current_user)):
    """
    RLS-Umbau Chunk 1 (2026-08-21, siehe docs/rls-force-umbau-plan-21-08.md):
    setzt database.user_context(current_user.id) für die Dauer genau dieses
    einen Requests -- jeder get_session()-Aufruf innerhalb des Request-
    Handlers (egal ob direkt in api.py oder in einer der ~48 aufgerufenen
    Business-Logic-Funktionen aus portfolio.py/tax_engine.py/rebalancing.py/
    database.py) übernimmt dadurch automatisch den korrekten user_id-Kontext,
    OHNE dass diese Funktionen selbst angepasst werden müssen. FastAPI
    schließt Yield-Dependencies wie einen with-Block: Code vor `yield` läuft
    vor dem Endpoint, der finally-Block in user_context() (über
    Depends-Exit-Stack) läuft garantiert danach, auch bei einer Exception im
    Endpoint selbst.

    BEWUSST `async def`, NICHT `def` (beim ersten Testlauf als `ValueError:
    Token ... was created in a different Context` aufgefallen): FastAPI führt
    synchrone Yield-Dependencies über contextmanager_in_threadpool aus, das
    __enter__ (vor yield) und __exit__ (nach yield) über ZWEI SEPARATE
    run_in_threadpool()-Aufrufe dispatcht -- jeder kopiert den Kontext für
    sich (anyio contextvars.copy_context()), wodurch das beim Setzen
    erzeugte contextvars.Token beim Zurücksetzen aus einer ANDEREN Context-
    Kopie stammt und Python das zu Recht ablehnt. Als `async def` läuft die
    gesamte Dependency (beide Seiten des yield) in einem Rutsch im
    Request-Task des Event-Loops, ohne Threadpool-Sprung dazwischen -- der
    nachfolgende Endpoint (meist synchrones `def`) sieht den gesetzten Wert
    trotzdem korrekt, weil FastAPI dessen eigenen run_in_threadpool()-Aufruf
    erst NACH dem Dependency-Durchlauf startet und dabei den zu diesem
    Zeitpunkt bereits aktualisierten Kontext kopiert (s. Testbericht).

    Setzt aktuell current_user.id, NICHT das Ergebnis von _resolve_user_id()
    -- der Admin-Cross-View-Fall (Admin ruft /api/positions?user_id=<andere
    ID> auf) bekommt dadurch in DIESEM Chunk noch NICHT automatisch den
    korrekten Ziel-Kontext gesetzt. Das ist bewusst so (siehe Plan-Dokument,
    eigener Sonderfall/Chunk) und aktuell folgenlos, da FORCE ROW LEVEL
    SECURITY hier noch nicht aktiviert wird -- der Kontext wird zwar gesetzt,
    wirkt aber (wie bei allen Tabellen in diesem Chunk) noch nicht
    sicherheitsrelevant, weil Postgres RLS-Policies ohne FORCE für den
    Tabellenbesitzer (trading_bot_user, mit dem die App verbindet) ohnehin
    ignoriert.
    """
    with user_context(current_user.id):
        yield current_user


# Alle Business-Endpoints hängen an diesem Router statt direkt an `app` – die
# Router-weiten Dependencies erzwingen ein gültiges JWT UND portfolio_os_access
# für JEDEN Endpoint darunter, ohne dass jede einzelne Funktionssignatur
# angepasst werden muss. Nur /api/auth/* (oben), /api/user/* (protected_shared,
# unten) und /api/health (unten) bleiben ohne portfolio_os_access-Prüfung.
# _apply_user_context als letzte Dependency, damit sie erst NACH
# require_portfolio_os_access (403-Check) läuft -- FastAPI löst
# Router-Dependencies in Listenreihenfolge auf.
protected = APIRouter(dependencies=[
    Depends(get_current_user), Depends(require_portfolio_os_access), Depends(_apply_user_context),
])

# Für Endpoints, die für JEDEN eingeloggten Nutzer erreichbar bleiben müssen,
# unabhängig von portfolio_os_access – aktuell nur die Alpaca-Connect/Status-
# Endpoints (siehe unten), da trading_bot-only-Nutzer (z.B. Dana, siehe
# Diagnose 2026-08-21) ihren Alpaca-Account weiterhin verbinden können müssen,
# auch ohne portfolio_os_access. /api/auth/* liegt bewusst NICHT hier,
# sondern direkt auf `app` (Login/Register brauchen noch gar keinen current_user).
# _apply_user_context auch hier: die zwei Endpoints lesen/schreiben zwar nur
# pos_users (außerhalb des RLS-Scopes dieser Runde), aber ein konsistent
# gesetzter Kontext über ALLE eingeloggten Requests hinweg ist einfacher zu
# verifizieren als eine Ausnahme extra zu begründen.
protected_shared = APIRouter(dependencies=[Depends(get_current_user), Depends(_apply_user_context)])


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


# ─────────────────────────────────────────────
# IDOR-FIX (2026-08-05, siehe trading_shared/docs/db-isolation-audit-05-08.md
# Teil C): "Meine Daten"-Endpoints dürfen NIE einem extern mitgegebenen user_id-
# Parameter vertrauen (das war der Kernfehler – jeder eingeloggte Nutzer konnte
# jeden anderen auslesen/verändern). Ausnahme: current_user.rolle == "admin"
# darf weiterhin für andere user_id agieren (erhält den Family-Switcher im
# Frontend, UserContext.tsx, für Profile ohne eigenen Login – analog zu
# require_owner() in trading_api.py/trading_api_saxo.py, dort mit hartcodierter
# owner-id statt einer Rolle). Jeder ECHTE Cross-User-Zugriff eines Admins wird
# in pos_admin_access_log protokolliert (siehe log_admin_access, database.py).
#
# ADMIN-SCOPE-TODO: "Admin" bedeutet aktuell voller Zugriff auf ALLE pos_users,
# nicht nur Familienmitglieder. Für den jetzigen Beta-Testerkreis bewusst so
# belassen – vor echtem Kunden-Onboarding (über die bestehenden Beta-Tester
# hinaus) nochmal bewusst entscheiden, z.B. getrennte Berechtigungen
# "Familien-Verwaltung" vs. "Support-Zugriff auf Kundendaten".
#
# ADMIN-SCOPE-TODO (2026-08-05, Audit Chunk 4): /api/family und
# /api/overview?family=true waren bis eben ganz ohne Rollen-Check erreichbar
# (jeder eingeloggte Nutzer sah Name+Depotwert+G/V+Positionsanzahl JEDES
# registrierten Nutzers) – jetzt auf current_user.rolle=="admin" beschränkt
# (siehe _require_admin unten), analog zum "Meine Daten"-Fix oben. OFFENE
# FRAGE, bewusst nicht selbst entschieden: falls es künftig einen legitimen
# Use-Case für Nicht-Admin-Zugriff auf family=true geben soll (z.B. "echte"
# Familienmitglieder untereinander, die sich gegenseitig sehen dürfen sollen,
# ohne dass jeder gleich Admin-Rechte auf ALLE pos_users bekommt) – das würde
# eine eigene Gruppierung brauchen (aktuell gibt es in pos_users keinerlei
# "Familie"-Feld, "family=true" aggregiert schlicht ALLE Nutzer). Bis dahin:
# admin-only.
# ─────────────────────────────────────────────


def _require_admin(current_user, endpoint: str) -> None:
    """Für Endpoints, die grundsätzlich über alle pos_users aggregieren
    (/api/family, /api/overview?family=true) – siehe ADMIN-SCOPE-TODO oben."""
    if current_user.rolle != "admin":
        raise HTTPException(status_code=403, detail="Nur für Admins verfügbar")


def _resolve_user_id(current_user, requested_user_id: Optional[int], endpoint: str, method: str = "GET") -> int:
    """Liefert die tatsächlich zu verwendende user_id für 'Meine Daten'-Endpoints
    (siehe Modulkommentar oben)."""
    if current_user.rolle != "admin" or requested_user_id is None:
        return current_user.id
    if requested_user_id != current_user.id:
        log_admin_access(current_user.id, requested_user_id, endpoint, method)
    return requested_user_id


def _owner_check_id(current_user) -> Optional[int]:
    """Für die Ownership-prüfenden Hilfsfunktionen in portfolio.py/database.py
    (update_position/delete_position/update_transaction/delete_transaction/
    update_portfolio/delete_portfolio/delete_real_estate): None überspringt die
    Prüfung dort (Admin-Bypass), sonst wird current_user.id streng durchgesetzt."""
    return None if current_user.rolle == "admin" else current_user.id


def _maybe_log_admin_access(current_user, actual_owner_id: int, endpoint: str, method: str) -> None:
    """Protokolliert einen ECHTEN Cross-User-Zugriff, nachdem eine der obigen
    Hilfsfunktionen mit Admin-Bypass (owner_user_id=None) gelaufen ist und die
    tatsächliche Besitzer-user_id zurückgegeben hat."""
    if current_user.rolle == "admin" and actual_owner_id != current_user.id:
        log_admin_access(current_user.id, actual_owner_id, endpoint, method)


def _position_owner_id(position_id: int) -> Optional[int]:
    with get_session() as session:
        pos = session.get(PosPosition, position_id)
        return pos.portfolio.user_id if pos else None


def _require_position_access(position_id: int, current_user, endpoint: str, method: str = "GET") -> None:
    owner_id = _position_owner_id(position_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail=f"Position {position_id} nicht gefunden")
    if owner_id != current_user.id:
        if current_user.rolle != "admin":
            raise HTTPException(status_code=404, detail=f"Position {position_id} nicht gefunden")
        log_admin_access(current_user.id, owner_id, endpoint, method)


def _portfolio_owner_id(portfolio_id: int) -> Optional[int]:
    with get_session() as session:
        pf = session.get(PosPortfolio, portfolio_id)
        return pf.user_id if pf else None


def _require_portfolio_access(portfolio_id: int, current_user, endpoint: str, method: str = "GET") -> None:
    owner_id = _portfolio_owner_id(portfolio_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} nicht gefunden")
    if owner_id != current_user.id:
        if current_user.rolle != "admin":
            raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} nicht gefunden")
        log_admin_access(current_user.id, owner_id, endpoint, method)


def _require_buchung_access(buchung, current_user, endpoint: str, method: str = "GET") -> None:
    """buchung bereits geladen (Objekt oder None)."""
    if buchung is None:
        raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
    if buchung.user_id != current_user.id:
        if current_user.rolle != "admin":
            raise HTTPException(status_code=404, detail="Buchung nicht gefunden")
        log_admin_access(current_user.id, buchung.user_id, endpoint, method)


# ─────────────────────────────────────────────
# USERS
# ─────────────────────────────────────────────

@protected.get("/api/users")
def list_users(current_user=Depends(get_current_user)):
    with get_session() as session:
        return [
            {"id": u.id, "name": u.name, "email": u.email, "rolle": u.rolle}
            for u in session.query(PosUser).all()
        ]


@protected.post("/api/users")
def create_user(payload: dict, current_user=Depends(get_current_user)):
    # IDOR-Fix (siehe Modulkommentar oben): "rolle" darf über diesen Weg NIE auf
    # "admin" gesetzt werden, egal wer aufruft – verhindert Privilegien-
    # Eskalation beim Anlegen eines neuen (Familien-)Profils.
    rolle = payload.get("rolle", "member")
    if rolle == "admin":
        rolle = "member"
    with get_session() as session:
        user = get_or_create_user(session, payload["name"], payload.get("email"), rolle=rolle)
        return {"id": user.id}


@protected.put("/api/users/{user_id}")
def update_user(user_id: int, payload: dict, current_user=Depends(get_current_user)):
    # IDOR-Fix + Privilegien-Eskalations-Fix (siehe Modulkommentar oben, und
    # db-isolation-audit-05-08.md Teil C, "Am schwersten"-Abschnitt): vorher
    # konnte sich JEDER eingeloggte Nutzer per PUT /api/users/{eigene_id} mit
    # {"rolle": "admin"} selbst zum Admin machen.
    is_admin = current_user.rolle == "admin"
    if user_id != current_user.id and not is_admin:
        raise HTTPException(status_code=403, detail="Nur eigenes Profil oder als Admin änderbar")
    if "rolle" in payload and not is_admin:
        raise HTTPException(status_code=403, detail="Nur Admins dürfen Rollen ändern")
    if is_admin and user_id != current_user.id:
        log_admin_access(current_user.id, user_id, "/api/users/{user_id}", "PUT")
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
# ALPACA CONNECT (pro Nutzer, siehe Feature 4 – Keys landen verschlüsselt in
# pos_users, analog zu PosRealEstate.adresse via encrypt_field/decrypt_field)
# ─────────────────────────────────────────────

@protected_shared.post("/api/user/alpaca-connect")
def connect_alpaca(body: dict, current_user=Depends(get_current_user)):
    api_key = body.get("api_key", "").strip()
    secret_key = body.get("secret_key", "").strip()
    mode = body.get("mode", "paper")

    if not api_key or not secret_key:
        raise HTTPException(400, "API Key und Secret erforderlich")
    if mode not in ("paper", "live"):
        raise HTTPException(400, "Ungültiger Modus")

    try:
        import alpaca_trade_api as tradeapi
        base_url = (
            "https://api.alpaca.markets" if mode == "live"
            else "https://paper-api.alpaca.markets"
        )
        api = tradeapi.REST(api_key, secret_key, base_url)
        account = api.get_account()
        buying_power = float(account.buying_power)
        cash = float(account.cash)
        account_status = account.status
    except Exception as e:
        raise HTTPException(400, f"Verbindung fehlgeschlagen: {str(e)}")

    with get_session() as session:
        user = session.query(PosUser).filter_by(id=current_user.id).first()
        user.alpaca_api_key_encrypted = encrypt_field(api_key)
        user.alpaca_secret_key_encrypted = encrypt_field(secret_key)
        user.alpaca_mode = mode

    return {
        "message": "Alpaca Account verbunden!",
        "account": {"status": account_status, "buying_power": buying_power, "cash": cash, "mode": mode},
    }


@protected_shared.get("/api/user/alpaca-status")
def alpaca_status(current_user=Depends(get_current_user)):
    with get_session() as session:
        user = session.query(PosUser).filter_by(id=current_user.id).first()
        if not user.alpaca_api_key_encrypted:
            return {"connected": False}

        try:
            import alpaca_trade_api as tradeapi
            api_key = decrypt_field(user.alpaca_api_key_encrypted)
            secret = decrypt_field(user.alpaca_secret_key_encrypted)
            base_url = (
                "https://api.alpaca.markets" if user.alpaca_mode == "live"
                else "https://paper-api.alpaca.markets"
            )
            api = tradeapi.REST(api_key, secret, base_url)
            account = api.get_account()
            return {
                "connected": True,
                "mode": user.alpaca_mode,
                "status": account.status,
                "buying_power": float(account.buying_power),
                "cash": float(account.cash),
            }
        except Exception:
            return {"connected": False, "error": "Verbindung fehlgeschlagen"}


# ─────────────────────────────────────────────
# ADMIN – Nutzer-Anträge (siehe Feature 5, nur für rolle == "admin")
# ─────────────────────────────────────────────

@protected.get("/api/admin/pending-users")
def get_pending_users(current_user=Depends(get_current_user)):
    if current_user.rolle != "admin":
        raise HTTPException(403, "Nur für Admins")
    with get_session() as session:
        users = session.query(PosUser).filter_by(status="pending").all()
        return [{
            "id": u.id, "name": u.name, "email": u.email,
            "reason": u.registration_reason, "created_at": str(u.created_at),
        } for u in users]


@protected.post("/api/admin/approve/{user_id}")
def admin_approve_user(user_id: int, current_user=Depends(get_current_user)):
    if current_user.rolle != "admin":
        raise HTTPException(403, "Nur für Admins")
    with get_session() as session:
        user = session.get(PosUser, user_id)
        if not user:
            raise HTTPException(404, f"Nutzer {user_id} nicht gefunden")
        user.status = "active"
        user.approval_token = None
        name, email = user.name, user.email
    send_welcome_email(name, email)
    return {"message": f"✅ {name} wurde freigeschaltet!"}


@protected.post("/api/admin/reject/{user_id}")
def admin_reject_user(user_id: int, current_user=Depends(get_current_user)):
    if current_user.rolle != "admin":
        raise HTTPException(403, "Nur für Admins")
    with get_session() as session:
        user = session.get(PosUser, user_id)
        if not user:
            raise HTTPException(404, f"Nutzer {user_id} nicht gefunden")
        user.status = "rejected"
        user.approval_token = None
        name, email = user.name, user.email
    send_rejection_email(name, email)
    return {"message": f"❌ {name} wurde abgelehnt."}


# ─────────────────────────────────────────────
# ÜBERSICHT
# ─────────────────────────────────────────────

@protected.get("/api/overview")
def overview(user_id: Optional[int] = None, family: bool = False, current_user=Depends(get_current_user)):
    """family=true aggregiert über alle Nutzer (Trading-Bot-Wert wird dabei nur
    EINMAL gezählt, nicht pro Nutzer – siehe get_total_wealth-Docstring), daher
    admin-only (ADMIN-SCOPE-TODO oben). Sonst IDOR-Fix (siehe Modulkommentar
    oben): user_id wird über _resolve_user_id aufgelöst statt dem Query-Param
    blind zu vertrauen."""
    if family:
        _require_admin(current_user, "/api/overview?family=true")
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
        user_id = _resolve_user_id(current_user, user_id, "/api/overview", "GET")
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

@protected.get("/api/positions")
def positions(user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/positions", "GET")
    depot = portfolio_module.get_positions(user_id)
    bot_detail = trading_bot_connector.get_bot_positions_detail()
    return {"depot": depot, "bot": bot_detail}


@protected.post("/api/positions/refresh-prices")
def refresh_prices():
    n = portfolio_module.update_prices()
    return {"aktualisiert": n}


@protected.put("/api/positions/{position_id}")
def edit_position(position_id: int, payload: dict, current_user=Depends(get_current_user)):
    asset_class_id = payload.get("asset_class_id")
    try:
        owner_id = portfolio_module.update_position(
            position_id,
            display_name=payload.get("display_name"),
            ticker=payload.get("ticker"),
            asset_class_id=asset_class_id,
            quantity=payload.get("quantity"),
            avg_buy_price=payload.get("avg_buy_price"),
            owner_user_id=_owner_check_id(current_user),
        )
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Position {position_id} nicht gefunden")
    _maybe_log_admin_access(current_user, owner_id, "/api/positions/{position_id}", "PUT")
    return {"ok": True}


@protected.delete("/api/positions/{position_id}")
def remove_position(position_id: int, current_user=Depends(get_current_user)):
    try:
        owner_id = portfolio_module.delete_position(position_id, owner_user_id=_owner_check_id(current_user))
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Position {position_id} nicht gefunden")
    _maybe_log_admin_access(current_user, owner_id, "/api/positions/{position_id}", "DELETE")
    return {"ok": True}


@protected.get("/api/positions/{position_id}/transactions")
def position_transactions(position_id: int, current_user=Depends(get_current_user)):
    """Kauf-/Verkaufshistorie einer Position – für die Kauf-Pins im Chart (Positionen-Tab)."""
    _require_position_access(position_id, current_user, "/api/positions/{position_id}/transactions", "GET")
    with get_session() as session:
        txs = (
            session.query(PosTransaction).filter_by(position_id=position_id)
            .order_by(PosTransaction.datum.asc()).all()
        )
        return [
            {"id": t.id, "typ": t.typ, "datum": str(t.datum), "quantity": t.quantity, "price": t.price}
            for t in txs
        ]


@protected.get("/api/chart/{ticker}")
def get_chart(ticker: str, period: str = "2y"):
    """
    Kursverlauf für den Positions-Chart. Rechnet auf EUR um (GBp-Notierung
    /100 + FX, USD-Notierung / FX) – degraded mode (leere Liste) statt Absturz
    bei ungültigem Ticker oder yfinance-Ausfall.
    """
    try:
        df = yf.download(ticker, period=period, interval="1d", auto_adjust=False, progress=False)
        prices = df["Close"]
        if hasattr(prices, "columns"):  # manche yfinance-Versionen liefern hier ein DataFrame
            prices = prices.iloc[:, 0]
        prices = prices.dropna()
        if prices.empty:
            return {"dates": [], "prices": [], "currency": "EUR", "verfuegbar": False}

        info = yf.Ticker(ticker).info
        currency = info.get("currency") or "EUR"
        if currency == "GBp":
            fx = yf.Ticker("GBPEUR=X").info.get("regularMarketPrice") or 1.18
            prices = prices / 100 * fx
        elif currency == "USD":
            fx = yf.Ticker("EURUSD=X").info.get("regularMarketPrice") or 1.08
            prices = prices / fx

        return {
            "dates": prices.index.strftime("%Y-%m-%d").tolist(),
            "prices": prices.round(2).tolist(),
            "currency": "EUR",
            "verfuegbar": True,
        }
    except Exception as e:
        print(f"⚠️  Chart für {ticker} nicht verfügbar: {e} (degraded mode)")
        return {"dates": [], "prices": [], "currency": "EUR", "verfuegbar": False}


@protected.get("/api/tax-preview")
def tax_preview(position_id: int, verkauf_preis: float, quantity: Optional[float] = None,
                 current_user=Depends(get_current_user)):
    _require_position_access(position_id, current_user, "/api/tax-preview", "GET")
    return tax_engine.get_tax_preview(position_id, verkauf_preis, quantity)


# ─────────────────────────────────────────────
# STEUER
# ─────────────────────────────────────────────

@protected.get("/api/tax")
def tax(user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/tax", "GET")
    return {
        "freistellung_rest": tax_engine.get_remaining_freistellung(user_id),
        "harvesting": tax_engine.find_tax_loss_harvesting(user_id),
        "jahresuebersicht": tax_engine.generate_jahresuebersicht(user_id, date.today().year),
    }


@protected.get("/api/tax/jahresuebersicht")
def tax_jahresuebersicht(jahr: int, user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/tax/jahresuebersicht", "GET")
    return tax_engine.generate_jahresuebersicht_detail(user_id, jahr)


@protected.get("/api/tax/config")
def get_tax_config(user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/tax/config", "GET")
    with get_session() as session:
        cfg = session.query(PosTaxConfig).filter_by(user_id=user_id).first()
        if not cfg:
            return {
                "freistellungsauftrag": FREISTELLUNGSAUFTRAG_DEFAULT, "freistellungsgenutzt": 0.0,
                "kirchensteuer": False, "verlusttopf_vorjahr": 0.0, "grenzsteuersatz": 0.42,
            }
        return {
            "freistellungsauftrag": cfg.freistellungsauftrag,
            "freistellungsgenutzt": cfg.freistellungsgenutzt,
            "kirchensteuer": cfg.kirchensteuer,
            "verlusttopf_vorjahr": cfg.verlusttopf_vorjahr,
            "grenzsteuersatz": cfg.grenzsteuersatz,
        }


@protected.put("/api/tax/config")
def update_tax_config(payload: dict, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, payload.get("user_id"), "/api/tax/config", "PUT")
    erlaubte_felder = {
        "freistellungsauftrag", "freistellungsgenutzt", "kirchensteuer",
        "verlusttopf_vorjahr", "grenzsteuersatz",
    }
    with get_session() as session:
        cfg = session.query(PosTaxConfig).filter_by(user_id=user_id).first()
        if not cfg:
            cfg = PosTaxConfig(user_id=user_id)
            session.add(cfg)
        for key, value in payload.items():
            if key in erlaubte_felder:
                setattr(cfg, key, value)
    return {"ok": True}


# ─────────────────────────────────────────────
# REBALANCING
# ─────────────────────────────────────────────

@protected.get("/api/rebalancing/analysis")
def rebalancing_analysis(user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    """
    Mathematischer Ist/Soll-Abgleich (pos_target_weights vs. aktuelles Portfolio)
    – siehe rebalancing.py Modulkommentar: reine Berechnung, keine Kauf-/
    Verkaufsempfehlung, jede Order platziert der Nutzer selbst beim Broker.
    """
    user_id = _resolve_user_id(current_user, user_id, "/api/rebalancing/analysis", "GET")
    with get_session() as session:
        user = session.get(PosUser, user_id)
        sparrate = user.monatliche_sparrate or 0.0

    return {
        "deviations": rebalancing.calculate_deviations(user_id),
        "sparrate": sparrate,
        "sparrate_empfehlung": rebalancing.get_sparrate_empfehlung(user_id, sparrate) if sparrate > 0 else [],
        "vollrebalancing": rebalancing.get_full_rebalance_orders(user_id),
    }


# ─────────────────────────────────────────────
# TRADING BOT
# ─────────────────────────────────────────────

@protected.get("/api/trading-bot")
def trading_bot_overview():
    account = trading_bot_connector.get_bot_account_value_eur()
    config = trading_bot_connector.get_bot_config_all()
    return {"account": account, "config": config}


@protected.get("/api/trading-bot/performance")
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


@protected.get("/api/trading-bot/trades")
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


ET_TZ = ZoneInfo("America/New_York")


@protected.get("/api/scan-log")
def get_scan_log(limit: int = 2000, ticker: str = None):
    """
    Scan-Historie gruppiert nach Tag → Slot (verschachtelt), für den
    aufklappbaren Scan-Historie-Unterreiter im Trading-Bot-Tab. Jeder Slot
    trägt bereits die Aggregate (total/above_threshold/trades/avg_score),
    damit die zugeklappte Ansicht ohne Client-seitige Berechnung auskommt.
    Tag-Gruppierung nach ET (Handelstag), nicht UTC – scan_time wird als UTC
    gespeichert (siehe main.py: datetime.utcnow()), ein Scan um z.B. 23:30 ET
    (03:30 UTC am Folgetag) würde sonst dem falschen Kalendertag zugeordnet.
    """
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                id, scan_time, slot_et, ticker, score,
                approved, instrument_type, current_price,
                rsi, rsi_score, sma_score, volume_score,
                pe_score, de_score, rev_score,
                ko_reason, guardrail_reason,
                trade_executed, trade_id, mode
            FROM scan_log
            WHERE (:ticker IS NULL OR ticker = :ticker)
            ORDER BY scan_time DESC
            LIMIT :limit
        """), {"ticker": ticker, "limit": limit}).fetchall()

        rows = [dict(r._mapping) for r in rows]

    # Tag → Slot (Scan-Zeitpunkt) → Ticker verschachteln.
    days: dict = {}
    for row in rows:
        scan_time = row["scan_time"]
        scan_time_et = scan_time.replace(tzinfo=ZoneInfo("UTC")).astimezone(ET_TZ) if scan_time else None
        day_key = scan_time_et.strftime("%Y-%m-%d") if scan_time_et else "?"
        slot_key = (scan_time.isoformat() if scan_time else "?", row["slot_et"])

        day = days.setdefault(day_key, {"date": day_key, "_slots": {}})
        slot = day["_slots"].setdefault(slot_key, {
            "slot": row["slot_et"],
            "scan_time": scan_time.isoformat() if scan_time else None,
            "total": 0, "above_threshold": 0, "trades": 0,
            "_score_sum": 0, "tickers": [],
        })
        slot["total"] += 1
        if row["approved"]:
            slot["above_threshold"] += 1
        if row["trade_executed"]:
            slot["trades"] += 1
        slot["_score_sum"] += row["score"] or 0
        slot["tickers"].append(row)

    result = []
    for day_key in sorted(days.keys(), reverse=True):
        day = days[day_key]
        slots = list(day["_slots"].values())
        for slot in slots:
            slot["avg_score"] = round(slot["_score_sum"] / slot["total"], 1) if slot["total"] else 0
            del slot["_score_sum"]
        slots.sort(key=lambda s: s["scan_time"] or "", reverse=True)
        result.append({"date": day_key, "slots": slots})

    return result


@protected.get("/api/scan-log/latest")
def get_latest_scan():
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT scan_time, slot_et,
                   COUNT(*) as total_scanned,
                   SUM(CASE WHEN approved THEN 1 ELSE 0 END) as approved_count,
                   SUM(CASE WHEN trade_executed THEN 1 ELSE 0 END) as trades_executed,
                   MAX(score) as max_score,
                   MIN(score) as min_score,
                   AVG(score) as avg_score
            FROM scan_log
            WHERE scan_time = (SELECT MAX(scan_time) FROM scan_log)
            GROUP BY scan_time, slot_et
        """)).fetchone()

    if not result:
        return {"message": "Noch keine Scans vorhanden"}

    return dict(result._mapping)


@protected.put("/api/trading-bot/config")
def update_trading_bot_config(werte: dict):
    trading_bot_connector.set_bot_config(werte)
    return trading_bot_connector.get_bot_config_all()


@protected.get("/api/bot-config")
def bot_config():
    return trading_bot_connector.get_bot_config_all()


@protected.put("/api/bot-config/{key}")
def update_bot_config_key(key: str, payload: dict):
    trading_bot_connector.set_bot_config({key: payload["value"]})
    return trading_bot_connector.get_bot_config_all()


# Risikoprofil-Presets für das Trading-Bot-Onboarding (siehe portfolio_react
# src/app/trading-bot/onboarding/page.tsx, Schritt 2). Werte identisch mit
# den Guardrail-Defaults, die trading_bot/database.py DEFAULT_CONFIG als
# hardcoded Fallback kennt.
BOT_CONFIG_PRESETS = {
    "konservativ": {
        "MAX_CAPITAL_PER_TRADE": "30",
        "MAX_OPEN_POSITIONS": "3",
        "ATR_MULTIPLIER_SL": "1.0",
        "ATR_MULTIPLIER_TP": "2.0",
        "MAX_HOLDING_DAYS": "3",
        "VOLATILE_SEGMENT_PCT": "0.0",
    },
    "ausgewogen": {
        "MAX_CAPITAL_PER_TRADE": "50",
        "MAX_OPEN_POSITIONS": "5",
        "ATR_MULTIPLIER_SL": "1.5",
        "ATR_MULTIPLIER_TP": "3.0",
        "MAX_HOLDING_DAYS": "5",
        "VOLATILE_SEGMENT_PCT": "0.33",
    },
    "aggressiv": {
        "MAX_CAPITAL_PER_TRADE": "100",
        "MAX_OPEN_POSITIONS": "8",
        "ATR_MULTIPLIER_SL": "2.0",
        "ATR_MULTIPLIER_TP": "4.0",
        "MAX_HOLDING_DAYS": "7",
        "VOLATILE_SEGMENT_PCT": "0.5",
    },
}


@protected.post("/api/bot-config/preset")
def apply_bot_config_preset(payload: dict):
    preset = payload.get("preset")
    if preset not in BOT_CONFIG_PRESETS:
        raise HTTPException(400, "Unbekanntes Preset")

    trading_bot_connector.set_bot_config(BOT_CONFIG_PRESETS[preset])

    return {"message": f"Preset '{preset}' angewendet",
            "settings": BOT_CONFIG_PRESETS[preset]}


@protected.get("/api/settings/monitoring-interval")
def get_monitoring_interval():
    """
    Gemeinsames Update-Intervall (Minuten) für Trading-Bot-SL/TP-Monitoring
    UND Portfolio-Preisupdate – ein Wert, ein Key (bot_config.MONITORING_INTERVAL_MIN).
    """
    cfg = trading_bot_connector.get_bot_config_all()
    return {"monitoring_interval_min": int(cfg.get("MONITORING_INTERVAL_MIN", 15))}


@protected.put("/api/settings/monitoring-interval")
def set_monitoring_interval(payload: dict):
    minuten = int(payload["monitoring_interval_min"])
    trading_bot_connector.set_bot_config({"MONITORING_INTERVAL_MIN": minuten})
    return {"monitoring_interval_min": minuten}


# ─────────────────────────────────────────────
# EINSTIEGSZEITPUNKTE (Entry-Time-Slots)
# ─────────────────────────────────────────────

@protected.get("/api/entry-time-slots")
def entry_time_slots():
    return trading_bot_connector.get_entry_time_slots()


@protected.put("/api/entry-time-slots/{slot_id}")
def update_entry_time_slot(slot_id: int, payload: dict):
    trading_bot_connector.update_entry_time_slot(
        slot_id, payload.get("gewichtung"), payload.get("aktiv")
    )
    return {"ok": True}


@protected.get("/api/entry-time-proposal")
def entry_time_proposal():
    return trading_bot_connector.get_pending_entry_proposal()


@protected.post("/api/entry-time-proposal/confirm")
def confirm_entry_time_proposal(payload: dict):
    proposal = trading_bot_connector.get_pending_entry_proposal()
    if not proposal:
        raise HTTPException(status_code=404, detail="Kein ausstehender Vorschlag")
    lernmodus = bool(payload.get("lernmodus", False))
    trading_bot_connector.apply_entry_time_proposal(proposal["vorschlaege"])
    trading_bot_connector.set_bot_config({"ENTRY_LEARNING_MODE": "true" if lernmodus else "false"})
    trading_bot_connector.clear_pending_entry_proposal("confirmed", lernmodus)
    return {"ok": True, "lernmodus": lernmodus}


@protected.post("/api/entry-time-proposal/reject")
def reject_entry_time_proposal():
    trading_bot_connector.clear_pending_entry_proposal("rejected")
    return {"ok": True}


# ─────────────────────────────────────────────
# IMMOBILIE
# ─────────────────────────────────────────────

@protected.get("/api/real-estate")
def real_estate(user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/real-estate", "GET")
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
                "adresse": decrypt_field(im.adresse),
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


@protected.post("/api/real-estate")
def create_real_estate(payload: dict, current_user=Depends(get_current_user)):
    from database import save_real_estate
    user_id = _resolve_user_id(current_user, payload.pop("user_id", None), "/api/real-estate", "POST")
    for feld in _REAL_ESTATE_DATE_FIELDS:
        if payload.get(feld):
            payload[feld] = datetime.strptime(payload[feld], "%Y-%m-%d").date()
    real_estate_id = save_real_estate(user_id, **payload)
    return {"id": real_estate_id}


@protected.delete("/api/real-estate/{real_estate_id}")
def remove_real_estate(real_estate_id: int, current_user=Depends(get_current_user)):
    from database import delete_real_estate
    try:
        owner_id = delete_real_estate(real_estate_id, owner_user_id=_owner_check_id(current_user))
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Immobilie {real_estate_id} nicht gefunden")
    _maybe_log_admin_access(current_user, owner_id, "/api/real-estate/{real_estate_id}", "DELETE")
    return {"ok": True}


# ─────────────────────────────────────────────
# FAMILIE
# ─────────────────────────────────────────────

@protected.get("/api/family")
def family(current_user=Depends(get_current_user)):
    """Aggregiert über ALLE pos_users, daher admin-only (ADMIN-SCOPE-TODO oben)."""
    _require_admin(current_user, "/api/family")
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

@protected.get("/api/haushaltsbuch")
def haushaltsbuch(user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/haushaltsbuch", "GET")
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


@protected.put("/api/haushaltsbuch/{buchung_id}")
def update_buchung(buchung_id: int, payload: dict, current_user=Depends(get_current_user)):
    with get_session() as session:
        buchung = session.get(PosBuchung, buchung_id)
        _require_buchung_access(buchung, current_user, "/api/haushaltsbuch/{buchung_id}", "PUT")
        kategorie = payload["kategorie"]
        buchung.kategorie = kategorie
        user_id = buchung.user_id
        empfaenger = buchung.empfaenger
    if payload.get("immer_so_kategorisieren") and empfaenger:
        add_kategorisierungsregel(user_id, empfaenger, kategorie)
    return {"ok": True}


@protected.post("/api/haushaltsbuch/upload")
async def haushaltsbuch_upload(user_id: int = Form(...), files: list[UploadFile] = File(...),
                                current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/haushaltsbuch/upload", "POST")
    return await _kontoauszug_import(user_id, files)


# ─────────────────────────────────────────────
# KI-ANALYSE (alles live LLM-Aufrufe – bewusst als POST/Action, nicht beim
# Laden der Seite; Antwortzeit kann mehrere Sekunden betragen)
# ─────────────────────────────────────────────

@protected.get("/api/ki-analyse/klumpenrisiko")
def klumpenrisiko(user_id: Optional[int] = None, schwelle_pct: float = 20.0,
                   current_user=Depends(get_current_user)):
    """Rein rechnerische Konzentrationsprüfung (keine LLM-Latenz) – größte
    Positionen als Anteil am Gesamtwert."""
    user_id = _resolve_user_id(current_user, user_id, "/api/ki-analyse/klumpenrisiko", "GET")
    pos = [p for p in portfolio_module.get_positions(user_id) if p["market_value"]]
    gesamt = sum(p["market_value"] for p in pos)
    if not gesamt:
        return {"positionen": [], "warnung": False}
    top = sorted(
        [{"name": p["name"], "anteil_pct": p["market_value"] / gesamt * 100} for p in pos],
        key=lambda p: -p["anteil_pct"],
    )[:5]
    return {"positionen": top, "warnung": any(p["anteil_pct"] > schwelle_pct for p in top)}


@protected.post("/api/ki-analyse/portfolio")
def ki_analyse_portfolio(payload: dict, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, payload.get("user_id"), "/api/ki-analyse/portfolio", "POST")
    return llm_analyst.analyze_portfolio(user_id)


@protected.post("/api/ki-analyse/quarterly-report")
def ki_quarterly_report(payload: dict, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, payload.get("user_id"), "/api/ki-analyse/quarterly-report", "POST")
    return llm_analyst.generate_quarterly_report(user_id)


@protected.post("/api/ki-analyse/ask")
def ki_ask(payload: dict, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, payload.get("user_id"), "/api/ki-analyse/ask", "POST")
    antwort = llm_analyst.answer_portfolio_question(user_id, payload["frage"])
    return {"antwort": antwort}


# ─────────────────────────────────────────────
# VERWALTUNG (bewusst auf die 3 im Redesign vorgesehenen Karten begrenzt:
# Portfolio anlegen, Transaktion, Kontoauszug/Screenshot-Import – nicht die
# volle CRUD-Oberfläche des Streamlit-Tabs)
# ─────────────────────────────────────────────

@protected.get("/api/portfolios")
def list_portfolios(user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    from database import PosPortfolio
    user_id = _resolve_user_id(current_user, user_id, "/api/portfolios", "GET")
    with get_session() as session:
        return [
            {"id": p.id, "name": p.name, "typ": p.typ, "broker": p.broker}
            for p in session.query(PosPortfolio).filter_by(user_id=user_id).all()
        ]


@protected.post("/api/portfolios")
def create_portfolio(payload: dict, current_user=Depends(get_current_user)):
    from database import PosPortfolio
    user_id = _resolve_user_id(current_user, payload.get("user_id"), "/api/portfolios", "POST")
    with get_session() as session:
        pf = PosPortfolio(
            user_id=user_id, name=payload["name"], typ=payload["typ"],
            broker=payload.get("broker"), is_kinderdepot=payload.get("is_kinderdepot", False),
        )
        session.add(pf)
        session.flush()
        return {"id": pf.id}


@protected.put("/api/portfolios/{portfolio_id}")
def edit_portfolio(portfolio_id: int, payload: dict, current_user=Depends(get_current_user)):
    try:
        owner_id = portfolio_module.update_portfolio(
            portfolio_id, name=payload.get("name"), typ=payload.get("typ"),
            broker=payload.get("broker"), is_kinderdepot=payload.get("is_kinderdepot"),
            owner_user_id=_owner_check_id(current_user),
        )
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} nicht gefunden")
    _maybe_log_admin_access(current_user, owner_id, "/api/portfolios/{portfolio_id}", "PUT")
    return {"ok": True}


@protected.delete("/api/portfolios/{portfolio_id}")
def remove_portfolio(portfolio_id: int, current_user=Depends(get_current_user)):
    try:
        owner_id = portfolio_module.delete_portfolio(portfolio_id, owner_user_id=_owner_check_id(current_user))
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} nicht gefunden")
    _maybe_log_admin_access(current_user, owner_id, "/api/portfolios/{portfolio_id}", "DELETE")
    return {"ok": True}


@protected.get("/api/asset-classes")
def list_asset_classes():
    with get_session() as session:
        return [
            {"id": ac.id, "name": ac.name, "slug": ac.slug}
            for ac in session.query(PosAssetClass).all()
        ]


@protected.get("/api/ticker-search")
def ticker_search(q: str):
    return portfolio_module.resolve_ticker(q)


@protected.post("/api/transactions")
def create_transaction(payload: dict, current_user=Depends(get_current_user)):
    _require_portfolio_access(payload["portfolio_id"], current_user, "/api/transactions", "POST")
    datum = datetime.strptime(payload["datum"], "%Y-%m-%d").date()
    return portfolio_module.add_transaction(
        portfolio_id=payload["portfolio_id"], typ=payload["typ"], ticker=payload["ticker"],
        quantity=payload["quantity"], price=payload["price"], datum=datum,
        fees=payload.get("fees", 0.0), asset_class_id=payload.get("asset_class_id"),
    )


@protected.put("/api/transactions/{transaction_id}")
def edit_transaction(transaction_id: int, payload: dict, current_user=Depends(get_current_user)):
    datum = datetime.strptime(payload["datum"], "%Y-%m-%d").date() if payload.get("datum") else None
    try:
        owner_id = portfolio_module.update_transaction(
            transaction_id, typ=payload.get("typ"), quantity=payload.get("quantity"),
            price=payload.get("price"), datum=datum, fees=payload.get("fees"),
            owner_user_id=_owner_check_id(current_user),
        )
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Transaktion {transaction_id} nicht gefunden")
    _maybe_log_admin_access(current_user, owner_id, "/api/transactions/{transaction_id}", "PUT")
    return {"ok": True}


@protected.delete("/api/transactions/{transaction_id}")
def remove_transaction(transaction_id: int, current_user=Depends(get_current_user)):
    try:
        owner_id = portfolio_module.delete_transaction(transaction_id, owner_user_id=_owner_check_id(current_user))
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Transaktion {transaction_id} nicht gefunden")
    _maybe_log_admin_access(current_user, owner_id, "/api/transactions/{transaction_id}", "DELETE")
    return {"ok": True}


@protected.post("/api/depot/import-csv")
async def depot_import_csv(portfolio_id: int = Form(...), broker: str = Form(...), file: UploadFile = File(...),
                            current_user=Depends(get_current_user)):
    """
    Importiert eine Transaktionshistorie-CSV (Comdirect/Trade Republic/ING/DKB/
    Sonstige) in ein Depot – siehe portfolio_module.import_csv() für die
    Spaltenerkennung je Broker. Nutzt dieselbe Funktion wie der bestehende
    CSV-Import im Streamlit-Dashboard (dashboard.py), nur über einen Datei-
    Upload statt eines lokalen Pfads.
    """
    _require_portfolio_access(portfolio_id, current_user, "/api/depot/import-csv", "POST")
    content = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        return portfolio_module.import_csv(portfolio_id, tmp_path, broker)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.unlink(tmp_path)


@protected.post("/api/positions/tagesgeld")
def add_tagesgeld_position(payload: dict, current_user=Depends(get_current_user)):
    """
    Legt eine Tagesgeld-Position an oder aktualisiert ihren Kontostand (siehe
    Feature 4: kein Ticker nötig, quantity ist direkt der Betrag).
    """
    _require_portfolio_access(payload["portfolio_id"], current_user, "/api/positions/tagesgeld", "POST")
    return portfolio_module.upsert_tagesgeld_position(
        portfolio_id=payload["portfolio_id"],
        konto_name=payload["konto_name"],
        betrag=payload["betrag"],
        zinssatz=payload.get("zinssatz"),
    )


@protected.post("/api/target-weights")
def set_target_weight(payload: dict, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, payload.get("user_id"), "/api/target-weights", "POST")
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


# Feste Assetklassen-Auswahl für die Zielgewichtung-Sektion im Verwaltung-Tab
# (bewusst eine Untermenge von pos_asset_classes – legacy/duplizierte Klassen
# wie "Aktien"/"Konto-Cash"/"Sonstiges" tauchen dort nicht auf).
TARGET_WEIGHT_ASSET_CLASSES = [2, 8, 9, 10, 11, 5, 4]  # ETF, Einzelaktie, Anleihe, Gold/Rohstoff, Tagesgeld, Immobilie, Krypto


@protected.get("/api/target-weights")
def get_target_weights(user_id: Optional[int] = None, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/target-weights", "GET")
    with get_session() as session:
        bestehend = {
            tw.asset_class_id: tw.target_pct
            for tw in session.query(PosTargetWeight).filter_by(user_id=user_id).all()
        }
    return [
        {"asset_class_id": acid, "target_pct": bestehend.get(acid, 0.0)}
        for acid in TARGET_WEIGHT_ASSET_CLASSES
    ]


@protected.put("/api/target-weights")
def set_target_weights(payload: dict, current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, payload.get("user_id"), "/api/target-weights", "PUT")
    weights = payload["weights"]
    with get_session() as session:
        for asset_class_id_raw, target_pct in weights.items():
            asset_class_id = int(asset_class_id_raw)
            min_pct = max(0.0, target_pct - 0.05)
            max_pct = min(1.0, target_pct + 0.05)
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
    """
    Analysiert Kontoauszüge und speichert die erkannten Buchungen (Haushaltsbuch).
    CSV-Dateien werden direkt geparst (kein KI-Call nötig, siehe
    kontoauszug_analyzer.parse_csv_kontoauszug), PDFs laufen weiterhin über die
    bestehende KI-Analyse (Text oder Vision, siehe analyze_kontoauszuege).
    Vorher wurde hier nur analysiert, nie gespeichert – die Buchungen
    verschwanden nach dem Request wieder.
    """
    csv_files, pdf_files = [], []
    for f in files:
        content = await f.read()
        if (f.filename or "").lower().endswith(".csv"):
            csv_files.append((f.filename, content))
        else:
            pdf_files.append((f.filename, content))

    result = kontoauszug_analyzer.analyze_kontoauszuege(pdf_files)

    csv_buchungen = []
    for _dateiname, content in csv_files:
        daten = kontoauszug_analyzer.parse_csv_kontoauszug(content)
        csv_buchungen.extend(daten.get("buchungen") or [])

    if csv_buchungen:
        result["verfuegbar"] = True
        result["buchungen"] = csv_buchungen + result.get("buchungen", [])

    if result.get("verfuegbar") and result.get("buchungen"):
        result["gespeichert"] = save_buchungen(user_id, result["buchungen"])
    return result


@protected.post("/api/kontoauszug-import")
async def kontoauszug_import(user_id: int = Form(...), files: list[UploadFile] = File(...),
                              current_user=Depends(get_current_user)):
    user_id = _resolve_user_id(current_user, user_id, "/api/kontoauszug-import", "POST")
    return await _kontoauszug_import(user_id, files)


@protected.post("/api/screenshot-import")
async def screenshot_import(user_id: int = Form(...), file: UploadFile = File(...),
                             current_user=Depends(get_current_user)):
    """Liest einen Portfolio-Screenshot per Claude Vision aus (siehe llm_analyst.py)
    und gibt die erkannten Positionen zur Bestätigung durch den Nutzer zurück –
    speichert bewusst NICHT automatisch (wie /api/kontoauszug-import)."""
    user_id = _resolve_user_id(current_user, user_id, "/api/screenshot-import", "POST")
    file_bytes = await file.read()
    positionen = llm_analyst.analyze_portfolio_screenshot(file_bytes, file.content_type or "image/jpeg")
    return {"positionen": positionen}


# ─────────────────────────────────────────────
# HEALTH / CONFIG
# ─────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"ok": True, "warnungen": validate_config(), "base_url": BASE_URL}


app.include_router(protected)
app.include_router(protected_shared)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8503)
