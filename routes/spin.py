import asyncio
import random
import datetime
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_

from database import SessionLocal, Spin, Lead, PrizeStock, ensure_prize_stock
from config import (
    ADMINS,
    CHANNEL_USERNAME,
    CHANNEL_URL,
    SPIN_COOLDOWN_DAYS,
    PRANK_USER_IDS,
    PRANK_TEXT,
    PRANK_SECTOR_INDEX,
)
from bot import get_bot_and_dispatcher

router = APIRouter()

# Захист від подвійного натискання / одночасних запитів
SPIN_LOCKS: dict[str, asyncio.Lock] = {}


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


async def check_channel_subscription(user_id_str: str, is_admin: bool) -> bool:
    """
    Перевіряє підписку на канал прямо перед прокруткою.
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

    caption = "\n".join(
        [
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
            "",
            "⏳ Наступна прокрутка: без обмежень для адміна"
            if is_admin
            else f"⏳ Наступна прокрутка через: {SPIN_COOLDOWN_DAYS} днів",
            "",
            f"Внутрішній ID: {user_id_str}_{lead.id}",
        ]
    )

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

            available_prizes = (
                db.query(PrizeStock)
                .filter(PrizeStock.weight > 0)
                .filter(
                    or_(
                        PrizeStock.stock.is_(None),
                        PrizeStock.stock > 0,
                    )
                )
                .all()
            )

            if not available_prizes:
                return JSONResponse(
                    {
                        "prize": "Нічого",
                        "sector_index": 3,
                        "repeat": False,
                        "message": "Призи закінчились.",
                    }
                )

            selected = random.choices(
                available_prizes,
                weights=[p.weight for p in available_prizes],
                k=1,
            )[0]

            prize = selected.prize
            sector_index = selected.sector_index

            if selected.stock is not None and not is_admin:
                selected.stock -= 1

            row = Spin(
                username=str(username),
                user_id=user_id_str,
                prize=prize,
            )

            db.add(row)
            db.commit()
            db.refresh(row)

            await notify_admins(
                lead=lead,
                prize=prize,
                sector_index=sector_index,
                user_id_str=user_id_str,
                is_admin=is_admin,
                is_prank=False,
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