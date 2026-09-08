import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from bot import (
    get_bot_and_dispatcher,
    notify_prizes_depleted_once,
    run_bot,
    shutdown_bot,
)
from config import PARTICIPANTS_XLSX_PATH
from database import SessionLocal, ensure_prize_stock, init_db
from routes.admin import router as admin_router
from routes.spin import router as spin_router


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================
# FASTAPI
# =========================

app = FastAPI()

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static",
)

app.include_router(spin_router)
app.include_router(admin_router)


# Зберігаємо посилання на polling task, щоб коректно завершити її
# під час shutdown Railway / Uvicorn.
bot_polling_task: asyncio.Task | None = None


# =========================
# ДОПОМІЖНІ ФУНКЦІЇ
# =========================


def ensure_excel_directory() -> None:
    """
    Створює директорію для participants.xlsx, якщо її ще немає.

    На Railway рекомендовано:
    PARTICIPANTS_XLSX_PATH=/app/data/participants.xlsx
    і Volume, змонтований у /app/data.

    Сам Excel-файл тут не створюємо — він буде створений модулем
    participants_excel.py при першій завершеній реєстрації.
    """

    excel_path = Path(PARTICIPANTS_XLSX_PATH).expanduser()
    parent = excel_path.parent

    # Для простого "participants.xlsx" parent == ".".
    parent.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Participants Excel path: %s",
        excel_path,
    )



def log_bot_task_result(task: asyncio.Task) -> None:
    """
    Не дає помилці polling-task загубитися як
    "Task exception was never retrieved".
    """

    if task.cancelled():
        logger.info("Telegram bot polling task cancelled")
        return

    try:
        exception = task.exception()
    except asyncio.CancelledError:
        logger.info("Telegram bot polling task cancelled")
        return

    if exception is not None:
        logger.exception(
            "Telegram bot polling task stopped with an error",
            exc_info=exception,
        )
    else:
        logger.info("Telegram bot polling task finished")


# =========================
# STARTUP
# =========================


@app.on_event("startup")
async def startup() -> None:
    global bot_polling_task

    logger.info("Starting application...")

    # 1. Створюємо нові таблиці та запускаємо backward-compatible
    # SQLite-міграції для старого wheel.db.
    init_db()
    logger.info("Database initialization/migrations complete")

    # 2. Синхронізуємо призовий фонд.
    # Якщо PRIZE_POOL_VERSION не змінився — фактичний stock НЕ скидається.
    db = SessionLocal()
    try:
        ensure_prize_stock(db)
    except Exception:
        db.rollback()
        logger.exception("Failed to synchronize prize stock")
        raise
    finally:
        db.close()

    logger.info("Prize stock synchronization complete")

    # 3. Готуємо директорію для Excel-бази учасників.
    try:
        ensure_excel_directory()
    except Exception:
        logger.exception(
            "Failed to prepare participants Excel directory: %s",
            PARTICIPANTS_XLSX_PATH,
        )
        # Excel є додатковим експортом, а не основною БД.
        # Тому через помилку директорії не зупиняємо весь застосунок.

    # 4. Створюємо Bot/Dispatcher до запуску polling.
    # Якщо BOT_TOKEN відсутній, краще впасти на startup одразу,
    # ніж тихо отримати помилку у фоновій task.
    bot_obj, _ = get_bot_and_dispatcher()

    # 5. Якщо після рестарту фонд уже дорівнює 0, але повідомлення адміну
    # ще не було зафіксоване, функція відправить його один раз.
    # Якщо прапорець уже стоїть — нічого повторно не відправиться.
    try:
        await notify_prizes_depleted_once(bot_obj)
    except Exception:
        # Помилка Telegram не повинна ламати запуск API/бота.
        logger.exception(
            "Failed to check/send prize depletion notification on startup"
        )

    # 6. Запускаємо Telegram long polling окремою task,
    # щоб FastAPI продовжував обслуговувати WebApp/API.
    if bot_polling_task is None or bot_polling_task.done():
        bot_polling_task = asyncio.create_task(
            run_bot(),
            name="telegram-bot-polling",
        )
        bot_polling_task.add_done_callback(log_bot_task_result)

    logger.info("Application startup complete")


# =========================
# SHUTDOWN
# =========================


@app.on_event("shutdown")
async def shutdown() -> None:
    global bot_polling_task

    logger.info("Shutting down application...")

    # Спочатку зупиняємо polling task.
    if bot_polling_task is not None and not bot_polling_task.done():
        bot_polling_task.cancel()

        try:
            await bot_polling_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error while stopping Telegram polling task")

    bot_polling_task = None

    # Потім закриваємо HTTP-сесію Telegram Bot.
    try:
        await shutdown_bot()
    except Exception:
        logger.exception("Error while closing Telegram bot session")

    logger.info("Application shutdown complete")


# =========================
# SERVICE ROUTES
# =========================


@app.get("/ping")
async def ping() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "soska-wheel",
        }
    )


@app.get("/")
async def root() -> RedirectResponse:
    # Версія вирівняна з WEBAPP_URL у config.py.
    return RedirectResponse(url="/static/index.html?v=46")


# =========================
# LOCAL START
# =========================


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )