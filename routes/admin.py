import datetime
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func

from database import (
    SessionLocal,
    Spin,
    PrizeStock,
    ParticipantRegistration,
    ensure_prize_stock,
)
from config import (
    ADMINS,
    PRANK_USER_IDS,
    PRANK_TEXT,
    PRIZE_MODE,
    WIN_CHANCE_PERCENT,
    PRIZES_,
    PRIZE_POOL_VERSION,
    CAMPAIGN_START_AT_UTC,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

logger = logging.getLogger(__name__)


# =========================
# ТЕХНІЧНІ РЕЗУЛЬТАТИ
# =========================

TECHNICAL_PRIZES = {
    "Нічого",
    "Подарунки закінчились",
    "Помилка",
    PRANK_TEXT,
}


# =========================
# ДОПОМІЖНІ ФУНКЦІЇ
# =========================


def get_excluded_user_ids() -> list[str]:
    """
    Користувачі, яких не враховуємо у реальній статистиці розіграшу:
    - адміністратори;
    - prank users.
    """

    excluded = {str(user_id) for user_id in ADMINS}
    excluded.update(str(user_id) for user_id in PRANK_USER_IDS)
    return list(excluded)


def get_campaign_start_datetime() -> datetime.datetime | None:
    """
    Повертає дату старту кампанії з config.py.
    Якщо значення некоректне — не застосовуємо фільтр, але пишемо в log.
    """

    if not CAMPAIGN_START_AT_UTC:
        return None

    try:
        return datetime.datetime.fromisoformat(CAMPAIGN_START_AT_UTC)
    except Exception as exc:
        logger.error(
            "Invalid CAMPAIGN_START_AT_UTC=%r: %s",
            CAMPAIGN_START_AT_UTC,
            exc,
        )
        return None


def apply_real_spin_filters(query):
    """
    Застосовує до Spin фільтри реальної кампанії:
    - без адмінів;
    - без prank users;
    - після CAMPAIGN_START_AT_UTC, якщо дата задана.
    """

    excluded_ids = get_excluded_user_ids()
    if excluded_ids:
        query = query.filter(~Spin.user_id.in_(excluded_ids))

    campaign_start = get_campaign_start_datetime()
    if campaign_start is not None:
        query = query.filter(Spin.datetime >= campaign_start)

    return query


def is_real_prize_name(prize: str | None) -> bool:
    if not prize:
        return False

    return prize not in TECHNICAL_PRIZES


def calculate_stock_stats(prize_stocks: list[PrizeStock]) -> dict:
    """
    Формує статистику по актуальному PrizeStock.

    stock=None означає безлімітний залишок. У такому випадку total_remaining
    повертаємо як None, а stock_depleted завжди False.
    """

    has_unlimited_stock = any(item.stock is None for item in prize_stocks)

    finite_remaining = sum(
        max(0, int(item.stock or 0))
        for item in prize_stocks
        if item.stock is not None
    )

    if has_unlimited_stock:
        total_remaining: int | None = None
        stock_depleted = False
    else:
        total_remaining = finite_remaining
        stock_depleted = finite_remaining == 0

    initial_stock_by_sector = {
        int(item["sector_index"]): item.get("stock")
        for item in PRIZES_
    }

    initial_total = 0
    initial_has_unlimited = False

    for row in prize_stocks:
        initial_stock = initial_stock_by_sector.get(int(row.sector_index))

        if initial_stock is None:
            initial_has_unlimited = True
            continue

        initial_total += max(0, int(initial_stock))

    if initial_has_unlimited or total_remaining is None:
        total_awarded_from_stock: int | None = None
    else:
        total_awarded_from_stock = max(0, initial_total - total_remaining)

    return {
        "total_remaining": total_remaining,
        "total_awarded_from_stock": total_awarded_from_stock,
        "stock_depleted": stock_depleted,
        "has_unlimited_stock": has_unlimited_stock,
    }


# =========================
# ADMIN PAGE
# =========================


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user_id: int | None = None):
    # Зберігаємо поточну схему доступу, щоб не ламати існуюче посилання
    # на адмін-панель. Доступ мають лише Telegram ID із ADMINS.
    if user_id is None or user_id not in ADMINS:
        return HTMLResponse(
            "<h2 style='color:red'>ACCESS DENIED</h2>"
            "<p>У вас немає прав доступу.</p>",
            status_code=403,
        )

    db = SessionLocal()

    try:
        # Гарантуємо, що PrizeStock створений/синхронізований.
        # Якщо PRIZE_POOL_VERSION не змінився, фактичні залишки не скидаються.
        ensure_prize_stock(db)

        # -------------------------
        # SPINS
        # -------------------------

        spins = (
            db.query(Spin)
            .order_by(Spin.id.desc())
            .all()
        )

        total_spins = len(spins)

        real_spin_query = apply_real_spin_filters(db.query(Spin))
        total_real_spins = real_spin_query.count()

        real_win_query = apply_real_spin_filters(
            db.query(Spin).filter(
                Spin.prize.notin_(list(TECHNICAL_PRIZES))
            )
        )
        total_wins = real_win_query.count()

        # Залишаємо real_wins для сумісності зі старим admin.html,
        # але тепер це справді реальні виграші.
        real_wins = [
            spin
            for spin in spins
            if str(spin.user_id) not in get_excluded_user_ids()
            and is_real_prize_name(spin.prize)
            and (
                get_campaign_start_datetime() is None
                or spin.datetime >= get_campaign_start_datetime()
            )
        ]

        # -------------------------
        # PRIZE STOCK
        # -------------------------

        prize_stocks = (
            db.query(PrizeStock)
            .order_by(PrizeStock.sector_index.asc())
            .all()
        )

        stock_stats = calculate_stock_stats(prize_stocks)

        # -------------------------
        # REGISTRATIONS
        # -------------------------

        registrations = (
            db.query(ParticipantRegistration)
            .order_by(ParticipantRegistration.id.desc())
            .all()
        )

        total_registrations = (
            db.query(ParticipantRegistration)
            .filter(
                ParticipantRegistration.registration_completed_at.is_not(None)
            )
            .count()
        )

        pending_registrations = (
            db.query(ParticipantRegistration)
            .filter(
                ParticipantRegistration.registration_completed_at.is_(None)
            )
            .count()
        )

        wheel_registrations = (
            db.query(ParticipantRegistration)
            .filter(
                ParticipantRegistration.registration_completed_at.is_not(None),
                ParticipantRegistration.participation_type == "Колесо Фортуни",
            )
            .count()
        )

        no_spin_registrations = (
            db.query(ParticipantRegistration)
            .filter(
                ParticipantRegistration.registration_completed_at.is_not(None),
                ParticipantRegistration.participation_type == "Реєстрація без спіна",
            )
            .count()
        )

        confirmed_subscriptions = (
            db.query(ParticipantRegistration)
            .filter(
                ParticipantRegistration.registration_completed_at.is_not(None),
                ParticipantRegistration.subscription_confirmed.is_(True),
            )
            .count()
        )

        unique_participants = (
            db.query(
                func.count(
                    func.distinct(ParticipantRegistration.user_id)
                )
            )
            .filter(
                ParticipantRegistration.registration_completed_at.is_not(None)
            )
            .scalar()
            or 0
        )

        registrations_with_prize = (
            db.query(ParticipantRegistration)
            .filter(
                ParticipantRegistration.registration_completed_at.is_not(None),
                ParticipantRegistration.prize.is_not(None),
                ParticipantRegistration.prize != "",
            )
            .count()
        )

        # -------------------------
        # TEMPLATE
        # -------------------------

        return templates.TemplateResponse(
            "admin.html",
            {
                "request": request,

                # Старі змінні — залишаємо для backward compatibility.
                "spins": spins,
                "prize_stocks": prize_stocks,
                "total_spins": total_spins,
                "total_wins": total_wins,
                "real_wins": real_wins,
                "user_id": user_id,
                "prize_mode": PRIZE_MODE,
                "win_chance_percent": WIN_CHANCE_PERCENT,

                # Нова статистика spin.
                "total_real_spins": total_real_spins,

                # Нова статистика stock.
                "total_remaining": stock_stats["total_remaining"],
                "total_awarded_from_stock": stock_stats[
                    "total_awarded_from_stock"
                ],
                "stock_depleted": stock_stats["stock_depleted"],
                "has_unlimited_stock": stock_stats["has_unlimited_stock"],
                "prize_pool_version": PRIZE_POOL_VERSION,

                # Нова статистика реєстрацій.
                "registrations": registrations,
                "total_registrations": total_registrations,
                "pending_registrations": pending_registrations,
                "wheel_registrations": wheel_registrations,
                "no_spin_registrations": no_spin_registrations,
                "confirmed_subscriptions": confirmed_subscriptions,
                "unique_participants": int(unique_participants),
                "registrations_with_prize": registrations_with_prize,
            },
        )

    except Exception:
        logger.exception("Failed to render admin page")
        raise

    finally:
        db.close()