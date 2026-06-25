from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
)
from sqlalchemy.orm import declarative_base, sessionmaker

import datetime

engine = create_engine(
    "sqlite:///wheel.db",
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# =========================
# КОРИСТУВАЧІ / ЗАЯВКИ
# =========================

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)

    username = Column(String, nullable=False)
    user_id = Column(String, unique=True, index=True, nullable=False)

    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)

    datetime = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
    )


# =========================
# СПІНИ
# =========================

class Spin(Base):
    __tablename__ = "spins"

    id = Column(Integer, primary_key=True, index=True)

    username = Column(String, nullable=False)
    user_id = Column(String, index=True, nullable=False)

    prize = Column(String, nullable=False)

    # коли був spin
    datetime = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
    )


# =========================
# STOCK ПРИЗІВ
# =========================

class PrizeStock(Base):
    __tablename__ = "prize_stock"

    id = Column(Integer, primary_key=True, index=True)

    sector_index = Column(Integer, unique=True, nullable=False)

    prize = Column(String, nullable=False)

    # None = безлімітно
    stock = Column(Integer, nullable=True)

    weight = Column(Integer, nullable=False)


# =========================
# НАЛАШТУВАННЯ СИСТЕМИ
# =========================

class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)

    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(String, nullable=False)

    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )


# =========================
# ІНІЦІАЛІЗАЦІЯ БАЗИ
# =========================

def init_db() -> None:
    Base.metadata.create_all(bind=engine)


# =========================
# СИНХРОНІЗАЦІЯ ПРИЗОВОГО ФОНДУ
# =========================

def ensure_prize_stock(db) -> None:
    """
    Синхронізує призовий фонд з config.py.

    Важливо:
    - якщо PRIZE_POOL_VERSION змінився — оновлюємо призи, stock і weight;
    - якщо PRIZE_POOL_VERSION такий самий — НЕ скидаємо залишки призів,
      щоб після рестарту Railway подарунки не повертались назад.
    """

    from config import PRIZES_, PRIZE_POOL_VERSION

    setting_key = "prize_pool_version"

    current_version = (
        db.query(AppSetting)
        .filter(AppSetting.key == setting_key)
        .first()
    )

    # Якщо версія вже актуальна — нічого не скидаємо
    if current_version and current_version.value == PRIZE_POOL_VERSION:
        return

    config_sector_indexes = {item["sector_index"] for item in PRIZES_}

    # Видаляємо старі сектори, яких більше немає в config.py
    old_prizes = db.query(PrizeStock).all()
    for old_prize in old_prizes:
        if old_prize.sector_index not in config_sector_indexes:
            db.delete(old_prize)

    # Оновлюємо або створюємо актуальні сектори
    for item in PRIZES_:
        prize_stock = (
            db.query(PrizeStock)
            .filter(PrizeStock.sector_index == item["sector_index"])
            .first()
        )

        if prize_stock is None:
            prize_stock = PrizeStock(
                sector_index=item["sector_index"],
                prize=item["prize"],
                stock=item["stock"],
                weight=item["weight"],
            )
            db.add(prize_stock)
        else:
            prize_stock.prize = item["prize"]
            prize_stock.stock = item["stock"]
            prize_stock.weight = item["weight"]

    # Записуємо нову версію призового фонду
    if current_version is None:
        current_version = AppSetting(
            key=setting_key,
            value=PRIZE_POOL_VERSION,
        )
        db.add(current_version)
    else:
        current_version.value = PRIZE_POOL_VERSION

    db.commit()