import asyncio
import datetime
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo


# =========================
# НАЛАШТУВАННЯ EXCEL
# =========================
# Railway:
# PARTICIPANTS_XLSX_PATH=/app/data/participants.xlsx
# PARTICIPANTS_TIMEZONE=Europe/Kyiv
#
# Важливо: /app/data має бути Railway Volume, інакше файл зникне
# після redeploy/restart контейнера.

PARTICIPANTS_XLSX_PATH = os.getenv(
    "PARTICIPANTS_XLSX_PATH",
    "participants.xlsx",
)
PARTICIPANTS_TIMEZONE = os.getenv(
    "PARTICIPANTS_TIMEZONE",
    "Europe/Kyiv",
)

SHEET_NAME = "Учасники"
TABLE_NAME = "ParticipantsTable"

# Видимі колонки. Остання технічна колонка з registration_id прихована.
HEADERS = [
    "№",
    "Дата реєстрації",
    "Час реєстрації",
    "Ім'я",
    "Номер телефону",
    "Telegram username",
    "Telegram ID",
    "Фото чека / Telegram file_id",
    "Підписка підтверджена",
    "Тип участі",
    "Результат / подарунок",
    "_registration_id",
]

VISIBLE_LAST_COLUMN = "K"
TECHNICAL_ID_COLUMN = "L"

# Один lock на процес бота. Для стандартного Railway deployment з одним
# процесом цього достатньо, щоб дві реєстрації не писали файл одночасно.
EXCEL_LOCK = asyncio.Lock()

# Стиль Soska Bar.
PURPLE_DARK = "4B1F6F"
PURPLE_LIGHT = "EEE6F7"
PURPLE_VERY_LIGHT = "F8F4FC"
WHITE = "FFFFFF"
TEXT_DARK = "26212B"
BORDER_COLOR = "D9D2E3"
GREEN_LIGHT = "E8F5E9"
GRAY_LIGHT = "F1F1F1"

THIN_SIDE = Side(style="thin", color=BORDER_COLOR)
CELL_BORDER = Border(
    left=THIN_SIDE,
    right=THIN_SIDE,
    top=THIN_SIDE,
    bottom=THIN_SIDE,
)


# =========================
# БАЗОВІ HELPERS
# =========================


def _excel_path() -> Path:
    path = Path(PARTICIPANTS_XLSX_PATH).expanduser()

    if not path.is_absolute():
        path = Path.cwd() / path

    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _safe_text(value: Any, default: str = "") -> str:
    """
    Захист від випадкового Excel formula injection.

    Ім'я/username/телефон можуть починатися з =, +, -, @.
    Excel сприймає такі значення як формули. Апостроф змушує зберегти
    значення як звичайний текст і не відображається користувачу в клітинці.
    """

    if value is None:
        text = default
    else:
        text = str(value).strip()

    if not text:
        text = default

    if text.startswith(("=", "+", "-", "@")):
        return "'" + text

    return text


def _registration_id_from_dict(registration: dict) -> int:
    value = registration.get("id")

    if value is None:
        raise ValueError("registration['id'] is required for Excel export")

    return int(value)


def _to_local_datetime(value: Any) -> datetime.datetime:
    """
    ParticipantRegistration.registration_completed_at зберігається як UTC
    без tzinfo. Для Excel показуємо локальний час кампанії.
    """

    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.datetime.fromisoformat(value.strip())
        except ValueError:
            dt = datetime.datetime.utcnow()
    else:
        dt = datetime.datetime.utcnow()

    try:
        target_tz = ZoneInfo(PARTICIPANTS_TIMEZONE)
    except Exception:
        logging.warning(
            "Invalid PARTICIPANTS_TIMEZONE=%s. Falling back to Europe/Kyiv.",
            PARTICIPANTS_TIMEZONE,
        )
        target_tz = ZoneInfo("Europe/Kyiv")

    # У БД використовується datetime.utcnow() без tzinfo.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    return dt.astimezone(target_tz)


def _load_or_create_workbook(path: Path):
    if path.exists():
        try:
            wb = load_workbook(path)
        except Exception as e:
            raise RuntimeError(
                f"Не вдалося відкрити Excel-файл {path}: {e}"
            ) from e
    else:
        wb = Workbook()
        default_sheet = wb.active
        default_sheet.title = SHEET_NAME

    if SHEET_NAME in wb.sheetnames:
        ws = wb[SHEET_NAME]
    else:
        ws = wb.create_sheet(SHEET_NAME)

    wb.properties.creator = "Soska Bar"
    wb.properties.title = "База учасників Колеса Фортуни"
    wb.properties.subject = "Реєстрації учасників"

    return wb, ws


def _ensure_headers(ws) -> None:
    """
    Створює потрібну структуру, але не видаляє існуючі дані.

    Якщо файл уже існував, технічна колонка L додається непомітно.
    """

    for column_index, header in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=column_index)

        if cell.value is None or str(cell.value).strip() == "":
            cell.value = header

    ws.column_dimensions[TECHNICAL_ID_COLUMN].hidden = True


def _apply_sheet_style(ws) -> None:
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False
    ws.auto_filter.ref = f"A1:{VISIBLE_LAST_COLUMN}{max(1, ws.max_row)}"

    # Шапка.
    for cell in ws[1][: len(HEADERS)]:
        cell.fill = PatternFill("solid", fgColor=PURPLE_DARK)
        cell.font = Font(
            name="Calibri",
            size=11,
            bold=True,
            color=WHITE,
        )
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )
        cell.border = CELL_BORDER

    ws.row_dimensions[1].height = 32

    # Зручні ширини колонок.
    column_widths = {
        "A": 7,
        "B": 16,
        "C": 12,
        "D": 24,
        "E": 19,
        "F": 24,
        "G": 18,
        "H": 42,
        "I": 22,
        "J": 26,
        "K": 30,
        "L": 14,
    }

    for column, width in column_widths.items():
        ws.column_dimensions[column].width = width

    ws.column_dimensions[TECHNICAL_ID_COLUMN].hidden = True

    # Оформлення вже існуючих рядків також приводимо до єдиного вигляду.
    for row in range(2, ws.max_row + 1):
        _style_data_row(ws, row)


def _style_data_row(ws, row: int) -> None:
    ws.row_dimensions[row].height = 24

    # Легка ручна зебра працює навіть у програмах, де Excel Table Style
    # відображається не повністю.
    fill_color = PURPLE_VERY_LIGHT if row % 2 == 0 else WHITE
    base_fill = PatternFill("solid", fgColor=fill_color)

    for column in range(1, 12):
        cell = ws.cell(row=row, column=column)
        cell.font = Font(name="Calibri", size=10, color=TEXT_DARK)
        cell.fill = base_fill
        cell.border = CELL_BORDER
        cell.alignment = Alignment(
            vertical="center",
            wrap_text=column in {4, 6, 8, 10, 11},
        )

    # Центруємо службові/короткі поля.
    for column in (1, 2, 3, 5, 7, 9):
        ws.cell(row=row, column=column).alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=False,
        )

    # Телефон, Telegram ID та file_id завжди текстом.
    for column in (5, 6, 7, 8):
        ws.cell(row=row, column=column).number_format = "@"

    ws.cell(row=row, column=2).number_format = "dd.mm.yyyy"
    ws.cell(row=row, column=3).number_format = "hh:mm"

    # Візуально виділяємо підтверджену підписку.
    subscribed_cell = ws.cell(row=row, column=9)
    if str(subscribed_cell.value or "").strip().lower() in {
        "так",
        "true",
        "1",
        "підтверджено",
    }:
        subscribed_cell.fill = PatternFill("solid", fgColor=GREEN_LIGHT)
        subscribed_cell.font = Font(
            name="Calibri",
            size=10,
            bold=True,
            color=TEXT_DARK,
        )

    # Реєстрації без spin легко відрізнити в таблиці.
    participation_cell = ws.cell(row=row, column=10)
    if str(participation_cell.value or "") == "Реєстрація без спіна":
        participation_cell.fill = PatternFill("solid", fgColor=GRAY_LIGHT)

    # Реальний приз виділяємо жирним.
    prize_cell = ws.cell(row=row, column=11)
    if str(prize_cell.value or "").strip():
        prize_cell.fill = PatternFill("solid", fgColor=PURPLE_LIGHT)
        prize_cell.font = Font(
            name="Calibri",
            size=10,
            bold=True,
            color=PURPLE_DARK,
        )

    # Прихована технічна колонка теж має стабільний числовий формат.
    ws.cell(row=row, column=12).number_format = "0"


def _ensure_table(ws) -> None:
    """
    Excel Table охоплює лише видимі колонки A:K.
    Технічний registration_id у L прихований і не потрапляє в таблицю.
    """

    if ws.max_row < 2:
        return

    table_ref = f"A1:{VISIBLE_LAST_COLUMN}{ws.max_row}"

    if TABLE_NAME in ws.tables:
        ws.tables[TABLE_NAME].ref = table_ref
        return

    table = Table(
        displayName=TABLE_NAME,
        ref=table_ref,
    )
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium4",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    ws.add_table(table)


def _atomic_save(wb, path: Path) -> None:
    """
    Спочатку записуємо тимчасовий .xlsx у тій самій директорії,
    після чого атомарно підміняємо основний файл.
    Це знижує ризик пошкодження participants.xlsx при аварії запису.
    """

    temp_path: str | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".xlsx",
            prefix="participants_",
            dir=str(path.parent),
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name

        wb.save(temp_path)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _find_registration_row(ws, registration_id: int) -> int | None:
    for row in range(2, ws.max_row + 1):
        value = ws.cell(row=row, column=12).value

        try:
            if int(value) == int(registration_id):
                return row
        except (TypeError, ValueError):
            continue

    return None


def _next_sequence_number(ws) -> int:
    current_max = 0

    for row in range(2, ws.max_row + 1):
        value = ws.cell(row=row, column=1).value

        try:
            current_max = max(current_max, int(value))
        except (TypeError, ValueError):
            continue

    return current_max + 1


def _write_registration_row(
    ws,
    *,
    row: int,
    registration: dict,
    sequence_number: int,
) -> None:
    registration_id = _registration_id_from_dict(registration)
    local_dt = _to_local_datetime(
        registration.get("registration_completed_at")
    )

    existing_prize = ws.cell(row=row, column=11).value
    incoming_prize = _safe_text(registration.get("prize"), "")

    # Повторний callback не повинен стерти вже записаний виграш.
    prize_value = incoming_prize or existing_prize or ""

    values = [
        sequence_number,
        local_dt.date(),
        local_dt.time().replace(tzinfo=None, microsecond=0),
        _safe_text(registration.get("name"), "-"),
        _safe_text(registration.get("phone"), "-"),
        _safe_text(registration.get("username"), "user"),
        _safe_text(registration.get("user_id"), "-"),
        _safe_text(registration.get("receipt_file_id"), ""),
        "Так" if bool(registration.get("subscription_confirmed")) else "Ні",
        _safe_text(registration.get("participation_type"), "-"),
        prize_value,
        registration_id,
    ]

    for column, value in enumerate(values, start=1):
        ws.cell(row=row, column=column).value = value

    _style_data_row(ws, row)


def _append_registration_sync(registration: dict) -> Path:
    path = _excel_path()
    registration_id = _registration_id_from_dict(registration)

    wb, ws = _load_or_create_workbook(path)
    _ensure_headers(ws)

    existing_row = _find_registration_row(ws, registration_id)

    if existing_row is None:
        row = max(2, ws.max_row + 1)
        sequence_number = _next_sequence_number(ws)
    else:
        row = existing_row
        try:
            sequence_number = int(ws.cell(row=row, column=1).value)
        except (TypeError, ValueError):
            sequence_number = _next_sequence_number(ws)

    _write_registration_row(
        ws,
        row=row,
        registration=registration,
        sequence_number=sequence_number,
    )

    _apply_sheet_style(ws)
    _ensure_table(ws)
    _atomic_save(wb, path)
    wb.close()

    return path


def _update_registration_prize_sync(
    *,
    registration_id: int,
    prize: str,
) -> bool:
    path = _excel_path()

    if not path.exists():
        logging.warning(
            "Participants Excel does not exist yet; prize update skipped. "
            "registration_id=%s",
            registration_id,
        )
        return False

    wb, ws = _load_or_create_workbook(path)
    _ensure_headers(ws)

    row = _find_registration_row(ws, int(registration_id))

    if row is None:
        wb.close()
        logging.warning(
            "Registration %s was not found in participants Excel; "
            "prize update skipped.",
            registration_id,
        )
        return False

    ws.cell(row=row, column=11).value = _safe_text(prize, "")
    _style_data_row(ws, row)
    _apply_sheet_style(ws)
    _ensure_table(ws)
    _atomic_save(wb, path)
    wb.close()

    return True


# =========================
# ПУБЛІЧНІ ASYNC-ФУНКЦІЇ
# =========================


async def append_registration_to_excel(registration: dict) -> str:
    """
    Додає завершену реєстрацію до participants.xlsx.

    Якщо registration_id уже є у файлі — оновлює існуючий рядок,
    а не створює дублікат.

    Повертає фактичний шлях до Excel-файлу.
    """

    async with EXCEL_LOCK:
        path = await asyncio.to_thread(
            _append_registration_sync,
            registration,
        )

    logging.info(
        "Registration %s exported to Excel: %s",
        registration.get("id"),
        path,
    )
    return str(path)


async def update_registration_prize_in_excel(
    *,
    registration_id: int,
    prize: str,
) -> bool:
    """
    Після реального spin знаходить реєстрацію за прихованим
    registration_id та записує назву отриманого подарунка.
    """

    async with EXCEL_LOCK:
        updated = await asyncio.to_thread(
            _update_registration_prize_sync,
            registration_id=int(registration_id),
            prize=str(prize),
        )

    if updated:
        logging.info(
            "Prize updated in participants Excel: registration_id=%s, prize=%s",
            registration_id,
            prize,
        )

    return updated