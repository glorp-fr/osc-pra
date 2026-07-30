"""Journal d'audit : connexions/déconnexions et actions de modification
effectuées par les utilisateurs (voir TODO.md, section « Mise en place des
logs »). Consulté depuis la zone admin > Sécurité (app/routers/admin.py) —
réservé aux admins, au même titre que les comptes et les paramètres."""
from fastapi import Request

from app.db import get_connection


def log_event(request: Request, username: str, action: str, detail: str = "") -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO audit_log (username, action, detail, ip_address) VALUES (?, ?, ?, ?)",
        (username, action, detail, request.client.host if request.client else None),
    )
    conn.commit()
    conn.close()


def recent_events(limit: int = 200) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows
