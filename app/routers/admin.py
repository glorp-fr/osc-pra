import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from passlib.hash import bcrypt

from app import octl
from app.auth import require_admin
from app.crypto import decrypt, encrypt
from app.db import get_connection
from app.templates_env import templates

router = APIRouter(prefix="/admin")


@router.get("")
def admin_index(request: Request):
    return RedirectResponse("/admin/parametres", status_code=303)


# --- Paramètres globaux ---------------------------------------------------

@router.get("/parametres")
def parametres_form(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()

    return templates.TemplateResponse(
        "admin/parametres.html",
        {"request": request, "user": user, "settings": settings, "saved": False},
    )


@router.post("/parametres")
def parametres_submit(
    request: Request,
    mail_from: str = Form(""),
    mail_to: str = Form(""),
    smtp_server: str = Form(""),
    storage_mode: str = Form("local"),
    s3_endpoint: str = Form(""),
    s3_bucket: str = Form(""),
    s3_ak: str = Form(""),
    s3_sk: str = Form(""),
    backup_bucket_mode: str = Form("reuse"),
    backup_endpoint: str = Form(""),
    backup_bucket: str = Form(""),
    backup_ak: str = Form(""),
    backup_sk: str = Form(""),
    backup_frequency: str = Form(""),
    backup_retain_count: int = Form(7),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    existing = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()

    s3_sk_encrypted = encrypt(s3_sk) if s3_sk else (existing["s3_sk_encrypted"] if existing else "")
    backup_sk_encrypted = (
        encrypt(backup_sk) if backup_sk else (existing["backup_sk_encrypted"] if existing else "")
    )

    conn.execute(
        """
        INSERT INTO settings (
            id, mail_from, mail_to, smtp_server, storage_mode, s3_endpoint, s3_bucket,
            s3_ak, s3_sk_encrypted, backup_bucket_mode, backup_endpoint, backup_bucket,
            backup_ak, backup_sk_encrypted, backup_frequency, backup_retain_count
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            mail_from=excluded.mail_from, mail_to=excluded.mail_to,
            smtp_server=excluded.smtp_server, storage_mode=excluded.storage_mode,
            s3_endpoint=excluded.s3_endpoint, s3_bucket=excluded.s3_bucket,
            s3_ak=excluded.s3_ak, s3_sk_encrypted=excluded.s3_sk_encrypted,
            backup_bucket_mode=excluded.backup_bucket_mode, backup_endpoint=excluded.backup_endpoint,
            backup_bucket=excluded.backup_bucket, backup_ak=excluded.backup_ak,
            backup_sk_encrypted=excluded.backup_sk_encrypted, backup_frequency=excluded.backup_frequency,
            backup_retain_count=excluded.backup_retain_count
        """,
        (
            mail_from, mail_to, smtp_server, storage_mode, s3_endpoint, s3_bucket,
            s3_ak, s3_sk_encrypted, backup_bucket_mode, backup_endpoint, backup_bucket,
            backup_ak, backup_sk_encrypted, backup_frequency, backup_retain_count,
        ),
    )
    conn.commit()
    settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()

    return templates.TemplateResponse(
        "admin/parametres.html",
        {"request": request, "user": user, "settings": settings, "saved": True},
    )


# --- Gestion des comptes ---------------------------------------------------

@router.get("/comptes")
def comptes_list(request: Request, error: str | None = None):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    accounts = conn.execute(
        "SELECT id, username, is_admin, created_at FROM users ORDER BY username"
    ).fetchall()
    conn.close()

    return templates.TemplateResponse(
        "admin/comptes.html",
        {"request": request, "user": user, "accounts": accounts, "error": error},
    )


@router.post("/comptes")
def comptes_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    if len(password) < 8:
        return RedirectResponse(
            "/admin/comptes?error=Le+mot+de+passe+doit+faire+au+moins+8+caractères",
            status_code=303,
        )

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
            (username, bcrypt.hash(password), int(is_admin)),
        )
        conn.commit()
    except Exception:
        conn.close()
        return RedirectResponse(
            "/admin/comptes?error=Ce+nom+d%27utilisateur+existe+déjà", status_code=303
        )
    conn.close()
    return RedirectResponse("/admin/comptes", status_code=303)


# --- Plans de reprise -------------------------------------------------------

@router.get("/plans")
def plans_list(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plans = conn.execute("SELECT * FROM plans ORDER BY name").fetchall()
    conn.close()

    return templates.TemplateResponse(
        "admin/plans.html", {"request": request, "user": user, "plans": plans}
    )


@router.get("/plans/nouveau")
def plan_new_form(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse(
        "admin/plan_form.html", {"request": request, "user": user, "error": None}
    )


@router.post("/plans/nouveau")
def plan_new_submit(
    request: Request,
    name: str = Form(...),
    source_ak: str = Form(""),
    source_sk: str = Form(""),
    source_region: str = Form(""),
    selected_vms: list[str] = Form([]),
    target_type: str = Form("meme_region"),
    target_region: str = Form(""),
    sync_endpoint: str = Form(""),
    sync_bucket: str = Form(""),
    sync_ak: str = Form(""),
    sync_sk: str = Form(""),
    target_retain_count: int = Form(7),
    snapshot_frequency: str = Form(""),
    source_retain_count: int = Form(7),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO plans (
                name, source_ak, source_sk_encrypted, source_region, selected_vms,
                target_type, target_region, sync_endpoint, sync_bucket, sync_ak,
                sync_sk_encrypted, target_retain_count, snapshot_frequency, source_retain_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name, source_ak, encrypt(source_sk), source_region, json.dumps(selected_vms),
                target_type, target_region, sync_endpoint, sync_bucket, sync_ak,
                encrypt(sync_sk), target_retain_count, snapshot_frequency, source_retain_count,
            ),
        )
        conn.commit()
    except Exception:
        conn.close()
        return templates.TemplateResponse(
            "admin/plan_form.html",
            {"request": request, "user": user, "error": "Un plan porte déjà ce nom"},
            status_code=400,
        )
    conn.close()
    return RedirectResponse("/admin/plans", status_code=303)


@router.post("/plans/octl/test-ak")
def plan_test_ak(request: Request, ak: str = Form(""), sk: str = Form(""), region: str = Form("")):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    if not (ak and sk and region):
        return templates.TemplateResponse(
            "admin/_ak_status.html",
            {"request": request, "result": None, "message": "AK, SK et région requis pour tester."},
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_ak_status.html",
            {"request": request, "result": None, "message": "octl n'est pas installé sur ce serveur."},
        )

    result = octl.check_access_key(ak, sk, region)
    return templates.TemplateResponse(
        "admin/_ak_status.html", {"request": request, "result": result, "message": None}
    )


@router.post("/plans/octl/scan-vms")
def plan_scan_vms(request: Request, ak: str = Form(""), sk: str = Form(""), region: str = Form("")):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    if not (ak and sk and region):
        return templates.TemplateResponse(
            "admin/_vm_list.html",
            {"request": request, "vms": None, "error": "AK, SK et région requis pour scanner."},
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_vm_list.html", {"request": request, "vms": None, "error": "octl n'est pas installé sur ce serveur."}
        )

    try:
        vms = octl.list_vms(ak, sk, region)
        error = None
    except octl.OctlError as exc:
        vms = None
        error = str(exc)

    return templates.TemplateResponse(
        "admin/_vm_list.html", {"request": request, "vms": vms, "error": error}
    )


@router.post("/plans/{plan_id}/desactiver")
def plan_disable(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    conn.execute("UPDATE plans SET active = 0 WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/plans", status_code=303)


@router.post("/plans/{plan_id}/activer")
def plan_enable(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    conn.execute("UPDATE plans SET active = 1 WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/plans", status_code=303)


@router.get("/plans/{plan_id}/supprimer")
def plan_delete_confirm(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return RedirectResponse("/admin/plans", status_code=303)

    return templates.TemplateResponse(
        "admin/plan_delete_confirm.html", {"request": request, "user": user, "plan": plan}
    )


@router.post("/plans/{plan_id}/supprimer")
def plan_delete(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/plans", status_code=303)


# --- Ressources (scan via octl) --------------------------------------------

RESOURCE_SCANS = {
    "subnets": octl.list_subnets,
    "security-groups": octl.list_security_groups,
    "route-tables": octl.list_route_tables,
}


@router.get("/plans/{plan_id}/ressources")
def plan_resources(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return RedirectResponse("/admin/plans", status_code=303)

    return templates.TemplateResponse(
        "admin/plan_ressources.html",
        {"request": request, "user": user, "plan": plan, "octl_available": octl.is_available()},
    )


@router.post("/plans/{plan_id}/ressources/scan/{resource_type}")
def plan_resources_scan(request: Request, plan_id: int, resource_type: str):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    scan_fn = RESOURCE_SCANS.get(resource_type)
    if scan_fn is None:
        return templates.TemplateResponse(
            "admin/_resource_list.html", {"request": request, "items": None, "error": "Type de ressource inconnu."}
        )

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return templates.TemplateResponse(
            "admin/_resource_list.html", {"request": request, "items": None, "error": "Plan introuvable."}
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_resource_list.html",
            {"request": request, "items": None, "error": "octl n'est pas installé sur ce serveur."},
        )

    if not (plan["source_ak"] and plan["source_sk_encrypted"] and plan["source_region"]):
        return templates.TemplateResponse(
            "admin/_resource_list.html",
            {"request": request, "items": None, "error": "AK/SK/région source non configurés pour ce plan."},
        )

    try:
        items = scan_fn(plan["source_ak"], decrypt(plan["source_sk_encrypted"]), plan["source_region"])
        error = None
    except octl.OctlError as exc:
        items = None
        error = str(exc)

    return templates.TemplateResponse(
        "admin/_resource_list.html", {"request": request, "items": items, "error": error}
    )
