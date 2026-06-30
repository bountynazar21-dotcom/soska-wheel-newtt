from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import SessionLocal, Spin, PrizeStock
from config import ADMINS, PRANK_TEXT, PRIZE_MODE, WIN_CHANCE_PERCENT

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user_id: int | None = None):
    if user_id is None or user_id not in ADMINS:
        return HTMLResponse(
            "<h2 style='color:red'>ACCESS DENIED</h2>"
            "<p>У вас немає прав доступу.</p>",
            status_code=403,
        )

    db = SessionLocal()

    try:
        spins = db.query(Spin).order_by(Spin.id.desc()).all()

        prize_stocks = (
            db.query(PrizeStock)
            .order_by(PrizeStock.sector_index.asc())
            .all()
        )

        total_spins = len(spins)

        real_wins = [
            spin
            for spin in spins
            if spin.prize != "Нічого" and spin.prize != PRANK_TEXT
        ]

        total_wins = len(real_wins)

        return templates.TemplateResponse(
            "admin.html",
            {
                "request": request,
                "spins": spins,
                "prize_stocks": prize_stocks,
                "total_spins": total_spins,
                "total_wins": total_wins,
                "user_id": user_id,
                "prize_mode": PRIZE_MODE,
                "win_chance_percent": WIN_CHANCE_PERCENT,
            },
        )

    finally:
        db.close()

