import os

APP_BASE_URL = os.getenv(
    "APP_BASE_URL",
    "https://soska-wheel-newtt-production.up.railway.app",
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

WEBAPP_URL = os.getenv(
    "WEBAPP_URL",
    f"{APP_BASE_URL}/static/index.html?v=40",
)

# Telegram ID адміністраторів
ADMINS: set[int] = {
    5480082089,
}

CHANNEL_USERNAME = "@soska_bar"
CHANNEL_URL = "https://t.me/soska_bar"

# Користувач може крутити колесо один раз на 7 днів
SPIN_COOLDOWN_DAYS = 7

# Очікувана кількість учасників / прокруток
EXPECTED_PARTICIPANTS = 151

# Режим видачі подарунків:
# chance — подарунки видаються відповідно до шансу WIN_CHANCE_PERCENT
# controlled — подарунки відкриваються на прокрутках PRIZE_UNLOCK_SPINS
PRIZE_MODE = "chance"

# 100.0 = кожна реальна прокрутка виграє приз,
# поки є залишки подарунків у PRIZES_
WIN_CHANCE_PERCENT = 100.0

# У режимі chance цей список не використовується
PRIZE_UNLOCK_SPINS = []

# Старт розіграшу:
# 13 серпня 2026 року о 07:00 за Києвом
CAMPAIGN_START_AT_UTC = "2026-08-13T04:00:00"

# Кінець розіграшу:
# 20 серпня 2026 року о 21:00 за Києвом
CAMPAIGN_END_AT_UTC = "2026-08-20T18:00:00"

# Версія призового фонду
# Змінюємо версію, щоб база оновила назви та залишки призів
PRIZE_POOL_VERSION = os.getenv(
    "PRIZE_POOL_VERSION",
    "oxva-merch-151-prizes-v3",
)

# ПОРЯДОК СЕКТОРІВ НА КОЛЕСІ:
# ВІД ВЕРХУ ЗА ГОДИННИКОВОЮ:
#
# 0 — Косметичка OXVA
# 1 — Панамка
# 2 — Сумка
# 3 — XLIM 3 Ultra
# 4 — Окуляри OXVA
# 5 — Кепка
PRIZES_ = [
    {
        "sector_index": 0,
        "prize": "Косметичка OXVA",
        "stock": 34,
        "weight": 34,
    },
    {
        "sector_index": 1,
        "prize": "Панамка",
        "stock": 8,
        "weight": 8,
    },
    {
        "sector_index": 2,
        "prize": "Сумка",
        "stock": 26,
        "weight": 26,
    },
    {
        "sector_index": 3,
        "prize": "XLIM 3 Ultra",
        "stock": 1,
        "weight": 1,
    },
    {
        "sector_index": 4,
        "prize": "Окуляри OXVA",
        "stock": 17,
        "weight": 17,
    },
    {
        "sector_index": 5,
        "prize": "Кепка",
        "stock": 65,
        "weight": 65,
    },
]

# Користувачі для жартівливої прокрутки
# Очищено, щоб усі користувачі брали участь нормально
PRANK_USER_IDS: set[int] = set()

PRANK_TEXT = "Хахах, попався шпіоніро ))"

# Сектора «Нічого» більше немає.
# Залишаємо технічно 0, щоб код не падав, якщо десь ця змінна імпортується.
PRANK_SECTOR_INDEX = 0