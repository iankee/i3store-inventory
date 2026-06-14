"""Database models for Sricreate Inventory System."""

from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
import enum
import json

from config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


# ── Enums ────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    VIEWER = "viewer"


# ── Permissions ──────────────────────────────────────────────────

# All available permissions with labels (for UI checkboxes)
ALL_PERMISSIONS = {
    "products.view":     "📋 Lihat Produk",
    "products.create":   "➕ Tambah Produk",
    "products.edit":     "✏️ Edit Produk",
    "products.delete":   "🗑️ Hapus Produk",
    "stock.in":          "📥 Barang Masuk",
    "stock.out":         "📤 Barang Keluar",
    "stock.adjust":      "⚖️ Adjustment Stok",
    "users.manage":      "👥 Kelola User",
    "telegram.bot":      "🤖 Akses Bot Telegram",
    "reports.view":      "📊 Lihat Laporan",
}

# Which permissions each legacy role gets
ROLE_PERMISSIONS = {
    UserRole.OWNER:  set(ALL_PERMISSIONS.keys()),
    UserRole.ADMIN:  {"products.view", "products.create", "products.edit",
                       "stock.in", "stock.out", "stock.adjust",
                       "telegram.bot", "reports.view"},
    UserRole.VIEWER: {"products.view", "reports.view"},
}


def parse_permissions(raw: str | None) -> set[str]:
    """Parse JSON permission string to set. Returns empty set on failure."""
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return set()


def has_permission(user: "User", permission: str) -> bool:
    """Check if user has a specific permission. OWNER always has everything."""
    if user.role == UserRole.OWNER:
        return True
    if not user.permissions:
        return permission in ROLE_PERMISSIONS.get(user.role, set())
    return permission in parse_permissions(user.permissions)


class MovementType(str, enum.Enum):
    STOCK_IN = "stock_in"
    STOCK_OUT = "stock_out"
    ADJUSTMENT = "adjustment"


class MovementSource(str, enum.Enum):
    TELEGRAM = "telegram"
    WEB = "web"
    SHOPEE = "shopee"
    TOKOPEDIA = "tokopedia"


# ── Models ───────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(128), nullable=False)
    role = Column(SAEnum(UserRole), default=UserRole.ADMIN, nullable=False)
    permissions = Column(String(2048), nullable=True)  # JSON array of permission keys
    telegram_username = Column(String(64), nullable=True)  # for bot auth
    is_active = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    movements = relationship("StockMovement", back_populates="user")

    def get_permissions(self) -> set[str]:
        """Get effective permissions for this user."""
        return parse_permissions(self.permissions) if self.permissions else ROLE_PERMISSIONS.get(self.role, set())

    def can(self, permission: str) -> bool:
        """Check if this user has a specific permission."""
        return has_permission(self, permission)


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(256), nullable=False, index=True)
    sku = Column(String(64), unique=True, nullable=True)  # optional SKU
    description = Column(String(1024), nullable=True)
    current_stock = Column(Integer, default=0, nullable=False)
    min_stock = Column(Integer, default=5, nullable=False)
    price_buy = Column(Float, nullable=True)
    price_sell = Column(Float, nullable=True)
    is_active = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    movements = relationship("StockMovement", back_populates="product", order_by="StockMovement.created_at.desc()")


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    type = Column(SAEnum(MovementType), nullable=False)
    source = Column(SAEnum(MovementSource), nullable=False, default=MovementSource.WEB)
    quantity = Column(Integer, nullable=False)  # positive = in, negative = out
    stock_before = Column(Integer, nullable=False)
    stock_after = Column(Integer, nullable=False)
    notes = Column(String(512), nullable=True)
    photo_path = Column(String(512), nullable=True)  # relative path in uploads/
    photo_hash = Column(String(64), nullable=True, index=True)  # SHA256 for dedup
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    product = relationship("Product", back_populates="movements")
    user = relationship("User", back_populates="movements")


# ── Init DB ──────────────────────────────────────────────────────

def init_db():
    """Create all tables."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """Yield a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
