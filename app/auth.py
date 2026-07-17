from fastapi import Request
from fastapi.responses import RedirectResponse


def current_user(request: Request) -> dict | None:
    username = request.session.get("user")
    if not username:
        return None
    return {"username": username, "is_admin": bool(request.session.get("is_admin"))}


def require_login(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return user


def require_admin(request: Request):
    user = require_login(request)
    if isinstance(user, RedirectResponse):
        return user
    if not user["is_admin"]:
        return RedirectResponse("/suivi", status_code=303)
    return user
