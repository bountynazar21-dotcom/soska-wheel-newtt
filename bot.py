import datetime
import logging

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramAPIError

from database import (
    SessionLocal,
    Lead,
    Spin,
    PrizeStock,
    AppSetting,
    ParticipantRegistration,
    ensure_prize_stock,
)
from participants_excel import append_registration_to_excel
from config import (
    BOT_TOKEN,
    WEBAPP_URL,
    CHANNEL_USERNAME,
    CHANNEL_URL,
    SPIN_COOLDOWN_DAYS,
    ADMINS,
    PRIZE_POOL_VERSION,
    PRIZES_,
)

bot: Bot | None = None
dp: Dispatcher | None = None
router = Router()


class Registration(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_receipt = State()
    waiting_for_subscription = State()


def is_admin_user(user_id: int | str | None) -> bool:
    if user_id is None:
        return False

    user_id_str = str(user_id)
    return user_id_str.isdigit() and int(user_id_str) in ADMINS


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


def build_webapp_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎡 Відкрити колесо фортуни",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )


def build_subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Підписатися на канал",
                    url=CHANNEL_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я підписався",
                    callback_data="check_subscription",
                )
            ],
        ]
    )


async def is_user_subscribed(bot: Bot, user_id: int) -> bool:
    if is_admin_user(user_id):
        return True

    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in (
            "member",
            "administrator",
            "creator",
            "restricted",
        )
    except TelegramAPIError as e:
        logging.error(f"Subscription check failed for user {user_id}: {e}")
        return False


def get_active_cooldown(user_id: str | int | None):
    if is_admin_user(user_id):
        return None

    user_id_str = str(user_id)
    db = SessionLocal()

    try:
        last_spin = (
            db.query(Spin)
            .filter(Spin.user_id == user_id_str)
            .order_by(Spin.datetime.desc())
            .first()
        )

        if not last_spin:
            return None

        now = datetime.datetime.utcnow()
        cooldown_until = last_spin.datetime + datetime.timedelta(
            days=SPIN_COOLDOWN_DAYS
        )

        if now >= cooldown_until:
            return None

        return cooldown_until - now
    finally:
        db.close()


def get_total_prize_stock() -> int | None:
    """
    int  -> сумарний актуальний залишок.
    None -> є безлімітний prize (stock=None), тому фонд не вичерпаний.
    """
    db = SessionLocal()

    try:
        ensure_prize_stock(db)
        rows = db.query(PrizeStock).all()

        if any(row.stock is None for row in rows):
            return None

        return sum(max(0, int(row.stock or 0)) for row in rows)
    finally:
        db.close()


def prizes_available() -> bool:
    total_stock = get_total_prize_stock()
    return total_stock is None or total_stock > 0


def get_stock_data() -> tuple[list[PrizeStock], int | None, int | None]:
    db = SessionLocal()

    try:
        ensure_prize_stock(db)
        rows = (
            db.query(PrizeStock)
            .order_by(PrizeStock.sector_index.asc())
            .all()
        )

        result = [
            {
                "sector_index": row.sector_index,
                "prize": row.prize,
                "stock": row.stock,
            }
            for row in rows
        ]

        if any(item["stock"] is None for item in result):
            return result, None, None

        total_remaining = sum(
            max(0, int(item["stock"] or 0))
            for item in result
        )

        initial_by_sector = {
            int(item["sector_index"]): int(item["stock"])
            for item in PRIZES_
            if item.get("stock") is not None
        }
        initial_total = sum(
            initial_by_sector.get(int(item["sector_index"]), 0)
            for item in result
        )
        total_awarded = max(0, initial_total - total_remaining)

        return result, total_remaining, total_awarded
    finally:
        db.close()


def save_lead(
    *,
    user_id: str,
    username: str,
    name: str,
    phone: str,
    receipt_file_id: str | None = None,
) -> bool:
    db = SessionLocal()

    try:
        lead = db.query(Lead).filter(Lead.user_id == user_id).first()

        if lead is None:
            lead = Lead(
                username=username,
                user_id=user_id,
                name=name,
                phone=phone,
                receipt_file_id=receipt_file_id,
            )
            db.add(lead)
        else:
            lead.username = username
            lead.name = name
            lead.phone = phone
            if receipt_file_id:
                lead.receipt_file_id = receipt_file_id

        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logging.error(f"Failed to save lead: {e}")
        return False
    finally:
        db.close()


def create_pending_registration(
    *,
    user_id: str,
    username: str,
    name: str,
    phone: str,
    receipt_file_id: str,
) -> int | None:
    db = SessionLocal()

    try:
        pending = (
            db.query(ParticipantRegistration)
            .filter(
                ParticipantRegistration.user_id == user_id,
                ParticipantRegistration.registration_completed_at.is_(None),
            )
            .order_by(ParticipantRegistration.id.desc())
            .first()
        )

        if pending is None:
            pending = ParticipantRegistration(
                user_id=user_id,
                username=username,
                name=name,
                phone=phone,
                receipt_file_id=receipt_file_id,
                subscription_confirmed=False,
            )
            db.add(pending)
        else:
            pending.username = username
            pending.name = name
            pending.phone = phone
            pending.receipt_file_id = receipt_file_id

        db.commit()
        db.refresh(pending)
        return int(pending.id)
    except Exception as e:
        db.rollback()
        logging.error(f"Failed to create pending registration: {e}")
        return None
    finally:
        db.close()


def get_pending_registration(
    *,
    user_id: str,
    registration_id: int | None = None,
) -> dict | None:
    db = SessionLocal()

    try:
        query = db.query(ParticipantRegistration).filter(
            ParticipantRegistration.user_id == user_id,
            ParticipantRegistration.registration_completed_at.is_(None),
        )

        if registration_id is not None:
            query = query.filter(ParticipantRegistration.id == registration_id)

        row = query.order_by(ParticipantRegistration.id.desc()).first()

        if row is None:
            return None

        return {
            "id": int(row.id),
            "user_id": str(row.user_id),
            "username": str(row.username or "user"),
            "name": str(row.name or "-"),
            "phone": str(row.phone or "-"),
            "receipt_file_id": str(row.receipt_file_id or ""),
        }
    finally:
        db.close()


def complete_registration(
    *,
    registration_id: int,
    participation_type: str,
) -> dict | None:
    db = SessionLocal()

    try:
        row = (
            db.query(ParticipantRegistration)
            .filter(ParticipantRegistration.id == registration_id)
            .first()
        )

        if row is None:
            return None

        if row.registration_completed_at is None:
            row.subscription_confirmed = True
            row.participation_type = participation_type
            row.registration_completed_at = datetime.datetime.utcnow()
            db.commit()
            db.refresh(row)

        return {
            "id": int(row.id),
            "user_id": str(row.user_id),
            "username": str(row.username or "user"),
            "name": str(row.name or "-"),
            "phone": str(row.phone or "-"),
            "receipt_file_id": str(row.receipt_file_id or ""),
            "subscription_confirmed": bool(row.subscription_confirmed),
            "participation_type": str(row.participation_type or participation_type),
            "registration_completed_at": row.registration_completed_at,
            "prize": str(row.prize or ""),
        }
    except Exception as e:
        db.rollback()
        logging.error(f"Failed to complete registration: {e}")
        return None
    finally:
        db.close()


async def notify_prizes_depleted_once(bot_obj: Bot) -> None:
    if get_total_prize_stock() != 0:
        return

    setting_key = f"prizes_depleted_notified::{PRIZE_POOL_VERSION}"
    db = SessionLocal()

    try:
        setting = (
            db.query(AppSetting)
            .filter(AppSetting.key == setting_key)
            .first()
        )

        if setting and setting.value == "1":
            return

        if setting is None:
            db.add(AppSetting(key=setting_key, value="1"))
        else:
            setting.value = "1"

        db.commit()
    except Exception as e:
        db.rollback()
        logging.error(f"Failed to save depletion notification flag: {e}")
        return
    finally:
        db.close()

    text = (
        "🚨 <b>ПОДАРУНКИ ЗАКІНЧИЛИСЯ</b>\n\n"
        "Усі подарунки в Колесі Фортуни були видані.\n\n"
        "🎁 Залишок: <b>0 шт.</b>\n\n"
        "Відтепер нові користувачі проходять реєстрацію, надсилають фото "
        "чека та підтверджують підписку, але колесо їм більше не відкривається."
    )

    for admin_id in ADMINS:
        try:
            await bot_obj.send_message(admin_id, text)
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")


async def notify_admins_about_no_spin_registration(
    *,
    bot_obj: Bot,
    registration: dict,
) -> None:
    username = registration["username"]
    telegram_name = (
        f"@{username}"
        if username != "user" and " " not in username and not username.startswith("@")
        else username
    )

    caption = (
        "📝 <b>НОВА РЕЄСТРАЦІЯ</b>\n\n"
        f"Імʼя: {registration['name']}\n"
        f"Телефон: {registration['phone']}\n"
        f"Telegram: {telegram_name}\n"
        f"Telegram ID: {registration['user_id']}\n\n"
        "📸 Чек отримано\n"
        "✅ Підписка підтверджена\n\n"
        "🎡 Колесо: недоступне\n"
        "🎁 Залишок подарунків: 0\n\n"
        "Користувача додано до бази учасників."
    )

    for admin_id in ADMINS:
        try:
            await bot_obj.send_photo(
                chat_id=admin_id,
                photo=registration["receipt_file_id"],
                caption=caption,
            )
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")


async def finish_registration_flow(
    *,
    user_id: str,
    registration_id: int,
    bot_obj: Bot,
    state: FSMContext,
    message: Message,
) -> None:
    total_stock = get_total_prize_stock()
    has_prizes = total_stock is None or total_stock > 0

    if has_prizes:
        cooldown_left = get_active_cooldown(user_id)
        if cooldown_left:
            await state.clear()
            await message.answer(
                "Ти вже крутив колесо 🎡\n"
                f"Наступна спроба буде доступна через {format_time_left(cooldown_left)}."
            )
            return

    participation_type = (
        "Колесо Фортуни"
        if has_prizes
        else "Реєстрація без спіна"
    )

    registration = complete_registration(
        registration_id=registration_id,
        participation_type=participation_type,
    )

    if registration is None:
        await message.answer("Сталася помилка. Спробуй ще раз /start")
        return

    try:
        await append_registration_to_excel(registration)
    except Exception as e:
        logging.exception(f"Excel export failed: {e}")

    await state.clear()

    if has_prizes:
        await message.answer(
            "Все готово! 🎉\n"
            "Натискай кнопку нижче, щоб відкрити колесо фортуни:",
            reply_markup=build_webapp_keyboard(),
        )
        return

    await notify_prizes_depleted_once(bot_obj)
    await notify_admins_about_no_spin_registration(
        bot_obj=bot_obj,
        registration=registration,
    )

    await message.answer(
        "✅ <b>Реєстрацію успішно завершено!</b>\n\n"
        "Дякуємо за участь 💜\n"
        "Твої дані зареєстровані."
    )


@router.message(Command("stock"))
async def cmd_stock(message: Message) -> None:
    if message.from_user is None or not is_admin_user(message.from_user.id):
        return

    rows, total_remaining, total_awarded = get_stock_data()
    lines = ["🎁 <b>Актуальний залишок подарунків</b>", ""]

    if total_remaining == 0:
        lines.append("Усі подарунки закінчилися.")
    else:
        for row in rows:
            stock_text = "∞" if row["stock"] is None else str(max(0, int(row["stock"])))
            lines.append(f"{row['prize']} — <b>{stock_text}</b> шт.")

    lines.extend(["", "━━━━━━━━━━━━━━"])
    lines.append(
        "Всього залишилось: <b>∞</b>"
        if total_remaining is None
        else f"Всього залишилось: <b>{total_remaining} шт.</b>"
    )

    if total_awarded is not None:
        lines.append(f"Всього видано: <b>{total_awarded} шт.</b>")

    await message.answer("\n".join(lines))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()

    if message.from_user is None:
        return

    user_id = str(message.from_user.id)

    # Після завершення призового фонду cooldown більше не блокує реєстрацію,
    # бо користувач уже не отримує spin.
    if prizes_available():
        cooldown_left = get_active_cooldown(user_id)
        if cooldown_left:
            await message.answer(
                "Ти вже крутив колесо 🎡\n"
                f"Наступна спроба буде доступна через {format_time_left(cooldown_left)}."
            )
            return

    await message.answer(
        "Привіт! 👋\n\nНапиши, будь ласка, своє імʼя.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Registration.waiting_for_name)


@router.message(Registration.waiting_for_name, F.text)
async def process_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()

    if len(name) < 2:
        await message.answer("Введи, будь ласка, коректне імʼя.")
        return

    await state.update_data(name=name)
    await message.answer(
        "Супер! ✨\nТепер напиши, будь ласка, номер телефону у форматі +380..."
    )
    await state.set_state(Registration.waiting_for_phone)


@router.message(Registration.waiting_for_name)
async def name_required(message: Message) -> None:
    await message.answer("Будь ласка, надішли імʼя текстом.")


@router.message(Registration.waiting_for_phone, F.text)
async def process_phone(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return

    phone = message.text.strip()
    digits_count = sum(ch.isdigit() for ch in phone)

    if digits_count < 10:
        await message.answer("Введи, будь ласка, коректний номер телефону.")
        return

    user_id = str(message.from_user.id)

    if prizes_available():
        cooldown_left = get_active_cooldown(user_id)
        if cooldown_left:
            await state.clear()
            await message.answer(
                "Ти вже крутив колесо 🎡\n"
                f"Наступна спроба буде доступна через {format_time_left(cooldown_left)}."
            )
            return

    data = await state.get_data()
    name = data.get("name") or "-"
    username = (
        message.from_user.username
        or f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
        or "user"
    )

    if not save_lead(
        user_id=user_id,
        username=str(username),
        name=str(name),
        phone=str(phone),
    ):
        await message.answer("Сталася помилка. Спробуй ще раз /start")
        return

    await state.update_data(
        phone=phone,
        username=str(username),
    )

    await message.answer(
        "Чудово! 📸\n\n"
        "Тепер надішли, будь ласка, <b>фото чека про покупку</b>."
    )
    await state.set_state(Registration.waiting_for_receipt)


@router.message(Registration.waiting_for_phone)
async def phone_required(message: Message) -> None:
    await message.answer("Будь ласка, надішли номер телефону текстом ☎️")


@router.message(Registration.waiting_for_receipt, F.photo)
async def process_receipt(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    if message.from_user is None or not message.photo:
        return

    user_id = str(message.from_user.id)
    receipt_file_id = message.photo[-1].file_id
    data = await state.get_data()

    name = data.get("name") or "-"
    phone = data.get("phone") or "-"
    username = data.get("username") or "user"

    if not save_lead(
        user_id=user_id,
        username=str(username),
        name=str(name),
        phone=str(phone),
        receipt_file_id=receipt_file_id,
    ):
        await message.answer("Сталася помилка при збереженні чека. Спробуй ще раз /start")
        return

    registration_id = create_pending_registration(
        user_id=user_id,
        username=str(username),
        name=str(name),
        phone=str(phone),
        receipt_file_id=receipt_file_id,
    )

    if registration_id is None:
        await message.answer("Сталася помилка при реєстрації. Спробуй ще раз /start")
        return

    await state.update_data(
        receipt_file_id=receipt_file_id,
        registration_id=registration_id,
    )

    subscribed = await is_user_subscribed(bot, message.from_user.id)

    if subscribed:
        await finish_registration_flow(
            user_id=user_id,
            registration_id=registration_id,
            bot_obj=bot,
            state=state,
            message=message,
        )
        return

    await state.set_state(Registration.waiting_for_subscription)
    await message.answer(
        "Фото чека отримано ✅\n\n"
        "Тепер підпишись на наш Telegram-канал 👇",
        reply_markup=build_subscribe_keyboard(),
    )


@router.message(Registration.waiting_for_receipt)
async def receipt_required(message: Message) -> None:
    await message.answer(
        "Будь ласка, надішли саме <b>фото чека</b> 📸\n"
        "Документ, текст, стікер або інший тип повідомлення не підходить."
    )


@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    subscribed = await is_user_subscribed(bot, callback.from_user.id)

    if not subscribed:
        await callback.answer(
            "Підписку ще не знайдено. Підпишись на канал і натисни ще раз.",
            show_alert=True,
        )
        return

    user_id = str(callback.from_user.id)
    data = await state.get_data()
    registration_id = data.get("registration_id")

    if registration_id is None:
        pending = get_pending_registration(user_id=user_id)
        if pending:
            registration_id = pending["id"]
            await state.update_data(
                registration_id=registration_id,
                receipt_file_id=pending["receipt_file_id"],
                name=pending["name"],
                phone=pending["phone"],
                username=pending["username"],
            )

    if registration_id is None:
        await callback.answer(
            "Реєстрацію не знайдено. Почни ще раз через /start.",
            show_alert=True,
        )
        return

    await callback.answer("Підписку підтверджено ✅")

    if callback.message:
        await finish_registration_flow(
            user_id=user_id,
            registration_id=int(registration_id),
            bot_obj=bot,
            state=state,
            message=callback.message,
        )


@router.message(Registration.waiting_for_subscription)
async def subscription_waiting_message(message: Message) -> None:
    await message.answer(
        "Підпишись на канал, а потім натисни кнопку <b>«✅ Я підписався»</b> нижче.",
        reply_markup=build_subscribe_keyboard(),
    )


def get_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    global bot, dp

    if bot is None or dp is None:
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is not set")

        bot = Bot(BOT_TOKEN, parse_mode="HTML")
        dp = Dispatcher()
        dp.include_router(router)

    return bot, dp


async def run_bot():
    bot_obj, dp_obj = get_bot_and_dispatcher()

    try:
        await dp_obj.start_polling(bot_obj)
    except TelegramAPIError as e:
        logging.error(f"Polling error: {e}")


async def shutdown_bot():
    global bot

    if bot:
        await bot.session.close()