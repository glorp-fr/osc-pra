import json
import subprocess
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from passlib.hash import bcrypt

from app import cron, octl, scheduling
from app.auth import ROLE_LABELS, ROLES, require_admin
from app.crypto import decrypt, encrypt
from app.db import DB_PATH, get_connection
from app.jobs import last_job
from app.templates_env import templates

router = APIRouter(prefix="/admin")


def _sync_crontab_or_error() -> str | None:
    try:
        cron.sync_crontab()
        return None
    except cron.CronError as exc:
        return str(exc)


@router.get("")
def admin_index(request: Request):
    return RedirectResponse("/admin/parametres", status_code=303)


# --- Paramètres globaux ---------------------------------------------------

@router.get("/parametres")
def parametres_form(request: Request, message: str | None = None, cron_error: str | None = None):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()

    backup_freq = scheduling.parse_cron(settings["backup_frequency"] if settings else None)

    return templates.TemplateResponse(
        "admin/parametres.html",
        {
            "request": request,
            "user": user,
            "settings": settings,
            "saved": False,
            "backup_freq": backup_freq,
            "message": message,
            "cron_error": cron_error,
            "last_backup": last_job("backup"),
            "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else None,
            "cron_lines": cron.build_cron_lines(),
        },
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
    backup_freq_type: str = Form("quotidien"),
    backup_hourly_interval: int = Form(1),
    backup_time: str = Form("02:00"),
    backup_days: list[int] = Form([]),
    backup_retain_count: int = Form(7),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    backup_frequency = scheduling.build_cron(backup_freq_type, backup_hourly_interval, backup_time, backup_days)

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

    backup_freq = scheduling.parse_cron(settings["backup_frequency"] if settings else None)
    cron_error = _sync_crontab_or_error()

    return templates.TemplateResponse(
        "admin/parametres.html",
        {
            "request": request,
            "user": user,
            "settings": settings,
            "saved": True,
            "backup_freq": backup_freq,
            "message": None,
            "cron_error": cron_error,
            "last_backup": last_job("backup"),
            "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else None,
            "cron_lines": cron.build_cron_lines(),
        },
    )


@router.post("/backup/lancer")
def backup_run_now(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "run_backup.py")])
    return RedirectResponse(
        "/admin/parametres?message=Sauvegarde+lanc%C3%A9e+en+arri%C3%A8re-plan", status_code=303
    )


@router.post("/planification/resync")
def planification_resync(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    error = _sync_crontab_or_error()
    if error:
        return RedirectResponse(f"/admin/parametres?cron_error={quote(error)}", status_code=303)
    return RedirectResponse("/admin/parametres?message=Crontab+resynchronis%C3%A9", status_code=303)


# --- Gestion des comptes ---------------------------------------------------

@router.get("/comptes")
def comptes_list(request: Request, error: str | None = None, message: str | None = None):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    accounts = conn.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY username"
    ).fetchall()
    conn.close()

    return templates.TemplateResponse(
        "admin/comptes.html",
        {
            "request": request,
            "user": user,
            "accounts": accounts,
            "error": error,
            "message": message,
            "roles": ROLES,
            "role_labels": ROLE_LABELS,
        },
    )


@router.post("/comptes")
def comptes_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("operateur"),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    if len(password) < 8:
        return RedirectResponse(
            "/admin/comptes?error=Le+mot+de+passe+doit+faire+au+moins+8+caractères",
            status_code=303,
        )

    if role not in ROLES:
        role = "operateur"

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, bcrypt.hash(password), role),
        )
        conn.commit()
    except Exception:
        conn.close()
        return RedirectResponse(
            "/admin/comptes?error=Ce+nom+d%27utilisateur+existe+déjà", status_code=303
        )
    conn.close()
    return RedirectResponse("/admin/comptes", status_code=303)


@router.post("/comptes/{account_id}/role")
def comptes_update_role(request: Request, account_id: int, role: str = Form(...)):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    if role not in ROLES:
        return RedirectResponse("/admin/comptes?error=Rôle+invalide", status_code=303)

    conn = get_connection()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (account_id,)).fetchone()
    if target is None:
        conn.close()
        return RedirectResponse("/admin/comptes", status_code=303)

    if target["role"] == "admin" and role != "admin":
        remaining_admins = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND id != ?", (account_id,)
        ).fetchone()["c"]
        if remaining_admins == 0:
            conn.close()
            return RedirectResponse(
                "/admin/comptes?error=Impossible+de+retirer+le+dernier+compte+admin", status_code=303
            )

    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, account_id))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/comptes", status_code=303)


@router.get("/comptes/{account_id}/reinitialiser")
def compte_reset_password_form(request: Request, account_id: int, error: str | None = None):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    account = conn.execute("SELECT * FROM users WHERE id = ?", (account_id,)).fetchone()
    conn.close()
    if account is None:
        return RedirectResponse("/admin/comptes", status_code=303)

    return templates.TemplateResponse(
        "admin/compte_reset_password.html",
        {"request": request, "user": user, "account": account, "error": error},
    )


@router.post("/comptes/{account_id}/reinitialiser")
def compte_reset_password_submit(
    request: Request,
    account_id: int,
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    if password != password_confirm:
        return RedirectResponse(
            f"/admin/comptes/{account_id}/reinitialiser?error=Les+mots+de+passe+ne+correspondent+pas",
            status_code=303,
        )
    if len(password) < 8:
        return RedirectResponse(
            f"/admin/comptes/{account_id}/reinitialiser?error=Le+mot+de+passe+doit+faire+au+moins+8+caractères",
            status_code=303,
        )

    conn = get_connection()
    account = conn.execute("SELECT * FROM users WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        conn.close()
        return RedirectResponse("/admin/comptes", status_code=303)

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (bcrypt.hash(password), account_id))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/comptes?message=Mot+de+passe+réinitialisé", status_code=303)


@router.get("/comptes/{account_id}/supprimer")
def compte_delete_confirm(request: Request, account_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    account = conn.execute("SELECT * FROM users WHERE id = ?", (account_id,)).fetchone()
    conn.close()
    if account is None:
        return RedirectResponse("/admin/comptes", status_code=303)

    return templates.TemplateResponse(
        "admin/compte_delete_confirm.html",
        {"request": request, "user": user, "account": account},
    )


@router.post("/comptes/{account_id}/supprimer")
def compte_delete(request: Request, account_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (account_id,)).fetchone()
    if target is None:
        conn.close()
        return RedirectResponse("/admin/comptes", status_code=303)

    if target["username"] == user["username"]:
        conn.close()
        return RedirectResponse(
            "/admin/comptes?error=Impossible+de+supprimer+votre+propre+compte", status_code=303
        )

    if target["role"] == "admin":
        remaining_admins = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND id != ?", (account_id,)
        ).fetchone()["c"]
        if remaining_admins == 0:
            conn.close()
            return RedirectResponse(
                "/admin/comptes?error=Impossible+de+supprimer+le+dernier+compte+admin", status_code=303
            )

    conn.execute("DELETE FROM users WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/comptes", status_code=303)


# --- Plans de reprise -------------------------------------------------------

def _resolve_source_sk(source_sk: str, plan_id: str) -> str:
    """Le champ SK n'est jamais réaffiché en clair : s'il est laissé vide sur
    un plan existant, on retombe sur le SK déjà enregistré (déchiffré)."""
    if source_sk or not plan_id:
        return source_sk

    conn = get_connection()
    plan = conn.execute("SELECT source_sk_encrypted FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan and plan["source_sk_encrypted"]:
        return decrypt(plan["source_sk_encrypted"])
    return source_sk


@router.get("/plans")
def plans_list(request: Request, message: str | None = None, error: str | None = None):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plans = conn.execute("SELECT * FROM plans ORDER BY name").fetchall()
    conn.close()

    return templates.TemplateResponse(
        "admin/plans.html",
        {"request": request, "user": user, "plans": plans, "message": message, "error": error},
    )


@router.get("/plans/nouveau")
def plan_new_form(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse(
        "admin/plan_form.html",
        {
            "request": request, "user": user, "error": None, "plan": None,
            "snapshot_freq": scheduling.parse_cron(None), "existing_vm_ids": [],
        },
    )


@router.post("/plans/nouveau")
def plan_new_submit(
    request: Request,
    name: str = Form(...),
    source_ak: str = Form(""),
    source_sk: str = Form(""),
    source_region: str = Form(""),
    selected_vms: list[str] = Form([]),
    source_vpc_id: str = Form(""),
    target_type: str = Form("meme_region"),
    target_region: str = Form(""),
    target_ak: str = Form(""),
    target_sk: str = Form(""),
    sync_endpoint: str = Form(""),
    sync_bucket: str = Form(""),
    sync_ak: str = Form(""),
    sync_sk: str = Form(""),
    target_retain_count: int = Form(7),
    snapshot_freq_type: str = Form("quotidien"),
    snapshot_hourly_interval: int = Form(1),
    snapshot_time: str = Form("02:00"),
    snapshot_days: list[int] = Form([]),
    source_retain_count: int = Form(7),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    snapshot_frequency = scheduling.build_cron(snapshot_freq_type, snapshot_hourly_interval, snapshot_time, snapshot_days)

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO plans (
                name, source_ak, source_sk_encrypted, source_region, selected_vms, source_vpc_id,
                target_type, target_region, target_ak, target_sk_encrypted, sync_endpoint, sync_bucket, sync_ak,
                sync_sk_encrypted, target_retain_count, snapshot_frequency, source_retain_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name, source_ak, encrypt(source_sk), source_region, json.dumps(selected_vms), source_vpc_id,
                target_type, target_region, target_ak, encrypt(target_sk), sync_endpoint, sync_bucket, sync_ak,
                encrypt(sync_sk), target_retain_count, snapshot_frequency, source_retain_count,
            ),
        )
        conn.commit()
    except Exception:
        conn.close()
        snapshot_freq = scheduling.parse_cron(snapshot_frequency)
        return templates.TemplateResponse(
            "admin/plan_form.html",
            {
                "request": request, "user": user, "error": "Un plan porte déjà ce nom", "plan": None,
                "snapshot_freq": snapshot_freq, "existing_vm_ids": selected_vms,
            },
            status_code=400,
        )
    conn.close()

    error = _sync_crontab_or_error()
    if error:
        return RedirectResponse(f"/admin/plans?error={quote(error)}", status_code=303)
    return RedirectResponse("/admin/plans", status_code=303)


@router.get("/plans/{plan_id}/modifier")
def plan_edit_form(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return RedirectResponse("/admin/plans", status_code=303)

    return templates.TemplateResponse(
        "admin/plan_form.html",
        {
            "request": request, "user": user, "error": None, "plan": plan,
            "snapshot_freq": scheduling.parse_cron(plan["snapshot_frequency"]),
            "existing_vm_ids": json.loads(plan["selected_vms"] or "[]"),
        },
    )


@router.post("/plans/{plan_id}/modifier")
def plan_edit_submit(
    request: Request,
    plan_id: int,
    name: str = Form(...),
    source_ak: str = Form(""),
    source_sk: str = Form(""),
    source_region: str = Form(""),
    selected_vms: list[str] = Form([]),
    source_vpc_id: str = Form(""),
    target_type: str = Form("meme_region"),
    target_region: str = Form(""),
    target_ak: str = Form(""),
    target_sk: str = Form(""),
    sync_endpoint: str = Form(""),
    sync_bucket: str = Form(""),
    sync_ak: str = Form(""),
    sync_sk: str = Form(""),
    target_retain_count: int = Form(7),
    snapshot_freq_type: str = Form("quotidien"),
    snapshot_hourly_interval: int = Form(1),
    snapshot_time: str = Form("02:00"),
    snapshot_days: list[int] = Form([]),
    source_retain_count: int = Form(7),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    existing = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if existing is None:
        conn.close()
        return RedirectResponse("/admin/plans", status_code=303)

    snapshot_frequency = scheduling.build_cron(snapshot_freq_type, snapshot_hourly_interval, snapshot_time, snapshot_days)
    source_sk_encrypted = encrypt(source_sk) if source_sk else existing["source_sk_encrypted"]
    sync_sk_encrypted = encrypt(sync_sk) if sync_sk else existing["sync_sk_encrypted"]
    target_sk_encrypted = encrypt(target_sk) if target_sk else existing["target_sk_encrypted"]
    source_vpc_id = source_vpc_id or existing["source_vpc_id"]

    try:
        conn.execute(
            """
            UPDATE plans SET
                name = ?, source_ak = ?, source_sk_encrypted = ?, source_region = ?, selected_vms = ?,
                source_vpc_id = ?, target_type = ?, target_region = ?, target_ak = ?, target_sk_encrypted = ?,
                sync_endpoint = ?, sync_bucket = ?, sync_ak = ?,
                sync_sk_encrypted = ?, target_retain_count = ?, snapshot_frequency = ?, source_retain_count = ?
            WHERE id = ?
            """,
            (
                name, source_ak, source_sk_encrypted, source_region, json.dumps(selected_vms),
                source_vpc_id, target_type, target_region, target_ak, target_sk_encrypted,
                sync_endpoint, sync_bucket, sync_ak,
                sync_sk_encrypted, target_retain_count, snapshot_frequency, source_retain_count,
                plan_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.close()
        snapshot_freq = scheduling.parse_cron(snapshot_frequency)
        return templates.TemplateResponse(
            "admin/plan_form.html",
            {
                "request": request, "user": user, "error": "Un plan porte déjà ce nom", "plan": existing,
                "snapshot_freq": snapshot_freq, "existing_vm_ids": selected_vms,
            },
            status_code=400,
        )
    conn.close()

    error = _sync_crontab_or_error()
    if error:
        return RedirectResponse(f"/admin/plans?error={quote(error)}", status_code=303)
    return RedirectResponse("/admin/plans", status_code=303)


@router.post("/plans/octl/test-ak")
def plan_test_ak(
    request: Request,
    source_ak: str = Form(""),
    source_sk: str = Form(""),
    source_region: str = Form(""),
    plan_id: str = Form(""),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    source_sk = _resolve_source_sk(source_sk, plan_id)

    if not (source_ak and source_sk and source_region):
        return templates.TemplateResponse(
            "admin/_ak_status.html",
            {"request": request, "result": None, "message": "AK, SK et région requis pour tester."},
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_ak_status.html",
            {"request": request, "result": None, "message": "octl n'est pas installé sur ce serveur."},
        )

    result = octl.check_access_key(source_ak, source_sk, source_region)
    return templates.TemplateResponse(
        "admin/_ak_status.html", {"request": request, "result": result, "message": None}
    )


@router.post("/plans/octl/scan-vms")
def plan_scan_vms(
    request: Request,
    source_ak: str = Form(""),
    source_sk: str = Form(""),
    source_region: str = Form(""),
    existing_selected_vms: str = Form("[]"),
    plan_id: str = Form(""),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    source_sk = _resolve_source_sk(source_sk, plan_id)

    try:
        selected_ids = set(json.loads(existing_selected_vms))
    except (ValueError, TypeError):
        selected_ids = set()

    if not (source_ak and source_sk and source_region):
        return templates.TemplateResponse(
            "admin/_vm_list.html",
            {"request": request, "vms": None, "error": "AK, SK et région requis pour scanner.", "selected_ids": selected_ids},
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_vm_list.html",
            {"request": request, "vms": None, "error": "octl n'est pas installé sur ce serveur.", "selected_ids": selected_ids},
        )

    try:
        vms = octl.list_vms(source_ak, source_sk, source_region)
        error = None
    except octl.OctlError as exc:
        vms = None
        error = str(exc)

    return templates.TemplateResponse(
        "admin/_vm_list.html", {"request": request, "vms": vms, "error": error, "selected_ids": selected_ids}
    )


@router.post("/plans/octl/scan-vpcs")
def plan_scan_vpcs(
    request: Request,
    source_ak: str = Form(""),
    source_sk: str = Form(""),
    source_region: str = Form(""),
    source_vpc_id: str = Form(""),
    plan_id: str = Form(""),
):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    source_sk = _resolve_source_sk(source_sk, plan_id)

    if not (source_ak and source_sk and source_region):
        return templates.TemplateResponse(
            "admin/_vpc_list.html",
            {"request": request, "vpcs": None, "error": "AK, SK et région requis pour scanner.", "selected_vpc_id": source_vpc_id},
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_vpc_list.html",
            {"request": request, "vpcs": None, "error": "octl n'est pas installé sur ce serveur.", "selected_vpc_id": source_vpc_id},
        )

    try:
        vpcs = octl.list_vpcs(source_ak, source_sk, source_region)
        error = None
    except octl.OctlError as exc:
        vpcs = None
        error = str(exc)

    return templates.TemplateResponse(
        "admin/_vpc_list.html", {"request": request, "vpcs": vpcs, "error": error, "selected_vpc_id": source_vpc_id}
    )


@router.post("/plans/{plan_id}/vpc-cible")
def plan_create_target_vpc(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()

    if plan is None or not plan["source_vpc_id"]:
        conn.close()
        return templates.TemplateResponse(
            "admin/_vpc_target_status.html",
            {"request": request, "error": "Sélectionne un VPC source et enregistre le plan avant de créer le VPC cible.", "result": None},
        )

    if plan["target_type"] == "autre_region":
        target_ak = plan["target_ak"]
        target_sk = decrypt(plan["target_sk_encrypted"]) if plan["target_sk_encrypted"] else ""
        target_region = plan["target_region"]
        if not (target_ak and target_sk and target_region):
            conn.close()
            return templates.TemplateResponse(
                "admin/_vpc_target_status.html",
                {"request": request, "error": "AK/SK et région du compte cible requis pour créer le VPC cible.", "result": None},
            )
    else:
        target_ak = plan["source_ak"]
        target_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""
        target_region = plan["source_region"]

    if not octl.is_available():
        conn.close()
        return templates.TemplateResponse(
            "admin/_vpc_target_status.html",
            {"request": request, "error": "octl n'est pas installé sur ce serveur.", "result": None},
        )

    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""
    result, error = None, None
    try:
        source_vpcs = octl.list_vpcs(plan["source_ak"], source_sk, plan["source_region"])
        source_vpc = next((v for v in source_vpcs if v.get("NetId") == plan["source_vpc_id"]), None)
        if source_vpc is None:
            raise octl.OctlError("VPC source introuvable sur le compte (supprimé ?).")

        name_tag = next(
            (t["Value"] for t in source_vpc.get("Tags", []) if t.get("Key") == "Name"),
            plan["name"],
        )
        target_vpc = octl.create_vpc(target_ak, target_sk, target_region, source_vpc["IpRange"], f"{name_tag}-pra")
        target_vpc_id = target_vpc.get("NetId") if isinstance(target_vpc, dict) else None
        conn.execute("UPDATE plans SET target_vpc_id = ? WHERE id = ?", (target_vpc_id, plan_id))
        conn.commit()
        result = {"vpc_id": target_vpc_id, "ip_range": source_vpc["IpRange"]}
    except octl.OctlError as exc:
        error = str(exc)

    conn.close()
    return templates.TemplateResponse(
        "admin/_vpc_target_status.html", {"request": request, "result": result, "error": error}
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

    error = _sync_crontab_or_error()
    if error:
        return RedirectResponse(f"/admin/plans?error={quote(error)}", status_code=303)
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

    error = _sync_crontab_or_error()
    if error:
        return RedirectResponse(f"/admin/plans?error={quote(error)}", status_code=303)
    return RedirectResponse("/admin/plans", status_code=303)


@router.post("/plans/{plan_id}/lancer")
def plan_run_now(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "run_plan.py"), str(plan_id)])
    return RedirectResponse(
        "/admin/plans?message=Job+de+snapshot+lanc%C3%A9+en+arri%C3%A8re-plan", status_code=303
    )


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

    error = _sync_crontab_or_error()
    if error:
        return RedirectResponse(f"/admin/plans?error={quote(error)}", status_code=303)
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
