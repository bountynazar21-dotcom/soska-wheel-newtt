import asyncio
import random
import datetime
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from database import SessionLocal, Spin, Lead, PrizeStock, ensure_prize_stock
from config import (
    ADMINS,
    CHANNEL_USERNAME,
    CHANNEL_URL,
    SPIN_COOLDOWN_DAYS,
    PRANK_USER_IDS,
    PRANK_TEXT,
    PRANK_SECTOR_INDEX,
    PRIZE_UNLOCK_SPINS,
    CAMPAIGN_START_AT_UTC,
)
from bot import get_bot_and_dispatcher

router = APIRouter()

# Захист від подвійного натискання одного користувача
SPIN_LOCKS: dict[str, asyncio.Lock] = {}

# Глобальний захист роздачі подарунків.
# Потрібен, щоб два різні користувачі одночасно не забрали один і той самий слот подарунка.
PRIZE_DISTRIBUTION_LOCK = asyncio.Lock()


def get_spin_lock(user_id_str: str) -> asyncio.Lock:
    if user_id_str not in SPIN_LOCKS:
        SPIN_LOCKS[user_id_str] = asyncio.Lock()
    return SPIN_LOCKS[user_id_str]


def format_time_left(delta: datetime.timedelta) -> str:
    total_seconds = int(delta.total_seconds())

    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    if days > 0:
        return f"{days} дн. {hours} год."
    if hours > 0:
        return f"{hours} год. {minutes} хв."
    return f"{minutes} хв."


def is_real_win(prize: str, sector_index: int, is_prank: bool) -> bool:
    if is_prank:
        return False

    if sector_index == 3:
        return False

    if prize == "Нічого":
        return False

    return True


def get_nothing_result() -> tuple[str, int]:
    return "Нічого", 3


def get_excluded_user_ids() -> list[str]:
    """
    Ці користувачі не рахуються як реальні учасники:
    - адміни;
    - prank users.
    """

    excluded_ids = set()

    for admin_id in ADMINS:
        excluded_ids.add(str(admin_id))

    for prank_id in PRANK_USER_IDS:
        excluded_ids.add(str(prank_id))

    return list(excluded_ids)


def get_campaign_start_datetime() -> datetime.datetime | None:
    """
    Якщо CAMPAIGN_START_AT_UTC заданий у config.py,
    рахуємо тільки прокрутки після цієї дати.
    """

    if not CAMPAIGN_START_AT_UTC:
        return None

    try:
        return datetime.datetime.fromisoformat(CAMPAIGN_START_AT_UTC)
    except Exception as e:
        logging.error(f"Invalid CAMPAIGN_START_AT_UTC: {e}")
        return None


def apply_campaign_filter(query):
    campaign_start = get_campaign_start_datetime()

    if campaign_start is None:
        return query

    return query.filter(Spin.datetime >= campaign_start)


def get_real_spin_count(db) -> int:
    """
    Рахує реальні прокрутки:
    - без адмінів;
    - без prank users;
    - за потреби тільки після CAMPAIGN_START_AT_UTC.
    """

    query = db.query(Spin)

    excluded_ids = get_excluded_user_ids()
    if excluded_ids:
        query = query.filter(~Spin.user_id.in_(excluded_ids))

    query = apply_campaign_filter(query)

    return query.count()


def get_awarded_real_prize_count(db) -> int:
    """
    Рахує, скільки реальних подарунків вже роздали.
    "Нічого", prank і адмінські тести не рахуються.
    """

    query = (
        db.query(Spin)
        .filter(Spin.prize != "Нічого")
        .filter(Spin.prize != PRANK_TEXT)
    )

    excluded_ids = get_excluded_user_ids()
    if excluded_ids:
        query = query.filter(~Spin.user_id.in_(excluded_ids))

    query = apply_campaign_filter(query)

    return query.count()


def get_unlocked_prize_slots(real_spin_number: int) -> int:
    """
    Скільки подарункових слотів вже відкрито
    на поточному номері реальної прокрутки.
    """

    return sum(
        1
        for unlock_spin in PRIZE_UNLOCK_SPINS
        if real_spin_number >= unlock_spin
    )


def get_available_gift_prizes(db) -> list[PrizeStock]:
    """
    Доступні саме подарунки.
    Сектор "Нічого" сюди не входить.
    """

    return (
        db.query(PrizeStock)
        .filter(PrizeStock.sector_index != 3)
        .filter(PrizeStock.prize != "Нічого")
        .filter(PrizeStock.weight > 0)
        .filter(PrizeStock.stock > 0)
        .all()
    )


def choose_controlled_prize(
    db,
    real_spin_number: int,
) -> tuple[str, int, PrizeStock | None]:
    """
    Контрольована логіка:

    1. Якщо ще не настав поріг подарунка — падає "Нічого".
    2. Якщо подарунковий слот відкрився — видаємо подарунок.
    3. Якщо всі подарунки закінчились — падає "Нічого".
    """

    unlocked_slots = get_unlocked_prize_slots(real_spin_number)
    awarded_prizes = get_awarded_real_prize_count(db)

    # На цьому етапі ще не можна видати новий подарунок
    if awarded_prizes >= unlocked_slots:
        prize, sector_index = get_nothing_result()
        return prize, sector_index, None

    available_gifts = get_available_gift_prizes(db)

    # Усі подарунки закінчились
    if not available_gifts:
        prize, sector_index = get_nothing_result()
        return prize, sector_index, None

    selected = random.choices(
        available_gifts,
        weights=[p.weight for p in available_gifts],
        k=1,
    )[0]

    return selected.prize, selected.sector_index, selected


async def check_channel_subscription(user_id_str: str, is_admin: bool) -> bool:
    """
    Перевіряє підписку на канал перед прокруткою.
    Адмінів пропускаємо без перевірки.
    """

    if is_admin:
        return True

    if not user_id_str.isdigit():
        return False

    try:
        bot, _ = get_bot_and_dispatcher()

        member = await bot.get_chat_member(
            chat_id=CHANNEL_USERNAME,
            user_id=int(user_id_str),
        )

        return member.status in {
            "creator",
            "administrator",
            "member",
            "restricted",
        }

    except Exception as e:
        logging.error(
            f"Failed to check channel subscription for user {user_id_str}: {e}"
        )
        return False


async def notify_admins(
    lead: Lead,
    prize: str,
    sector_index: int,
    user_id_str: str,
    is_admin: bool,
    is_prank: bool,
    real_spin_number: int | None = None,
    unlocked_slots: int | None = None,
    awarded_prizes: int | None = None,
):
    bot, _ = get_bot_and_dispatcher()

    win = is_real_win(
        prize=prize,
        sector_index=sector_index,
        is_prank=is_prank,
    )

    if win:
        title = "🚨🚨🚨 ПЕРЕМОЖЕЦЬ! ВИГРАВ ПОДАРУНОК 🚨🚨🚨"
        result_block = "\n".join(
            [
                "━━━━━━━━━━━━━━━━━━━━",
                f"🎁 ПРИЗ: {prize}",
                "━━━━━━━━━━━━━━━━━━━━",
            ]
        )
    elif is_prank:
        title = "🤣 PRANK USER"
        result_block = f"🎭 Результат: {prize}"
    else:
        title = "🌀 Нова прокрутка"
        result_block = f"Результат: {prize}"

    notes = []
    if is_admin:
        notes.append("🧪 ТЕСТ АДМІНА")
    if is_prank:
        notes.append("🤣 PRANK USER")

    admin_note = f" {' | '.join(notes)}" if notes else ""

    telegram_line = (
        f"Telegram: @{lead.username}"
        if lead.username and not lead.username.isdigit()
        else f"User ID: {lead.user_id}"
    )

    caption_parts = [
        f"{title}{admin_note}",
        "",
        f"Заявка №{lead.id}",
        "",
        f"Імʼя: {lead.name}",
        f"Телефон: {lead.phone}",
        telegram_line,
        f"Telegram ID: {user_id_str}",
        "",
        result_block,
    ]

    if real_spin_number is not None:
        caption_parts.extend(
            [
                "",
                "📊 Статистика розіграшу:",
                f"Реальна прокрутка №: {real_spin_number}",
                f"Відкрито подарункових слотів: {unlocked_slots}",
                f"Роздано подарунків: {awarded_prizes}",
            ]
        )

    caption_parts.extend(
        [
            "",
            "⏳ Наступна прокрутка: без обмежень для адміна"
            if is_admin
            else f"⏳ Наступна прокрутка через: {SPIN_COOLDOWN_DAYS} днів",
            "",
            f"Внутрішній ID: {user_id_str}_{lead.id}",
        ]
    )

    caption = "\n".join(caption_parts)

    for admin_id in ADMINS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=caption,
            )
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")


async def notify_user_win(
    user_id_str: str,
    prize: str,
    sector_index: int,
    is_prank: bool,
):
    if not is_real_win(
        prize=prize,
        sector_index=sector_index,
        is_prank=is_prank,
    ):
        return

    try:
        bot, _ = get_bot_and_dispatcher()

        await bot.send_message(
            chat_id=int(user_id_str),
            text=(
                "🎉 Вітаємо!\n\n"
                f"Твій виграш: {prize}\n\n"
                "Наш адміністратор перевірить заявку та звʼяжеться з тобою."
            ),
        )

    except Exception as e:
        logging.error(f"Failed to send win message to user {user_id_str}: {e}")


@router.post("/spin")
async def spin(request: Request):
    data = await request.json()

    username = data.get("username") or "unknown"
    user_id = data.get("user_id")

    user_id_str = str(user_id) if user_id is not None else "unknown"

    lock = get_spin_lock(user_id_str)

    async with lock:
        db = SessionLocal()

        try:
            ensure_prize_stock(db)

            is_admin = (
                int(user_id_str) in ADMINS
                if user_id_str.isdigit()
                else False
            )

            is_prank_user = (
                int(user_id_str) in PRANK_USER_IDS
                if user_id_str.isdigit()
                else False
            )

            now = datetime.datetime.utcnow()

            lead = (
                db.query(Lead)
                .filter(Lead.user_id == user_id_str)
                .first()
            )

            if not lead:
                return JSONResponse(
                    {
                        "prize": "Нічого",
                        "sector_index": 3,
                        "repeat": True,
                        "message": "Спочатку пройди реєстрацію в боті.",
                    }
                )

            is_subscribed = await check_channel_subscription(
                user_id_str=user_id_str,
                is_admin=is_admin,
            )

            if not is_subscribed:
                return JSONResponse(
                    {
                        "prize": "Нічого",
                        "sector_index": 3,
                        "repeat": True,
                        "message": (
                            "Щоб крутити колесо, потрібно бути підписаним "
                            f"на наш канал: {CHANNEL_URL}"
                        ),
                    }
                )

            last_spin = (
                db.query(Spin)
                .filter(Spin.user_id == user_id_str)
                .order_by(Spin.datetime.desc())
                .first()
            )

            if last_spin and not is_admin:
                cooldown_until = last_spin.datetime + datetime.timedelta(
                    days=SPIN_COOLDOWN_DAYS
                )

                if now < cooldown_until:
                    time_left = cooldown_until - now

                    return JSONResponse(
                        {
                            "prize": last_spin.prize,
                            "sector_index": PRANK_SECTOR_INDEX
                            if is_prank_user
                            else 3,
                            "repeat": True,
                            "message": (
                                "Ви вже крутили колесо. "
                                f"Наступна спроба через {format_time_left(time_left)}."
                            ),
                        }
                    )

            if is_prank_user and not is_admin:
                row = Spin(
                    username=str(username),
                    user_id=user_id_str,
                    prize=PRANK_TEXT,
                )

                db.add(row)
                db.commit()
                db.refresh(row)

                await notify_admins(
                    lead=lead,
                    prize=PRANK_TEXT,
                    sector_index=PRANK_SECTOR_INDEX,
                    user_id_str=user_id_str,
                    is_admin=False,
                    is_prank=True,
                )

                return JSONResponse(
                    {
                        "prize": PRANK_TEXT,
                        "sector_index": PRANK_SECTOR_INDEX,
                        "repeat": False,
                        "message": "",
                    }
                )

            # Вибір і запис результату захищені глобальним lock,
            # щоб подарунки не списались неправильно при одночасних прокрутках.
            async with PRIZE_DISTRIBUTION_LOCK:
                real_spin_count_before = get_real_spin_count(db)

                # Адмін не рухає прогрес розіграшу.
                # Реальний користувач = наступний номер прокрутки.
                real_spin_number = real_spin_count_before + (
                    0 if is_admin else 1
                )

                prize, sector_index, selected_prize_stock = choose_controlled_prize(
                    db=db,
                    real_spin_number=real_spin_number,
                )

                if selected_prize_stock is not None and not is_admin:
                    selected_prize_stock.stock -= 1

                row = Spin(
                    username=str(username),
                    user_id=user_id_str,
                    prize=prize,
                )

                db.add(row)
                db.commit()
                db.refresh(row)

                unlocked_slots = get_unlocked_prize_slots(real_spin_number)
                awarded_prizes = get_awarded_real_prize_count(db)

            await notify_admins(
                lead=lead,
                prize=prize,
                sector_index=sector_index,
                user_id_str=user_id_str,
                is_admin=is_admin,
                is_prank=False,
                real_spin_number=real_spin_number,
                unlocked_slots=unlocked_slots,
                awarded_prizes=awarded_prizes,
            )

            await notify_user_win(
                user_id_str=user_id_str,
                prize=prize,
                sector_index=sector_index,
                is_prank=False,
            )

            return JSONResponse(
                {
                    "prize": prize,
                    "sector_index": sector_index,
                    "repeat": False,
                    "message": "",
                }
            )

        finally:
            db.close()