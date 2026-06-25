from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from database import SessionLocal, Spin
from config import ADMINS

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

        return templates.TemplateResponse(
            "admin.html",
            {
                "request": request,
                "spins": spins,
                "user_id": user_id,
            },
        )

    finally:
        db.close()

