from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.hash import bcrypt
from starlette.middleware.sessions import SessionMiddleware

from app.db import get_connection, init_db
from app.secret import load_or_create_secret

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Osc-PRA")
app.add_middleware(SessionMiddleware, secret_key=load_or_create_secret())
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/")
def index(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "user": request.session["user"]}
    )


@app.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()

    if row is None or not bcrypt.verify(password, row["password_hash"]):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Identifiants invalides"},
            status_code=401,
        )

    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
