from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.auth import require_login
from app.db import get_connection
from app.jobs import recent_jobs
from app.templates_env import templates

router = APIRouter()


@router.get("/suivi")
def suivi(request: Request):
    user = require_login(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plans = conn.execute(
        "SELECT id, name, active FROM plans ORDER BY name"
    ).fetchall()
    conn.close()

    return templates.TemplateResponse(
        "suivi.html", {"request": request, "user": user, "plans": plans, "jobs": recent_jobs()}
    )
