```python
import os


# =========================
# ОСНОВНІ URL / TELEGRAM
# =========================

APP_BASE_URL = os.getenv(
    "APP_BASE_URL",
    "https://soska-wheel-newtt-production.up.railway.app",
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

WEBAPP_URL = os.getenv(
    "WEBAPP_URL",
    f"{APP_BASE_URL}/static/index.html?v=46",
)


# =========================
# АДМІНІСТРАТОРИ
# =========================

# Telegram ID адміністраторів.
# Залишаємо як set[int], щоб усі поточні перевірки ADMINS працювали без змін.
ADMINS: set[int] = {
    5480082089,
}


# =========================
# TELEGRAM-КАНАЛ
# =========================

CHANNEL_USERNAME = os.getenv(
    "CHANNEL_USERNAME",
    "@soska_bar",
)

CHANNEL_URL = os.getenv(
    "CHANNEL_URL",
    "https://t.me/soska_bar",
)


# =========================
# COOLDOWN
# =========================

# Користувач може крутити колесо один раз на 7 днів.
# Після завершення призового фонду cooldown не заважає новій
# реєстрації без відкривання колеса.
SPIN_COOLDOWN_DAYS = int(
    os.getenv("SPIN_COOLDOWN_DAYS", "7")
)


# =========================
# EXCEL-БАЗА УЧАСНИКІВ
# =========================

# На Railway рекомендовано:
# PARTICIPANTS_XLSX_PATH=/app/data/participants.xlsx
#
# ВАЖЛИВО:
# /app/data повинен бути підключений як Railway Volume,
# інакше participants.xlsx зникне після redeploy/restart.
PARTICIPANTS_XLSX_PATH = os.getenv(
    "PARTICIPANTS_XLSX_PATH",
    "participants.xlsx",
)

# Час, який буде відображатись у Excel.
PARTICIPANTS_TIMEZONE = os.getenv(
    "PARTICIPANTS_TIMEZONE",
    "Europe/Kyiv",
)


# =========================
# НАЛАШТУВАННЯ РОЗІГРАШУ
# =========================

# Це інформаційне/цільове значення.
# Воно НЕ визначає момент завершення подарунків.
# Єдиний критерій завершення — фактичний stock у PrizeStock.
EXPECTED_PARTICIPANTS = int(
    os.getenv("EXPECTED_PARTICIPANTS", "151")
)

# Режим видачі подарунків:
# chance     — подарунки видаються відповідно до WIN_CHANCE_PERCENT;
# controlled — подарунки відкриваються на прокрутках PRIZE_UNLOCK_SPINS.
PRIZE_MODE = os.getenv(
    "PRIZE_MODE",
    "chance",
).strip().lower()

if PRIZE_MODE not in {"chance", "controlled"}:
    PRIZE_MODE = "chance"

# 100.0 = кожна реальна прокрутка виграє приз,
# поки фактично є PrizeStock.stock > 0.
try:
    WIN_CHANCE_PERCENT = float(
        os.getenv("WIN_CHANCE_PERCENT", "100.0")
    )
except (TypeError, ValueError):
    WIN_CHANCE_PERCENT = 100.0

WIN_CHANCE_PERCENT = max(
    0.0,
    min(100.0, WIN_CHANCE_PERCENT),
)

# У режимі chance цей список не використовується.
# Якщо знову буде потрібен controlled — пороги задаються тут числами spin.
PRIZE_UNLOCK_SPINS: list[int] = []


# =========================
# ПЕРІОД КАМПАНІЇ
# =========================

# Старт розіграшу:
# 25 вересня 2026 року о 09:00 за Києвом
# = 06:00 UTC.
CAMPAIGN_START_AT_UTC = "2026-09-25T06:00:00"

# Кінець розіграшу:
# 25 вересня 2026 року о 20:00 за Києвом
# = 17:00 UTC.
CAMPAIGN_END_AT_UTC = "2026-09-25T17:00:00"


# =========================
# ВЕРСІЯ ПРИЗОВОГО ФОНДУ
# =========================

# КРИТИЧНО:
# НЕ змінювати це значення просто через оновлення коду.
#
# ensure_prize_stock() використовує PRIZE_POOL_VERSION як сигнал,
# що почався НОВИЙ призовий фонд. Якщо версію змінити, база синхронізує
# stock з PRIZES_ і стартові залишки можуть бути завантажені знову.
#
# Версію змінюємо ТІЛЬКИ тоді, коли реально запускаємо новий фонд
# і свідомо хочемо завантажити нові стартові залишки.
PRIZE_POOL_VERSION = os.getenv(
    "PRIZE_POOL_VERSION",
    "oxva-merch-151-prizes-v5",
)


# =========================
# ПРИЗОВИЙ ФОНД
# =========================

# ПОРЯДОК СЕКТОРІВ НА КОЛЕСІ:
# ВІД ВЕРХУ ЗА ГОДИННИКОВОЮ:
#
# 0 — Косметичка OXVA
# 1 — Панамка
# 2 — Сумка
# 3 — XLIM 3 Ultra
# 4 — Окуляри OXVA
# 5 — Кепка
#
# stock нижче — СТАРТОВІ значення фонду.
# Після першої синхронізації актуальний залишок береться з PrizeStock у БД.
# Поки PRIZE_POOL_VERSION не змінюється, рестарт Railway НЕ повинен
# відновлювати ці стартові цифри.
PRIZES_ = [
    {
        "sector_index": 0,
        "prize": "Панамка OXVA",
        "stock": 8,
        "weight": 8,
    },
    {
        "sector_index": 1,
        "prize": "Кабель OXVA",
        "stock": 3,
        "weight": 3,
    },
    {
        "sector_index": 2,
        "prize": "Брелок OXVA",
        "stock": 3,
        "weight": 3,
    },
    {
        "sector_index": 3,
        "prize": "Под-система OXVA",
        "stock": 0,
        "weight": 0,
    },
    {
        "sector_index": 4,
        "prize": "Ароматизатор в машину",
        "stock": 10,
        "weight": 10,
    },
    {
        "sector_index": 5,
        "prize": "Пов'язка OXVA",
        "stock": 28,
        "weight": 28,
    },
]


# =========================
# PRANK-РЕЖИМ
# =========================

# Очищено, щоб усі звичайні користувачі брали участь нормально.
PRANK_USER_IDS: set[int] = set()

PRANK_TEXT = "Хахах, попався шпіоніро ))"

# Сектора «Нічого» більше немає.
# Залишаємо технічно 0, щоб старі імпорти не падали.
PRANK_SECTOR_INDEX = 0
```
