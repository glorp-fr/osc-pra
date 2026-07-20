from fastapi import Request
from fastapi.responses import RedirectResponse

ROLES = ("admin", "operateur", "readonly")
ROLE_LABELS = {"admin": "Admin", "operateur": "Opérateur", "readonly": "Lecture seule"}


def current_user(request: Request) -> dict | None:
    username = request.session.get("user")
    if not username:
        return None
    return {"username": username, "role": request.session.get("role", "operateur")}


def require_login(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return user


def require_admin(request: Request):
    user = require_login(request)
    if isinstance(user, RedirectResponse):
        return user
    if user["role"] != "admin":
        return RedirectResponse("/suivi", status_code=303)
    return user
