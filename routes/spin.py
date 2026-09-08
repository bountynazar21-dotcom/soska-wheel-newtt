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
    PRANimport asyncio
import datetime
import logging
import random

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_

from database import (
    SessionLocal,
    Spin,
    Lead,
    PrizeStock,
    ParticipantRegistration,
    ensure_prize_stock,
)
from config import (
    ADMINS,
    CHANNEL_USERNAME,
    CHANNEL_URL,
    SPIN_COOLDOWN_DAYS,
    PRANK_USER_IDS,
    PRANK_TEXT,
    PRANK_SECTOR_INDEX,
    PRIZE_UNLOCK_SPINS,
    PRIZE_MODE,
    WIN_CHANCE_PERCENT,
    CAMPAIGN_START_AT_UTC,
)
from bot import get_bot_and_dispatcher, notify_prizes_depleted_once
from participants_excel import update_registration_prize_in_excel

router = APIRouter()

# Сектора «Нічого» більше немає.
# Fallback-сектор — 0, Косметичка OXVA.
# Важливо: коли реальний призовий фонд = 0, /spin НЕ створює Spin
# і НЕ запускає колесо. Fallback потрібен тільки для технічної відповіді API.
FALLBACK_SECTOR_INDEX = 0

# Захист від подвійного натискання одного користувача.
SPIN_LOCKS: dict[str, asyncio.Lock] = {}

# Глобальний захист роздачі подарунків.
# Потрібен, щоб два різні користувачі одночасно не забрали
# останній або один і той самий подарунок.
PRIZE_DISTRIBUTION_LOCK = asyncio.Lock()


# =========================
# БАЗОВІ HELPERS
# =========================


def get_spin_lock(user_id_str: str) -> asyncio.Lock:
    if user_id_str not in SPIN_LOCKS:
        SPIN_LOCKS[user_id_str] = asyncio.Lock()
    return SPIN_LOCKS[user_id_str]


def format_time_left(delta: datetime.timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))

    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    if days > 0:
        return f"{days} дн. {hours} год."
    if hours > 0:
        return f"{hours} год. {minutes} хв."
    return f"{minutes} хв."


def is_real_win(prize: str, sector_index: int, is_prank: bool) -> bool:
    """
    Перевіряє, чи результат є реальним подарунком.

    sector_index навмисно залишений у сигнатурі, бо його використовує
    поточна логіка викликів і він може знадобитися при зміні секторів.
    """

    if is_prank:
        return False

    if not prize:
        return False

    if prize in {
        "Нічого",
        "Помилка",
        "Подарунки закінчились",
        PRANK_TEXT,
    }:
        return False

    return True


def get_no_prize_result() -> tuple[str, int]:
    """
    Технічний результат для chance/controlled логіки, якщо саме ця
    прокрутка не повинна видати подарунок.

    ВАЖЛИВО: коли весь реальний stock = 0, ця функція не використовується
    для створення Spin — endpoint завершується раніше.
    """

    return "Подарунки закінчились", FALLBACK_SECTOR_INDEX


def get_error_result() -> tuple[str, int]:
    return "Помилка", FALLBACK_SECTOR_INDEX


def get_fund_depleted_response() -> JSONResponse:
    """
    Відповідь для старого/прямого WebApp, коли фонд уже закінчився.

    repeat=True означає: фронтенд не повинен вважати це новою прокруткою.
    У БД при цьому Spin НЕ створюється.
    """

    return JSONResponse(
        {
            "prize": "Подарунки закінчились",
            "sector_index": FALLBACK_SECTOR_INDEX,
            "repeat": True,
            "message": (
                "Подарунки в Колесі Фортуни вже закінчилися. "
                "Нові учасники можуть пройти реєстрацію в Telegram-боті."
            ),
        }
    )


# =========================
# ПРИЗОВИЙ ФОНД
# =========================


def _real_gift_query(db):
    """
    Єдине визначення реальних призів для spin.py.

    Не враховує технічні результати та prank-текст.
    """

    return (
        db.query(PrizeStock)
        .filter(PrizeStock.prize != "Нічого")
        .filter(PrizeStock.prize != "Подарунки закінчились")
        .filter(PrizeStock.prize != "Помилка")
        .filter(PrizeStock.prize != PRANK_TEXT)
        .filter(PrizeStock.weight > 0)
    )


def get_total_real_gift_stock(db) -> int | None:
    """
    Повертає актуальний сумарний залишок реальних подарунків.

    int  -> точний залишок.
    None -> у фонді є хоча б один безлімітний подарунок (stock=None),
            тому фонд не вважається вичерпаним.
    """

    rows = _real_gift_query(db).all()

    if any(row.stock is None for row in rows):
        return None

    return sum(max(0, int(row.stock or 0)) for row in rows)


def get_available_gift_prizes(db) -> list[PrizeStock]:
    """
    Доступні реальні подарунки: stock > 0 або stock=None.
    """

    return (
        _real_gift_query(db)
        .filter(
            or_(
                PrizeStock.stock.is_(None),
                PrizeStock.stock > 0,
            )
        )
        .all()
    )


def choose_available_gift_prize(db) -> tuple[str, int, PrizeStock | None]:
    """
    Обирає випадковий подарунок із тих, які реально доступні.
    """

    available_gifts = get_available_gift_prizes(db)

    if not available_gifts:
        prize, sector_index = get_no_prize_result()
        return prize, sector_index, None

    selected = random.choices(
        available_gifts,
        weights=[max(0, int(p.weight)) for p in available_gifts],
        k=1,
    )[0]

    return selected.prize, selected.sector_index, selected


# =========================
# СТАТИСТИКА РОЗІГРАШУ
# =========================


def get_excluded_user_ids() -> list[str]:
    """
    Ці користувачі не рахуються як реальні учасники:
    - адміни;
    - prank users.
    """

    excluded_ids = {str(admin_id) for admin_id in ADMINS}
    excluded_ids.update(str(prank_id) for prank_id in PRANK_USER_IDS)
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
    Рахує, скільки реальних подарунків уже видано.
    Технічні результати, prank і адмінські тести не рахуються.
    """

    query = (
        db.query(Spin)
        .filter(Spin.prize != "Нічого")
        .filter(Spin.prize != "Подарунки закінчились")
        .filter(Spin.prize != "Помилка")
        .filter(Spin.prize != PRANK_TEXT)
    )

    excluded_ids = get_excluded_user_ids()
    if excluded_ids:
        query = query.filter(~Spin.user_id.in_(excluded_ids))

    query = apply_campaign_filter(query)
    return query.count()


def get_unlocked_prize_slots(real_spin_number: int) -> int:
    """
    Скільки подарункових слотів уже відкрито на поточному номері
    реальної прокрутки. Використовується тільки для controlled mode.
    """

    return sum(
        1
        for unlock_spin in PRIZE_UNLOCK_SPINS
        if real_spin_number >= unlock_spin
    )


# =========================
# ВИБІР ПРИЗУ
# =========================


def choose_chance_prize(db) -> tuple[str, int, PrizeStock | None]:
    """
    PRIZE_MODE = chance.

    При WIN_CHANCE_PERCENT = 100.0 кожна реальна прокрутка отримує
    один із доступних подарунків, поки stock > 0.
    """

    chance = max(0.0, min(100.0, float(WIN_CHANCE_PERCENT)))
    roll = random.uniform(0, 100)

    if roll > chance:
        prize, sector_index = get_no_prize_result()
        return prize, sector_index, None

    return choose_available_gift_prize(db)


def choose_controlled_prize(
    db,
    real_spin_number: int,
) -> tuple[str, int, PrizeStock | None]:
    """
    PRIZE_MODE = controlled.

    Новий подарунок можна видати лише тоді, коли кількість уже розданих
    подарунків менша за кількість відкритих слотів.
    """

    unlocked_slots = get_unlocked_prize_slots(real_spin_number)
    awarded_prizes = get_awarded_real_prize_count(db)

    if awarded_prizes >= unlocked_slots:
        prize, sector_index = get_no_prize_result()
        return prize, sector_index, None

    return choose_available_gift_prize(db)


def choose_prize(
    db,
    real_spin_number: int,
) -> tuple[str, int, PrizeStock | None]:
    if PRIZE_MODE == "controlled":
        return choose_controlled_prize(
            db=db,
            real_spin_number=real_spin_number,
        )

    return choose_chance_prize(db)


# =========================
# РЕЄСТРАЦІЯ / ДОПУСК ДО SPIN
# =========================


def get_latest_eligible_registration(
    db,
    *,
    user_id_str: str,
    last_spin: Spin | None,
) -> ParticipantRegistration | None:
    """
    Повертає актуальну завершену реєстрацію, яка дає право на spin.

    Критично важливо:
    якщо у користувача вже був попередній spin, нова реєстрація повинна
    бути завершена ПІСЛЯ нього. Тому старе WebApp-посилання не можна
    повторно використати через 7 днів без нового чека та реєстрації.
    """

    query = db.query(ParticipantRegistration).filter(
        ParticipantRegistration.user_id == user_id_str,
        ParticipantRegistration.subscription_confirmed.is_(True),
        ParticipantRegistration.registration_completed_at.isnot(None),
        ParticipantRegistration.participation_type == "Колесо Фортуни",
    )

    if last_spin is not None:
        query = query.filter(
            ParticipantRegistration.registration_completed_at
            > last_spin.datetime
        )

    return (
        query.order_by(
            ParticipantRegistration.registration_completed_at.desc(),
            ParticipantRegistration.id.desc(),
        )
        .first()
    )


async def check_channel_subscription(user_id_str: str, is_admin: bool) -> bool:
    """
    Перевіряє підписку на канал безпосередньо перед прокруткою.
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


# =========================
# СПОВІЩЕННЯ
# =========================


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
    registration_id: int | None = None,
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
    ]

    if registration_id is not None:
        caption_parts.append(f"Реєстрація №{registration_id}")

    caption_parts.extend(
        [
            "",
            f"Імʼя: {lead.name}",
            f"Телефон: {lead.phone}",
            telegram_line,
            f"Telegram ID: {user_id_str}",
            "",
            result_block,
        ]
    )

    if real_spin_number is not None:
        caption_parts.extend(
            [
                "",
                "📊 Статистика розіграшу:",
                f"Режим: {PRIZE_MODE}",
                f"Шанс виграшу: {WIN_CHANCE_PERCENT}%",
                f"Реальна прокрутка №: {real_spin_number}",
                f"Роздано подарунків: {awarded_prizes}",
            ]
        )

        if PRIZE_MODE == "controlled":
            caption_parts.append(
                f"Відкрито подарункових слотів: {unlocked_slots}"
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


# =========================
# ENDPOINT /spin
# =========================


@router.post("/spin")
async def spin(request: Request):
    try:
        data = await request.json()
    except Exception:
        prize, sector_index = get_error_result()
        return JSONResponse(
            {
                "prize": prize,
                "sector_index": sector_index,
                "repeat": True,
                "message": "Некоректний запит. Відкрий колесо через Telegram-бота.",
            },
            status_code=400,
        )

    username = data.get("username") or "unknown"
    user_id = data.get("user_id")

    if user_id is None:
        prize, sector_index = get_error_result()
        return JSONResponse(
            {
                "prize": prize,
                "sector_index": sector_index,
                "repeat": True,
                "message": "Не вдалося визначити користувача. Відкрий колесо через Telegram-бота.",
            },
            status_code=400,
        )

    user_id_str = str(user_id)
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
                prize, sector_index = get_error_result()

                return JSONResponse(
                    {
                        "prize": prize,
                        "sector_index": sector_index,
                        "repeat": True,
                        "message": "Спочатку пройди реєстрацію в боті.",
                    }
                )

            # Підписку перевіряємо ще раз на backend.
            is_subscribed = await check_channel_subscription(
                user_id_str=user_id_str,
                is_admin=is_admin,
            )

            if not is_subscribed:
                prize, sector_index = get_error_result()

                return JSONResponse(
                    {
                        "prize": prize,
                        "sector_index": sector_index,
                        "repeat": True,
                        "message": (
                            "Щоб крутити колесо, потрібно бути підписаним "
                            f"на наш канал: {CHANNEL_URL}"
                        ),
                    }
                )

            # КРИТИЧНА ПЕРЕВІРКА №1.
            # Якщо весь реальний фонд уже = 0, Spin не створюємо взагалі.
            db.expire_all()
            total_stock_before = get_total_real_gift_stock(db)

            if total_stock_before == 0:
                bot_obj, _ = get_bot_and_dispatcher()
                await notify_prizes_depleted_once(bot_obj)
                return get_fund_depleted_response()

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
                            "sector_index": FALLBACK_SECTOR_INDEX,
                            "repeat": True,
                            "message": (
                                "Ви вже крутили колесо. "
                                f"Наступна спроба через {format_time_left(time_left)}."
                            ),
                        }
                    )

            # Для звичайного користувача недостатньо просто мати Lead.
            # Потрібна завершена НОВА реєстрація з фото чека та підтвердженою
            # підпискою. Після попереднього spin стара реєстрація більше
            # не дає права на нову прокрутку.
            eligible_registration: ParticipantRegistration | None = None

            if not is_admin:
                eligible_registration = get_latest_eligible_registration(
                    db,
                    user_id_str=user_id_str,
                    last_spin=last_spin,
                )

                if eligible_registration is None:
                    prize, sector_index = get_error_result()

                    return JSONResponse(
                        {
                            "prize": prize,
                            "sector_index": sector_index,
                            "repeat": True,
                            "message": (
                                "Перед прокруткою потрібно пройти нову реєстрацію "
                                "в Telegram-боті, надіслати фото чека та підтвердити підписку."
                            ),
                        }
                    )

            registration_id = (
                int(eligible_registration.id)
                if eligible_registration is not None
                else None
            )

            # Prank користувач проходить усі перевірки реєстрації/підписки,
            # але не забирає реальний stock.
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
                    registration_id=registration_id,
                )

                return JSONResponse(
                    {
                        "prize": PRANK_TEXT,
                        "sector_index": PRANK_SECTOR_INDEX,
                        "repeat": False,
                        "message": "",
                    }
                )

            # Ці значення потрібні після виходу з глобального lock.
            fund_depleted_while_waiting = False
            fund_depleted_after_award = False
            excel_registration_id: int | None = None
            excel_prize: str | None = None
            prize = "Помилка"
            sector_index = FALLBACK_SECTOR_INDEX
            real_spin_number = 0
            unlocked_slots = 0
            awarded_prizes = 0

            # Вибір + списання stock + створення Spin виконуються одним
            # критичним блоком. Саме тут захищається останній подарунок.
            async with PRIZE_DISTRIBUTION_LOCK:
                # Поки цей користувач чекав lock, інший міг забрати
                # останній подарунок. Тому обов'язково перечитуємо БД.
                db.expire_all()
                total_stock_inside_lock = get_total_real_gift_stock(db)

                if total_stock_inside_lock == 0:
                    fund_depleted_while_waiting = True
                else:
                    real_spin_count_before = get_real_spin_count(db)

                    # Адмін не рухає прогрес реального розіграшу.
                    real_spin_number = real_spin_count_before + (
                        0 if is_admin else 1
                    )

                    prize, sector_index, selected_prize_stock = choose_prize(
                        db=db,
                        real_spin_number=real_spin_number,
                    )

                    # Адмінський тест ніколи не зменшує реальний stock.
                    if selected_prize_stock is not None and not is_admin:
                        if selected_prize_stock.stock is not None:
                            # Додатковий захист від від'ємного залишку.
                            if selected_prize_stock.stock <= 0:
                                db.rollback()
                                fund_depleted_while_waiting = (
                                    get_total_real_gift_stock(db) == 0
                                )
                            else:
                                selected_prize_stock.stock -= 1

                    if not fund_depleted_while_waiting:
                        row = Spin(
                            username=str(username),
                            user_id=user_id_str,
                            prize=prize,
                        )
                        db.add(row)
                        db.flush()

                        real_win = is_real_win(
                            prize=prize,
                            sector_index=sector_index,
                            is_prank=False,
                        )

                        # Прив'язуємо фактичний виграш до конкретної
                        # реєстрації учасника. Адмінські тести сюди не йдуть.
                        if (
                            real_win
                            and not is_admin
                            and eligible_registration is not None
                        ):
                            eligible_registration.prize = prize
                            excel_registration_id = int(eligible_registration.id)
                            excel_prize = str(prize)

                        db.commit()
                        db.refresh(row)

                        # Після commit ще раз читаємо залишок.
                        db.expire_all()
                        total_stock_after = get_total_real_gift_stock(db)
                        fund_depleted_after_award = (
                            total_stock_after == 0
                            and selected_prize_stock is not None
                            and not is_admin
                        )

                        unlocked_slots = get_unlocked_prize_slots(
                            real_spin_number
                        )
                        awarded_prizes = get_awarded_real_prize_count(db)

            # Якщо останній подарунок забрав інший користувач, поки ми
            # чекали глобальний lock, цьому користувачу Spin НЕ створюємо.
            if fund_depleted_while_waiting:
                bot_obj, _ = get_bot_and_dispatcher()
                await notify_prizes_depleted_once(bot_obj)
                return get_fund_depleted_response()

            # Excel — вторинний експорт. Помилка Excel не повинна
            # скасовувати вже успішний spin у БД.
            if excel_registration_id is not None and excel_prize is not None:
                try:
                    await update_registration_prize_in_excel(
                        registration_id=excel_registration_id,
                        prize=excel_prize,
                    )
                except Exception as e:
                    logging.exception(
                        "Failed to update prize in participants Excel: %s",
                        e,
                    )

            # Останній реальний подарунок щойно видано.
            # notify_prizes_depleted_once() сама гарантує одноразове
            # повідомлення через persistent AppSetting.
            if fund_depleted_after_award:
                bot_obj, _ = get_bot_and_dispatcher()
                await notify_prizes_depleted_once(bot_obj)

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
                registration_id=registration_id,
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

        except Exception as e:
            db.rollback()
            logging.exception(
                "Unhandled /spin error for user %s: %s",
                user_id_str,
                e,
            )

            prize, sector_index = get_error_result()
            return JSONResponse(
                {
                    "prize": prize,
                    "sector_index": sector_index,
                    "repeat": True,
                    "message": "Сталася помилка. Спробуй відкрити колесо ще раз через Telegram-бота.",
                },
                status_code=500,
            )

        finally:
            db.close()K_TEXT,
    PRANK_SECTOR_INDEX,
    PRIZE_UNLOCK_SPINS,
    PRIZE_MODE,
    WIN_CHANCE_PERCENT,
    CAMPAIGN_START_AT_UTC,
)
from bot import get_bot_and_dispatcher

router = APIRouter()

# Сектора «Нічого» більше немає.
# Fallback-сектор — 0, Косметичка OXVA.
# Використовується тільки для помилок / повторної спроби / коли подарунки закінчились.
FALLBACK_SECTOR_INDEX = 0

# Захист від подвійного натискання одного користувача
SPIN_LOCKS: dict[str, asyncio.Lock] = {}

# Глобальний захист роздачі подарунків.
# Потрібен, щоб два різні користувачі одночасно не забрали один і той самий подарунок.
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
    """
    Перевіряє, чи це реальний виграш.

    Сектора «Нічого» більше немає, тому НЕ перевіряємо sector_index.
    Бо сектор 2 тепер — це Сумка.
    """

    if is_prank:
        return False

    if not prize:
        return False

    if prize == "Нічого":
        return False

    if prize == "Помилка":
        return False

    if prize == "Подарунки закінчились":
        return False

    if prize == PRANK_TEXT:
        return False

    return True


def get_no_prize_result() -> tuple[str, int]:
    """
    Технічний результат, якщо подарунки закінчились або шанс не спрацював.

    На самому колесі такого сектора вже немає, тому ставимо fallback 0.
    При WIN_CHANCE_PERCENT = 100.0 це спрацює тільки коли всі stock = 0.
    """

    return "Подарунки закінчились", FALLBACK_SECTOR_INDEX


def get_error_result() -> tuple[str, int]:
    return "Помилка", FALLBACK_SECTOR_INDEX


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
    Технічні результати, prank і адмінські тести не рахуються.
    """

    query = (
        db.query(Spin)
        .filter(Spin.prize != "Нічого")
        .filter(Spin.prize != "Подарунки закінчились")
        .filter(Spin.prize != "Помилка")
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
    Використовується тільки для режиму PRIZE_MODE = controlled.
    """

    return sum(
        1
        for unlock_spin in PRIZE_UNLOCK_SPINS
        if real_spin_number >= unlock_spin
    )


def get_available_gift_prizes(db) -> list[PrizeStock]:
    """
    Доступні подарунки.

    ВАЖЛИВО:
    Сектора «Нічого» більше немає, тому сектор 2 НЕ виключаємо.
    Сектор 2 тепер — це Сумка.
    """

    return (
        db.query(PrizeStock)
        .filter(PrizeStock.prize != "Нічого")
        .filter(PrizeStock.prize != "Подарунки закінчились")
        .filter(PrizeStock.prize != "Помилка")
        .filter(PrizeStock.prize != PRANK_TEXT)
        .filter(PrizeStock.weight > 0)
        .filter(PrizeStock.stock > 0)
        .all()
    )


def choose_available_gift_prize(db) -> tuple[str, int, PrizeStock | None]:
    """
    Обирає випадковий подарунок із тих, які ще є в наявності.
    """

    available_gifts = get_available_gift_prizes(db)

    if not available_gifts:
        prize, sector_index = get_no_prize_result()
        return prize, sector_index, None

    selected = random.choices(
        available_gifts,
        weights=[p.weight for p in available_gifts],
        k=1,
    )[0]

    return selected.prize, selected.sector_index, selected


def choose_chance_prize(db) -> tuple[str, int, PrizeStock | None]:
    """
    Логіка шансу:

    1. Генеруємо випадкове число від 0 до 100.
    2. Якщо число більше за WIN_CHANCE_PERCENT — технічний результат.
    3. Якщо шанс спрацював — видаємо один із доступних подарунків.
    4. Якщо подарунки закінчились — технічний результат.

    При WIN_CHANCE_PERCENT = 100.0 кожна прокрутка видає приз,
    поки у PRIZES_ є stock > 0.
    """

    chance = max(0.0, min(100.0, float(WIN_CHANCE_PERCENT)))
    roll = random.uniform(0, 100)

    if roll > chance:
        prize, sector_index = get_no_prize_result()
        return prize, sector_index, None

    return choose_available_gift_prize(db)


def choose_controlled_prize(
    db,
    real_spin_number: int,
) -> tuple[str, int, PrizeStock | None]:
    """
    Контрольована логіка:

    1. Якщо ще не настав поріг подарунка — технічний результат.
    2. Якщо подарунковий слот відкрився — видаємо подарунок.
    3. Якщо всі подарунки закінчились — технічний результат.
    """

    unlocked_slots = get_unlocked_prize_slots(real_spin_number)
    awarded_prizes = get_awarded_real_prize_count(db)

    # На цьому етапі ще не можна видати новий подарунок
    if awarded_prizes >= unlocked_slots:
        prize, sector_index = get_no_prize_result()
        return prize, sector_index, None

    return choose_available_gift_prize(db)


def choose_prize(
    db,
    real_spin_number: int,
) -> tuple[str, int, PrizeStock | None]:
    """
    Основний вибір подарунка.
    Режим задається в config.py через PRIZE_MODE.

    PRIZE_MODE = "chance" — працює шанс у відсотках.
    PRIZE_MODE = "controlled" — працюють пороги PRIZE_UNLOCK_SPINS.
    """

    if PRIZE_MODE == "controlled":
        return choose_controlled_prize(
            db=db,
            real_spin_number=real_spin_number,
        )

    return choose_chance_prize(db)


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
                f"Режим: {PRIZE_MODE}",
                f"Шанс виграшу: {WIN_CHANCE_PERCENT}%",
                f"Реальна прокрутка №: {real_spin_number}",
                f"Роздано подарунків: {awarded_prizes}",
            ]
        )

        if PRIZE_MODE == "controlled":
            caption_parts.append(
                f"Відкрито подарункових слотів: {unlocked_slots}"
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
                prize, sector_index = get_error_result()

                return JSONResponse(
                    {
                        "prize": prize,
                        "sector_index": sector_index,
                        "repeat": True,
                        "message": "Спочатку пройди реєстрацію в боті.",
                    }
                )

            is_subscribed = await check_channel_subscription(
                user_id_str=user_id_str,
                is_admin=is_admin,
            )

            if not is_subscribed:
                prize, sector_index = get_error_result()

                return JSONResponse(
                    {
                        "prize": prize,
                        "sector_index": sector_index,
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
                            "sector_index": FALLBACK_SECTOR_INDEX,
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

                prize, sector_index, selected_prize_stock = choose_prize(
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