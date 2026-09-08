import datetime
import logging
import os

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker


# =========================
# ШЛЯХ ДО БАЗИ
# =========================
# Локально буде wheel.db
# На Railway бажано поставити змінну:
# DB_PATH=/app/data/wheel.db
# і підключити Railway Volume до /app/data

DB_PATH = os.getenv("DB_PATH", "wheel.db")

DB_DIR = os.path.dirname(DB_PATH)
if DB_DIR:
    os.makedirs(DB_DIR, exist_ok=True)


engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)

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

    # Telegram file_id останнього надісланого фото чека.
    # Сам файл на Railway не зберігаємо — Telegram дозволяє повторно
    # використати file_id для відправки фото адміну.
    receipt_file_id = Column(String, nullable=True)

    datetime = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
    )


# =========================
# ІСТОРІЯ РЕЄСТРАЦІЙ
# =========================
# Lead — це поточний профіль користувача.
# ParticipantRegistration — append-only історія окремих участей.
# Це дозволяє одному Telegram-користувачу знову зареєструватися
# після cooldown і не перезаписувати стару участь.

class ParticipantRegistration(Base):
    __tablename__ = "participant_registrations"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(String, index=True, nullable=False)
    username = Column(String, nullable=False)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)

    # Telegram file_id фото чека саме для цієї реєстрації.
    receipt_file_id = Column(String, nullable=False)

    # False до успішної перевірки підписки на канал.
    subscription_confirmed = Column(
        Boolean,
        default=False,
        nullable=False,
    )

    # "Колесо Фортуни" або "Реєстрація без спіна".
    participation_type = Column(String, nullable=True)

    # Назва реально отриманого подарунка.
    # Заповнюватиметься після успішного spin.
    prize = Column(String, nullable=True)

    # Коли користувач почав/створив цю реєстрацію.
    datetime = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        index=True,
    )

    # None = реєстрація ще не завершена.
    # Після фото чека + підтвердження підписки запис отримує дату.
    registration_completed_at = Column(
        DateTime,
        nullable=True,
        index=True,
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
# БЕЗПЕЧНІ SQLITE-МІГРАЦІЇ
# =========================
# SQLAlchemy create_all() створює відсутні таблиці, але НЕ додає нові
# колонки у вже існуючі таблиці. Тому для старого wheel.db робимо
# маленькі backward-compatible міграції вручну.


def _table_exists(connection, table_name: str) -> bool:
    result = connection.execute(
        text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name=:table_name LIMIT 1"
        ),
        {"table_name": table_name},
    ).first()

    return result is not None


def _get_table_columns(connection, table_name: str) -> set[str]:
    if not _table_exists(connection, table_name):
        return set()

    # Назви таблиць тут тільки внутрішні, контрольовані кодом.
    rows = connection.execute(
        text(f'PRAGMA table_info("{table_name}")')
    ).mappings().all()

    return {str(row["name"]) for row in rows}


def _add_column_if_missing(
    connection,
    *,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    columns = _get_table_columns(connection, table_name)

    if column_name in columns:
        return

    logging.info(
        "Database migration: adding %s.%s",
        table_name,
        column_name,
    )

    connection.execute(
        text(
            f'ALTER TABLE "{table_name}" '
            f'ADD COLUMN "{column_name}" {column_sql}'
        )
    )


def _run_sqlite_migrations() -> None:
    """
    Міграції для вже існуючої бази.

    Функцію можна безпечно запускати при кожному старті — вона перевіряє,
    чи існує колонка/індекс, і не дублює їх.
    """

    with engine.begin() as connection:
        # Старий Lead не мав фото чека.
        _add_column_if_missing(
            connection,
            table_name="leads",
            column_name="receipt_file_id",
            column_sql="VARCHAR",
        )

        # Нижче — додатковий захист на випадок, якщо десь уже була створена
        # рання/тестова версія participant_registrations з неповною схемою.
        if _table_exists(connection, "participant_registrations"):
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="user_id",
                column_sql="VARCHAR",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="username",
                column_sql="VARCHAR",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="name",
                column_sql="VARCHAR",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="phone",
                column_sql="VARCHAR",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="receipt_file_id",
                column_sql="VARCHAR",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="subscription_confirmed",
                column_sql="BOOLEAN NOT NULL DEFAULT 0",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="participation_type",
                column_sql="VARCHAR",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="prize",
                column_sql="VARCHAR",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="datetime",
                column_sql="DATETIME",
            )
            _add_column_if_missing(
                connection,
                table_name="participant_registrations",
                column_name="registration_completed_at",
                column_sql="DATETIME",
            )

        # Індекси. CREATE INDEX IF NOT EXISTS безпечний при повторному запуску.
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_participant_registrations_user_id "
                "ON participant_registrations (user_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_participant_registrations_datetime "
                "ON participant_registrations (datetime)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_participant_registrations_registration_completed_at "
                "ON participant_registrations (registration_completed_at)"
            )
        )


# =========================
# ІНІЦІАЛІЗАЦІЯ БАЗИ
# =========================


def init_db() -> None:
    # Спочатку створюємо всі відсутні таблиці.
    Base.metadata.create_all(bind=engine)

    # Потім доповнюємо старі таблиці новими колонками.
    _run_sqlite_migrations()


# =========================
# СИНХРОНІЗАЦІЯ ПРИЗОВОГО ФОНДУ
# =========================


def ensure_prize_stock(db) -> None:
    """
    Синхронізує призовий фонд з config.py.

    Важливо:
    - якщо PRIZE_POOL_VERSION змінився — оновлюємо призи, stock і weight;
    - якщо PRIZE_POOL_VERSION такий самий — НЕ скидаємо залишки призів,
      щоб після рестарту Railway подарунки не повертались назад;
    - при переході на нову версію фонду очищаємо старі прапорці
      "подарунки закінчилися", щоб новий фонд міг окремо повідомити адмінів,
      коли він реально буде вичерпаний.
    """

    from config import PRIZES_, PRIZE_POOL_VERSION

    setting_key = "prize_pool_version"

    current_version = (
        db.query(AppSetting)
        .filter(AppSetting.key == setting_key)
        .first()
    )

    # Якщо версія вже актуальна — нічого не скидаємо.
    if current_version and current_version.value == PRIZE_POOL_VERSION:
        return

    config_sector_indexes = {
        int(item["sector_index"])
        for item in PRIZES_
    }

    # Видаляємо старі сектори, яких більше немає в config.py.
    old_prizes = db.query(PrizeStock).all()

    for old_prize in old_prizes:
        if int(old_prize.sector_index) not in config_sector_indexes:
            db.delete(old_prize)

    # Оновлюємо або створюємо актуальні сектори.
    for item in PRIZES_:
        sector_index = int(item["sector_index"])

        prize_stock = (
            db.query(PrizeStock)
            .filter(PrizeStock.sector_index == sector_index)
            .first()
        )

        if prize_stock is None:
            prize_stock = PrizeStock(
                sector_index=sector_index,
                prize=str(item["prize"]),
                stock=item.get("stock"),
                weight=int(item["weight"]),
            )
            db.add(prize_stock)
        else:
            prize_stock.prize = str(item["prize"])
            prize_stock.stock = item.get("stock")
            prize_stock.weight = int(item["weight"])

    # Записуємо нову версію призового фонду.
    if current_version is None:
        current_version = AppSetting(
            key=setting_key,
            value=str(PRIZE_POOL_VERSION),
        )
        db.add(current_version)
    else:
        current_version.value = str(PRIZE_POOL_VERSION)

    # Старі прапорці завершення фонду більше не актуальні.
    # bot.py використовує ключ виду:
    # prizes_depleted_notified::<PRIZE_POOL_VERSION>
    db.query(AppSetting).filter(
        AppSetting.key.like("prizes_depleted_notified%")
    ).delete(synchronize_session=False)

    db.commit()