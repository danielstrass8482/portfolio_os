"""
database.py – SQLAlchemy-Modelle und Datenbankzugriff für Portfolio-OS.
Nutzt dieselbe Postgres-Instanz wie der Trading Bot. Alle Tabellen tragen
das Präfix "pos_", damit es keine Konflikte mit den Trading-Bot-Tabellen gibt.
"""

from datetime import datetime, date
from contextlib import contextmanager
import json

from sqlalchemy import (
    create_engine, Column, Integer, Float, String, Boolean,
    DateTime, Date, Text, ForeignKey, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

from config import DATABASE_URL, DEFAULT_ASSET_CLASSES, FREISTELLUNGSAUFTRAG_DEFAULT

Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False)
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
    ticker         = Column(String(20), nullable=False)
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


class PosTaxConfig(Base):
    """Steuerliche Einstellungen je Nutzer."""
    __tablename__ = "pos_tax_config"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    user_id               = Column(Integer, ForeignKey("pos_users.id"), nullable=False, unique=True)
    kirchensteuer         = Column(Boolean, default=False)
    freistellungsauftrag  = Column(Float, default=FREISTELLUNGSAUFTRAG_DEFAULT)
    freistellungsgenutzt  = Column(Float, default=0.0)
    verlusttopf_vorjahr   = Column(Float, default=0.0)

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


def init_db():
    """Erstellt alle pos_*-Tabellen (idempotent – safe to call multiple times)."""
    Base.metadata.create_all(engine)
    with get_session() as session:
        _seed_asset_classes(session)


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
            setattr(obj, key, value)
        session.flush()
        return obj.id


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


if __name__ == "__main__":
    init_db()
    print("✅ Datenbank initialisiert.")
