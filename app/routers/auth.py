from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from passlib.hash import bcrypt

from app.audit import log_event
from app.db import get_connection
from app.templates_env import templates

router = APIRouter()


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if row is None or not bcrypt.verify(password, row["password_hash"]):
        log_event(request, username, "login_failed")
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Identifiants invalides"},
            status_code=401,
        )

    request.session["user"] = username
    request.session["role"] = row["role"]
    log_event(request, username, "login")
    return RedirectResponse("/suivi", status_code=303)


@router.post("/logout")
def logout(request: Request):
    username = request.session.get("user")
    if username:
        log_event(request, username, "logout")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
