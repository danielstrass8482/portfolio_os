"""
database.py – SQLAlchemy-Modelle und Datenbankzugriff für Portfolio-OS.
Nutzt dieselbe Postgres-Instanz wie der Trading Bot. Alle Tabellen tragen
das Präfix "pos_", damit es keine Konflikte mit den Trading-Bot-Tabellen gibt.
"""

from datetime import datetime, date
from contextlib import contextmanager
import json
import os

from cryptography.fernet import Fernet
from sqlalchemy import (
    create_engine, Column, Integer, Float, String, Boolean,
    DateTime, Date, Text, ForeignKey, func, JSON
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

from config import DATABASE_URL, DEFAULT_ASSET_CLASSES, FREISTELLUNGSAUFTRAG_DEFAULT

Base = declarative_base()

# ─────────────────────────────────────────────
# VERSCHLÜSSELUNG SENSIBLER FELDER (z.B. pos_real_estate.adresse)
# ─────────────────────────────────────────────
# Ohne ENCRYPTION_KEY (z.B. lokal ohne .env) bleibt fernet None – encrypt_field/
# decrypt_field werden dann zu No-Ops statt abzustürzen.
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
fernet = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None


def encrypt_field(value: str) -> str:
    if not fernet or not value:
        return value
    return fernet.encrypt(value.encode()).decode()


def decrypt_field(value: str) -> str:
    if not fernet or not value:
        return value
    try:
        return fernet.decrypt(value.encode()).decode()
    except Exception:
        # Vor der Verschlüsselungseinführung gespeicherte Klartext-Werte sind
        # kein gültiges Fernet-Token – unverändert zurückgeben statt abzustürzen.
        return value


def sanitize_csv_field(value: str) -> str:
    """CSV-Injection-Schutz: Felder aus Kontoauszug-/CSV-Importen (z.B. Empfänger,
    Verwendungszweck), die später in Excel/Sheets geöffnet werden könnten, dürfen
    nicht mit einem Formel-Trigger beginnen."""
    if value and value[0] in ("=", "@", "+", "-", "\t", "\r"):
        return "'" + value
    return value
# pool_pre_ping: verwirft tote Connections (z.B. "SSL connection has been closed
# unexpectedly" nach DB-seitigem Idle-Timeout) vor der Nutzung statt mit ihnen
# fehlzuschlagen. pool_recycle: ersetzt Connections vorsorglich nach 280s.
engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True, pool_recycle=280)
# expire_on_commit=False: geladene Attribute bleiben nach commit()/close() im
# Objekt gecacht statt sich zu "expiren" – verhindert DetachedInstanceError,
# wenn ORM-Objekte (z.B. im Dashboard) außerhalb ihres "with get_session()"-Blocks
# gelesen werden. Ersetzt NICHT die Notwendigkeit, Relationships/Lazy-Loads
# weiterhin innerhalb der Session aufzulösen.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# ─────────────────────────────────────────────
# MODELLE
# ─────────────────────────────────────────────

class PosUser(Base):
    """Ein Familienmitglied / Nutzer des Systems."""
    __tablename__ = "pos_users"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    name       = Column(String(100), nullable=False)
    email      = Column(String(200), nullable=True)
    rolle      = Column(String(20), default="member")   # admin / member
    created_at = Column(DateTime, default=datetime.utcnow)

    # Login (siehe Security-Layer: JWT-Auth in api.py)
    password_hash = Column(Text, nullable=True)
    last_login    = Column(DateTime, nullable=True)

    # Registrierung mit Admin-Freischaltung (siehe /api/auth/register in api.py)
    status                 = Column(String(20), default="active")   # pending / active / rejected
    registration_reason    = Column(Text, nullable=True)
    approval_token         = Column(String(100), nullable=True)
    approval_token_expires = Column(DateTime, nullable=True)

    # Alpaca-Anbindung pro Nutzer (verschlüsselt, siehe encrypt_field/decrypt_field oben)
    alpaca_api_key_encrypted    = Column(Text, nullable=True)
    alpaca_secret_key_encrypted = Column(Text, nullable=True)
    alpaca_mode                 = Column(String(10), default="paper")   # paper / live

    # Onboarding / Risikoprofil (siehe onboarding.py)
    onboarding_completed = Column(Boolean, default=False)
    alter_jahre           = Column(Integer, nullable=True)
    familienstand          = Column(Text, nullable=True)
    monatliche_sparrate    = Column(Float, default=0.0)
    anlagehorizont_jahre   = Column(Integer, nullable=True)
    risikoprofil           = Column(Text, nullable=True)   # konservativ/ausgewogen/wachstum/aggressiv
    risikoscore            = Column(Integer, nullable=True)  # 0-10

    portfolios = relationship("PosPortfolio", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<PosUser {self.name} ({self.rolle})>"


class PosPortfolio(Base):
    """Ein Depot/Konto (z.B. Comdirect-Depot, Binance-Wallet, Girokonto)."""
    __tablename__ = "pos_portfolios"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    user_id        = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    name           = Column(String(150), nullable=False)
    broker         = Column(String(100), nullable=True)
    typ            = Column(String(20), nullable=False)   # depot / krypto / immobilie / konto
    is_kinderdepot = Column(Boolean, default=False)

    user      = relationship("PosUser", back_populates="portfolios")
    positions = relationship("PosPosition", back_populates="portfolio", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<PosPortfolio {self.name} ({self.typ})>"


class PosAssetClass(Base):
    """Assetklasse, z.B. Aktien, ETF, Krypto – mit optionaler Unterklasse via parent_id."""
    __tablename__ = "pos_asset_classes"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    name      = Column(String(100), nullable=False)
    slug      = Column(String(100), nullable=False, unique=True)
    parent_id = Column(Integer, ForeignKey("pos_asset_classes.id"), nullable=True)

    parent = relationship("PosAssetClass", remote_side=[id])

    def __repr__(self):
        return f"<PosAssetClass {self.name}>"


class PosPosition(Base):
    """Eine gehaltene Position (Ticker) innerhalb eines Portfolios."""
    __tablename__ = "pos_positions"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id   = Column(Integer, ForeignKey("pos_portfolios.id"), nullable=False)
    asset_class_id = Column(Integer, ForeignKey("pos_asset_classes.id"), nullable=True)
    # nullable=True seit Feature 4: Tagesgeld-Positionen haben keinen Ticker
    # (siehe upsert_tagesgeld_position() in portfolio.py).
    ticker         = Column(String(20), nullable=True)
    name           = Column(String(200), nullable=True)
    display_name   = Column(Text, nullable=True)
    quantity       = Column(Float, default=0.0)
    avg_buy_price  = Column(Float, default=0.0)
    current_price  = Column(Float, nullable=True)
    currency       = Column(String(10), default="EUR")
    last_updated   = Column(DateTime, nullable=True)

    portfolio   = relationship("PosPortfolio", back_populates="positions")
    asset_class = relationship("PosAssetClass")
    transactions = relationship("PosTransaction", back_populates="position", cascade="all, delete-orphan")

    @property
    def market_value(self) -> float:
        price = self.current_price if self.current_price is not None else self.avg_buy_price
        return (price or 0.0) * (self.quantity or 0.0)

    @property
    def unrealized_pnl(self) -> float:
        if self.current_price is None:
            return 0.0
        return (self.current_price - self.avg_buy_price) * self.quantity

    def __repr__(self):
        return f"<PosPosition {self.ticker} qty={self.quantity}>"


class PosTransaction(Base):
    """Kauf/Verkauf/Dividende/Sparrate – jede Bewegung einer Position."""
    __tablename__ = "pos_transactions"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("pos_portfolios.id"), nullable=False)
    position_id  = Column(Integer, ForeignKey("pos_positions.id"), nullable=True)
    typ          = Column(String(20), nullable=False)   # kauf / verkauf / dividende / sparrate
    datum        = Column(Date, default=date.today)
    quantity     = Column(Float, default=0.0)
    price        = Column(Float, default=0.0)
    fees         = Column(Float, default=0.0)
    steuern      = Column(Float, default=0.0)

    portfolio = relationship("PosPortfolio")
    position  = relationship("PosPosition", back_populates="transactions")

    def __repr__(self):
        return f"<PosTransaction {self.typ} {self.quantity}@{self.price}>"


class PosTargetWeight(Base):
    """Ziel-Gewichtung einer Assetklasse für einen Nutzer."""
    __tablename__ = "pos_target_weights"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    user_id        = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    asset_class_id = Column(Integer, ForeignKey("pos_asset_classes.id"), nullable=False)
    target_pct     = Column(Float, nullable=False)
    min_pct        = Column(Float, nullable=True)
    max_pct        = Column(Float, nullable=True)

    asset_class = relationship("PosAssetClass")

    def __repr__(self):
        return f"<PosTargetWeight asset_class_id={self.asset_class_id} target={self.target_pct}>"


class PosGoal(Base):
    """Ein finanzielles Ziel eines Nutzers (Altersvorsorge, Immobilie, ...), aus dem Onboarding."""
    __tablename__ = "pos_goals"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    user_id           = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    name              = Column(Text, nullable=False)
    typ               = Column(Text, nullable=True)   # rente/immobilie/studium/sonstiges
    zielbetrag        = Column(Float, nullable=False)
    zeitraum_jahre    = Column(Integer, nullable=False)
    prioritaet        = Column(Text, default="haupt")   # haupt/neben
    erwartete_rendite = Column(Float, default=0.06)
    # Anteil der monatlichen Gesamtsparrate (in %), der diesem Ziel im Sparplan-Rechner
    # (onboarding.py Schritt 4) zugewiesen wurde. None = noch nicht zugewiesen.
    sparrate_anteil_pct = Column(Float, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PosGoal {self.name} Ziel={self.zielbetrag}>"


class PosInvestmentPreference(Base):
    """Anlagepräferenzen eines Nutzers (Assetklassen, ETF/Aktien-Präferenzen, Aus-/Einschlusskriterien)."""
    __tablename__ = "pos_investment_preferences"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    user_id             = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    aktive_assetklassen = Column(JSON, nullable=True)   # ["etf", "stocks", "gold", ...]
    etf_fokus           = Column(Text, nullable=True)   # world/em/europa/sektoren
    etf_ausschuettend   = Column(Boolean, default=False)
    aktien_strategie    = Column(Text, nullable=True)   # dividende/wachstum/beides
    blacklist           = Column(JSON, nullable=True)   # ["waffen", "tabak", "fossil", ...]
    whitelist           = Column(JSON, nullable=True)   # ["esg", "tech", "healthcare", ...]
    whitelist_branchen  = Column(JSON, nullable=True)   # bevorzugte Branchen ["esg", "technologie", ...]
    updated_at          = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<PosInvestmentPreference user_id={self.user_id}>"


class PosTaxConfig(Base):
    """Steuerliche Einstellungen je Nutzer."""
    __tablename__ = "pos_tax_config"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    user_id               = Column(Integer, ForeignKey("pos_users.id"), nullable=False, unique=True)
    kirchensteuer         = Column(Boolean, default=False)
    freistellungsauftrag  = Column(Float, default=FREISTELLUNGSAUFTRAG_DEFAULT)
    freistellungsgenutzt  = Column(Float, default=0.0)
    verlusttopf_vorjahr   = Column(Float, default=0.0)
    grenzsteuersatz       = Column(Float, default=0.42)  # für AfA-Berechnung bei Immobilien

    def __repr__(self):
        return f"<PosTaxConfig user_id={self.user_id}>"


class PosTaxEvent(Base):
    """Steuerlich relevantes Ereignis (i.d.R. aus einem Verkauf entstanden)."""
    __tablename__ = "pos_tax_events"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    user_id        = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    transaction_id = Column(Integer, ForeignKey("pos_transactions.id"), nullable=True)
    gewinn_verlust = Column(Float, nullable=False)
    steuer_betrag  = Column(Float, default=0.0)
    datum          = Column(Date, default=date.today)

    transaction = relationship("PosTransaction")

    def __repr__(self):
        return f"<PosTaxEvent {self.gewinn_verlust} Steuer={self.steuer_betrag}>"


class PosRealEstate(Base):
    """Eine Immobilie (Eigennutzung oder Vermietung)."""
    __tablename__ = "pos_real_estate"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    user_id            = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    adresse            = Column(String(300), nullable=False)
    kaufpreis          = Column(Float, nullable=False)
    kaufjahr           = Column(Integer, nullable=True)

    # Kaufpreisaufteilung für die AfA (§7 EStG: der Grundstücksanteil ist nicht
    # abschreibungsfähig, nur das Gebäude bzw. bescheinigte Sanierungskosten
    # dürfen als Abschreibungsbasis dienen – siehe abschreibungsbasis unten).
    grundstuecksanteil = Column(Float, default=0.0)
    gebaeudewert       = Column(Float, default=0.0)
    kaufpreis_gesamt   = Column(Float, default=0.0)
    sanierungskosten   = Column(Float, default=0.0)
    wohnflaeche_qm     = Column(Float, nullable=True)
    eigenkapital       = Column(Float, default=0.0)
    restschuld         = Column(Float, default=0.0)
    monatliche_rate    = Column(Float, default=0.0)
    mieteinnahmen      = Column(Float, default=0.0)
    letzter_schaetzwert = Column(Float, nullable=True)
    letztes_update     = Column(DateTime, nullable=True)

    # Finanzierung & Vermietung
    vermietung_start           = Column(Date, nullable=True)
    kredit_gesamtbetrag        = Column(Float, default=0.0)
    kredit_abgerufen           = Column(Float, default=0.0)
    kredit_zinssatz            = Column(Float, default=0.0)
    kredit_laufzeit_jahre      = Column(Integer, default=0)
    vorfaelligkeitsgebuehr_pct = Column(Float, default=0.0)
    zinsbindung_bis            = Column(Date, nullable=True)
    finanzierungskosten        = Column(Float, default=0.0)

    # Abschreibung
    abschreibungsart   = Column(Text, default="Keine")
    abschreibungsbasis = Column(Float, default=0.0)
    abschreibungssatz  = Column(Float, default=0.0)
    kaufdatum          = Column(Date, nullable=True)

    def __repr__(self):
        return f"<PosRealEstate {self.adresse}>"


class PosRebalancingProposal(Base):
    """Ein vom System erzeugter Rebalancing-Vorschlag."""
    __tablename__ = "pos_rebalancing_proposals"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    erstellt_am   = Column(DateTime, default=datetime.utcnow)
    status        = Column(String(20), default="pending")   # pending / confirmed / rejected
    vorschlag_json = Column(Text, nullable=True)
    begruendung   = Column(Text, nullable=True)
    ki_analyse    = Column(Text, nullable=True)

    def get_vorschlag(self) -> dict:
        if self.vorschlag_json:
            try:
                return json.loads(self.vorschlag_json)
            except json.JSONDecodeError:
                return {}
        return {}

    def set_vorschlag(self, vorschlag: dict):
        self.vorschlag_json = json.dumps(vorschlag, ensure_ascii=False, default=str)

    def __repr__(self):
        return f"<PosRebalancingProposal {self.id} {self.status}>"


class PosFamilyGoal(Base):
    """Ein gemeinsames Sparziel der Familie (Notgroschen, Kinderstudium, ...)."""
    __tablename__ = "pos_family_goals"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    name          = Column(String(150), nullable=False)
    ziel_betrag   = Column(Float, nullable=False)
    aktuell_betrag = Column(Float, default=0.0)
    zieldatum     = Column(Date, nullable=True)
    beschreibung  = Column(Text, nullable=True)

    @property
    def fortschritt_pct(self) -> float:
        if not self.ziel_betrag:
            return 0.0
        return min(100.0, (self.aktuell_betrag or 0.0) / self.ziel_betrag * 100)

    def __repr__(self):
        return f"<PosFamilyGoal {self.name} {self.fortschritt_pct:.0f}%>"


class PosDailySnapshot(Base):
    """Täglicher Schnappschuss des Gesamtvermögens eines Nutzers (für Performance/Charts)."""
    __tablename__ = "pos_daily_snapshots"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    user_id             = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    datum               = Column(Date, default=date.today)
    gesamtvermoegen     = Column(Float, nullable=False)
    asset_breakdown_json = Column(Text, nullable=True)

    def get_breakdown(self) -> dict:
        if self.asset_breakdown_json:
            try:
                return json.loads(self.asset_breakdown_json)
            except json.JSONDecodeError:
                return {}
        return {}

    def set_breakdown(self, breakdown: dict):
        self.asset_breakdown_json = json.dumps(breakdown, ensure_ascii=False, default=str)

    def __repr__(self):
        return f"<PosDailySnapshot {self.datum} {self.gesamtvermoegen}>"


class PosBuchung(Base):
    """Eine Buchung (Einnahme/Ausgabe) aus einem analysierten Kontoauszug (Haushaltsbuch)."""
    __tablename__ = "pos_buchungen"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    user_id           = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    datum             = Column(Date, nullable=False)
    betrag            = Column(Float, nullable=False)
    empfaenger        = Column(Text, nullable=True)
    verwendungszweck  = Column(Text, nullable=True)
    kategorie         = Column(Text, nullable=True)
    typ               = Column(Text, nullable=True)   # einnahme / ausgabe
    quelle            = Column(Text, default="kontoauszug")
    created_at        = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PosBuchung {self.datum} {self.betrag} {self.empfaenger}>"


class PosKategorisierungsregel(Base):
    """Regel 'wenn Empfänger X enthält, dann Kategorie Y' – aus dem Haushaltsbuch-Tab
    ('Immer so kategorisieren'), wird bei künftigen Kontoauszug-Uploads angewendet."""
    __tablename__ = "pos_kategorisierungsregeln"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    user_id             = Column(Integer, ForeignKey("pos_users.id"), nullable=False)
    empfaenger_contains = Column(Text, nullable=False)
    kategorie           = Column(Text, nullable=False)
    created_at          = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PosKategorisierungsregel {self.empfaenger_contains} -> {self.kategorie}>"


# ─────────────────────────────────────────────
# SESSION / INIT
# ─────────────────────────────────────────────

@contextmanager
def get_session():
    """Context Manager für sichere Datenbanksessions (Commit/Rollback automatisch)."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_session_for_user(user_id: int):
    """
    Wie get_session(), setzt zusätzlich app.current_user_id für die Dauer der
    Transaktion (SET LOCAL via set_config() – kein String-Interpolation, damit
    keine SQL-Injection möglich ist). Voraussetzung für die Row-Level-Security-
    Policies auf den pos_*-Tabellen (siehe Security Schritt 2).

    WICHTIG: trading_bot_user ist Owner der pos_*-Tabellen, und Postgres wendet
    RLS-Policies per Default NICHT auf den Tabellenbesitzer an (nur zusätzlich
    mit FORCE ROW LEVEL SECURITY). Die Policies sind aktuell also Vorbereitung
    für echtes Multi-Tenant, noch OHNE Wirkung – und diese Funktion wird von
    den bestehenden API-Endpoints noch NICHT verwendet (die weiterhin
    get_session() nutzen). Erst wenn ein zweiter echter Login-Nutzer dazukommt,
    lohnt sich der Aufwand, alle relevanten Endpoints hierauf umzustellen UND
    FORCE ROW LEVEL SECURITY zu setzen.
    """
    from sqlalchemy import text
    session = SessionLocal()
    try:
        session.execute(text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)})
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """Erstellt alle pos_*-Tabellen (idempotent – safe to call multiple times)."""
    Base.metadata.create_all(engine)
    _migrate_real_estate_columns()
    _migrate_goal_columns()
    _migrate_tax_config_columns()
    _migrate_position_columns()
    _migrate_user_columns()
    with get_session() as session:
        _seed_asset_classes(session)


def _migrate_user_columns():
    """Idempotente Migration für pos_users.password_hash/last_login sowie die
    Registrierungs-Approval- und Alpaca-Connect-Spalten (siehe _migrate_real_estate_columns).
    DEFAULT 'active' greift dank ADD COLUMN ... DEFAULT auch rückwirkend für
    bereits bestehende Zeilen (z.B. Daniels Account, id=1) – kein Extra-UPDATE nötig."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS password_hash TEXT"))
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP"))
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'"))
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS registration_reason TEXT"))
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS approval_token TEXT"))
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS approval_token_expires TIMESTAMP"))
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS alpaca_api_key_encrypted TEXT"))
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS alpaca_secret_key_encrypted TEXT"))
        conn.execute(text("ALTER TABLE pos_users ADD COLUMN IF NOT EXISTS alpaca_mode TEXT DEFAULT 'paper'"))


def _migrate_tax_config_columns():
    """Idempotente Migration für pos_tax_config.grenzsteuersatz (siehe _migrate_real_estate_columns)."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE pos_tax_config ADD COLUMN IF NOT EXISTS grenzsteuersatz FLOAT DEFAULT 0.42"
        ))


def _migrate_position_columns():
    """
    Base.metadata.create_all() ändert keine Spalten einer bereits bestehenden
    Tabelle – pos_positions.ticker war ursprünglich NOT NULL, ist aber seit
    Feature 4 (Tagesgeld-Positionen ohne Ticker) nullable. Idempotent (Postgres
    macht DROP NOT NULL beim zweiten Aufruf einfach nochmal, ohne Fehler).
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE pos_positions ALTER COLUMN ticker DROP NOT NULL"))


def _migrate_goal_columns():
    """Idempotente Migration für pos_goals.sparrate_anteil_pct (siehe _migrate_real_estate_columns)."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE pos_goals ADD COLUMN IF NOT EXISTS sparrate_anteil_pct FLOAT"
        ))


def _migrate_real_estate_columns():
    """
    Base.metadata.create_all() legt nur FEHLENDE Tabellen an, ändert aber keine
    Spalten einer bereits bestehenden Tabelle. Für neu hinzugekommene Spalten auf
    pos_real_estate (Kaufpreisaufteilung Grundstück/Gebäude für die AfA) daher
    ein idempotentes ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
    """
    from sqlalchemy import text
    statements = [
        "ALTER TABLE pos_real_estate ADD COLUMN IF NOT EXISTS grundstuecksanteil FLOAT DEFAULT 0",
        "ALTER TABLE pos_real_estate ADD COLUMN IF NOT EXISTS gebaeudewert FLOAT DEFAULT 0",
        "ALTER TABLE pos_real_estate ADD COLUMN IF NOT EXISTS kaufpreis_gesamt FLOAT DEFAULT 0",
        "ALTER TABLE pos_real_estate ADD COLUMN IF NOT EXISTS sanierungskosten FLOAT DEFAULT 0",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def _seed_asset_classes(session: Session):
    """Legt Standard-Assetklassen an, falls noch keine existieren."""
    existing = {row.slug for row in session.query(PosAssetClass.slug).all()}
    for name in DEFAULT_ASSET_CLASSES:
        slug = name.lower().replace("/", "-").replace(" ", "-")
        if slug not in existing:
            session.add(PosAssetClass(name=name, slug=slug))


def get_or_create_user(session: Session, name: str, email: str = None, rolle: str = "member") -> PosUser:
    """Holt einen Nutzer per Name oder legt ihn (samt Steuer-Config) neu an."""
    user = session.query(PosUser).filter_by(name=name).first()
    if user:
        return user
    user = PosUser(name=name, email=email, rolle=rolle)
    session.add(user)
    session.flush()
    session.add(PosTaxConfig(user_id=user.id))
    return user


def get_asset_class_by_slug(session: Session, slug: str) -> PosAssetClass:
    return session.query(PosAssetClass).filter_by(slug=slug).first()


def save_real_estate(user_id: int, real_estate_id: int = None, **felder) -> int:
    """
    Legt eine Immobilie an (real_estate_id=None) oder aktualisiert eine
    bestehende (real_estate_id gesetzt) – z.B. für die KI-Auswertung eines
    Kreditvertrags, die nur einzelne Felder einer bereits angelegten
    Immobilie nachträgt. Gibt die id der Immobilie zurück.
    """
    with get_session() as session:
        if real_estate_id is not None:
            obj = session.get(PosRealEstate, real_estate_id)
            if obj is None:
                raise ValueError(f"Immobilie {real_estate_id} nicht gefunden")
        else:
            obj = PosRealEstate(user_id=user_id)
            session.add(obj)
        for key, value in felder.items():
            if key == "adresse" and value:
                value = encrypt_field(value)
            setattr(obj, key, value)
        session.flush()
        return obj.id


def update_real_estate(real_estate_id: int, **kwargs):
    """
    Aktualisiert eine bestehende Immobilie – nur übergebene, nicht-None Felder
    werden geändert (im Unterschied zu save_real_estate() also sicher für
    Bearbeiten-Formulare, bei denen einzelne optionale Felder leer bleiben
    können, ohne dass dadurch bereits gespeicherte Werte überschrieben werden).
    """
    with get_session() as session:
        obj = session.query(PosRealEstate).filter_by(id=real_estate_id).first()
        if obj is None:
            raise ValueError(f"Immobilie {real_estate_id} nicht gefunden")
        for key, value in kwargs.items():
            if hasattr(obj, key) and value is not None:
                if key == "adresse":
                    value = encrypt_field(value)
                setattr(obj, key, value)
        session.commit()
        return obj


def delete_real_estate(real_estate_id: int):
    """Löscht eine Immobilie unwiderruflich."""
    with get_session() as session:
        obj = session.query(PosRealEstate).filter_by(id=real_estate_id).first()
        if obj is None:
            raise ValueError(f"Immobilie {real_estate_id} nicht gefunden")
        session.delete(obj)


def save_daily_snapshot(session: Session, user_id: int, gesamtvermoegen: float, asset_breakdown: dict = None):
    """Speichert oder aktualisiert den täglichen Vermögens-Snapshot eines Nutzers (für Performance/Charts)."""
    today = date.today()
    existing = session.query(PosDailySnapshot).filter_by(user_id=user_id, datum=today).first()
    if existing:
        existing.gesamtvermoegen = gesamtvermoegen
        if asset_breakdown is not None:
            existing.set_breakdown(asset_breakdown)
    else:
        snap = PosDailySnapshot(user_id=user_id, datum=today, gesamtvermoegen=gesamtvermoegen)
        if asset_breakdown is not None:
            snap.set_breakdown(asset_breakdown)
        session.add(snap)


def save_buchungen(user_id: int, buchungen: list) -> int:
    """
    Speichert per KI erkannte Kontoauszug-Buchungen (siehe kontoauszug_analyzer.py)
    für einen Nutzer. Dedupliziert gegen bereits gespeicherte Buchungen (gleiche
    datum+betrag+empfaenger) und wendet vorhandene Kategorisierungsregeln
    (pos_kategorisierungsregeln, "Immer so kategorisieren" im Haushaltsbuch-Tab)
    auf den Empfänger an, bevor gespeichert wird. Gibt die Anzahl NEU
    gespeicherter Buchungen zurück (Duplikate werden übersprungen, kein Fehler).
    """
    with get_session() as session:
        regeln = session.query(PosKategorisierungsregel).filter_by(user_id=user_id).all()
        vorhandene = {
            (b.datum, round(b.betrag, 2), (b.empfaenger or "").strip().lower())
            for b in session.query(PosBuchung).filter_by(user_id=user_id).all()
        }
        neu = 0
        for b in buchungen:
            rohdatum = b.get("datum")
            try:
                datum = rohdatum if isinstance(rohdatum, date) else date.fromisoformat(str(rohdatum))
            except (ValueError, TypeError):
                continue
            betrag = float(b.get("betrag") or 0.0)
            empfaenger = (b.get("empfaenger") or "").strip()
            key = (datum, round(betrag, 2), empfaenger.lower())
            if key in vorhandene:
                continue
            vorhandene.add(key)

            kategorie = b.get("kategorie")
            for regel in regeln:
                if regel.empfaenger_contains.lower() in empfaenger.lower():
                    kategorie = regel.kategorie
                    break

            session.add(PosBuchung(
                user_id=user_id, datum=datum, betrag=betrag,
                empfaenger=sanitize_csv_field(empfaenger),
                verwendungszweck=sanitize_csv_field((b.get("verwendungszweck") or "").strip()) or None,
                kategorie=kategorie,
                typ=b.get("typ"), quelle=b.get("quelle") or "kontoauszug",
            ))
            neu += 1
        return neu


def add_kategorisierungsregel(user_id: int, empfaenger_contains: str, kategorie: str):
    """Legt eine Kategorisierungsregel an (Checkbox 'Immer so kategorisieren' im Haushaltsbuch-Tab)."""
    with get_session() as session:
        session.add(PosKategorisierungsregel(
            user_id=user_id, empfaenger_contains=empfaenger_contains.strip(), kategorie=kategorie,
        ))


def reset_onboarding(user_id: int):
    """
    Setzt das Onboarding eines Nutzers zurück ('🔄 Onboarding neu starten' im
    Übersicht-Tab). Positionen/Transaktionen bleiben unangetastet – nur
    Risikoprofil/Ziele/Zielgewichtungen/Anlagepräferenzen werden gelöscht bzw.
    zurückgesetzt, damit der Wizard (siehe onboarding.py) beim nächsten Laden
    wieder von vorn durchlaufen wird.
    """
    with get_session() as session:
        user = session.get(PosUser, user_id)
        if user is None:
            raise ValueError(f"Nutzer {user_id} nicht gefunden")
        user.onboarding_completed = False
        user.risikoprofil = None
        user.risikoscore = None
        session.query(PosGoal).filter_by(user_id=user_id).delete()
        session.query(PosTargetWeight).filter_by(user_id=user_id).delete()
        session.query(PosInvestmentPreference).filter_by(user_id=user_id).delete()


if __name__ == "__main__":
    init_db()
    print("✅ Datenbank initialisiert.")
