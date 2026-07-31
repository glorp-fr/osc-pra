import getpass
import json
import subprocess
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from passlib.hash import bcrypt

from app import cron, octl, scheduling
from app.audit import log_event, recent_events
from app.auth import ROLE_LABELS, ROLES, require_admin, require_login, require_operator
from app.crypto import decrypt, encrypt
from app.db import DB_PATH, get_connection
from app.jobs import finish_job, get_job, get_job_logs, jobs_for_plan, last_executions_by_plan, last_job, running_plan_ids
from app.plan_vpcs import (
    create_plan_vpc, delete_plan_vpc, get_plan_vpc, list_plan_vpcs, selected_vms_of,
    update_plan_vpc, vm_image_overrides_of,
)
from app.resource_scan import get_cached_source_counts, scan_and_cache_source
from app.target import count_vpc_resources, delete_target_vpc, resolve_target_credentials, sync_net_peerings, sync_target_network
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
    last_update = last_job("update")

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
            "last_update": last_update,
            "last_update_logs": get_job_logs(last_update["id"]) if last_update else [],
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
    log_event(request, user["username"], "parametres_modifies")

    backup_freq = scheduling.parse_cron(settings["backup_frequency"] if settings else None)
    cron_error = _sync_crontab_or_error()
    last_update = last_job("update")

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
            "last_update": last_update,
            "last_update_logs": get_job_logs(last_update["id"]) if last_update else [],
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
    log_event(request, user["username"], "sauvegarde_lancee_manuellement")
    return RedirectResponse(
        "/admin/parametres?message=Sauvegarde+lanc%C3%A9e+en+arri%C3%A8re-plan", status_code=303
    )


@router.post("/mise-a-jour")
def mise_a_jour_lancer(request: Request):
    """Lance update.sh via une unité systemd transitoire indépendante
    (systemd-run --collect), pas un simple sous-processus de l'app :
    update.sh redémarre le service osc-pra (KillMode=control-group par
    défaut), ce qui tuerait un sous-processus normal avant qu'il ait fini
    de journaliser le résultat — voir scripts/run_update.py.

    Exécutée avec --uid/--gid de l'utilisateur courant (celui du service,
    propriétaire du dépôt), pas en root : un `git diff`/`git checkout`
    lancé en root dans un dépôt appartenant à un autre utilisateur se
    heurte à la protection « dubious ownership » de git (et laisserait de
    toute façon des fichiers appartenant à root dans venv/ après le `pip
    install`). Seul `systemctl restart` (dans update.sh) a besoin de root,
    couvert par le sudo NOPASSWD déjà en place pour cet utilisateur."""
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    service_user = getpass.getuser()
    unit_name = f"osc-pra-update-{int(time.time())}"
    subprocess.Popen([
        "sudo", "systemd-run", f"--unit={unit_name}", "--collect",
        "--description=Mise à jour Osc-PRA", f"--uid={service_user}", f"--gid={service_user}",
        str(cron.APP_DIR / "venv" / "bin" / "python3"), str(cron.APP_DIR / "scripts" / "run_update.py"),
    ])
    log_event(request, user["username"], "mise_a_jour_lancee")
    return RedirectResponse(
        "/admin/parametres?message=Mise+%C3%A0+jour+lanc%C3%A9e+en+arri%C3%A8re-plan+%E2%80%94+le+service+va+red%C3%A9marrer",
        status_code=303,
    )


@router.post("/planification/resync")
def planification_resync(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    error = _sync_crontab_or_error()
    log_event(request, user["username"], "crontab_resynchronise")
    if error:
        return RedirectResponse(f"/admin/parametres?cron_error={quote(error)}", status_code=303)
    return RedirectResponse("/admin/parametres?message=Crontab+resynchronis%C3%A9", status_code=303)


# --- Sécurité (journal d'audit) --------------------------------------------

@router.get("/securite")
def securite(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse(
        "admin/securite.html",
        {"request": request, "user": user, "events": recent_events()},
    )


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
    log_event(request, user["username"], "compte_cree", f"{username} ({ROLE_LABELS.get(role, role)})")
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
    log_event(
        request, user["username"], "compte_role_modifie",
        f"{target['username']} : {ROLE_LABELS.get(target['role'], target['role'])} -> {ROLE_LABELS.get(role, role)}",
    )
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
    log_event(request, user["username"], "compte_mdp_reinitialise", account["username"])
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
    log_event(request, user["username"], "compte_supprime", target["username"])
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


def _plan_vm_status(plan) -> tuple[list[dict], str | None]:
    """Pour chaque VM sélectionnée du plan : nombre de points de sauvegarde
    réussis, date du dernier snapshot, date de la génération de snapshot
    actuellement restaurée sur le volume racine de la VM cible (vm_targets,
    voir app/restore.py._record_root_restore), et statut dérivé de l'état
    actif du plan et du dernier job (Défaillant si le dernier job de cette VM
    est en erreur). Retourne aussi la dernière date à laquelle TOUTES les VMs
    sélectionnées ont un snapshot réussi (dernier point de restauration
    complet du plan). Un plan peut avoir plusieurs VPC (voir
    app/plan_vpcs.py) : les VM de tous les VPC du plan sont fusionnées ici,
    l'ID de VM étant unique sur le compte quel que soit son VPC."""
    selected_vms = [vm_id for pv in list_plan_vpcs(plan["id"]) for vm_id in selected_vms_of(pv)]
    if not selected_vms:
        return [], None

    conn = get_connection()
    jobs = conn.execute(
        "SELECT vm_id, status, started_at FROM jobs "
        "WHERE plan_id = ? AND job_type = 'snapshot' AND vm_id IS NOT NULL "
        "ORDER BY started_at",
        (plan["id"],),
    ).fetchall()
    targets = conn.execute(
        "SELECT source_vm_id, restored_at FROM vm_targets WHERE plan_id = ?", (plan["id"],)
    ).fetchall()
    conn.close()

    by_vm: dict[str, list] = {}
    for job in jobs:
        by_vm.setdefault(job["vm_id"], []).append(job)
    restored_at_by_vm = {t["source_vm_id"]: t["restored_at"] for t in targets}

    rows = []
    success_dates_by_vm = {}
    for vm_id in selected_vms:
        vm_jobs = by_vm.get(vm_id, [])
        success_jobs = [j for j in vm_jobs if j["status"] == "success"]
        last_job = vm_jobs[-1] if vm_jobs else None

        if not plan["active"]:
            status = "desactive"
        elif last_job is not None and last_job["status"] == "error":
            status = "defaillant"
        else:
            status = "actif"

        rows.append({
            "vm_id": vm_id,
            "backup_points": len(success_jobs),
            "last_snapshot": success_jobs[-1]["started_at"] if success_jobs else None,
            "target_restored_at": restored_at_by_vm.get(vm_id),
            "status": status,
        })
        success_dates_by_vm[vm_id] = {j["started_at"][:10] for j in success_jobs}

    common_dates = None
    for dates in success_dates_by_vm.values():
        common_dates = dates if common_dates is None else common_dates & dates
    last_full_date = max(common_dates) if common_dates else None

    return rows, last_full_date


def _resolve_target_sk(target_sk: str, plan_id: str) -> str:
    """Idem _resolve_source_sk, côté cible : utilisé pour scanner les OMI du
    compte cible sans jamais réafficher le SK cible en clair."""
    if target_sk or not plan_id:
        return target_sk

    conn = get_connection()
    plan = conn.execute("SELECT target_sk_encrypted FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan and plan["target_sk_encrypted"]:
        return decrypt(plan["target_sk_encrypted"])
    return target_sk


def _extract_vm_image_overrides(form, selected_vms: list[str]) -> dict:
    """Les OMI choisies par VM sont envoyées via des champs dynamiques
    `vm_image__{VmId}` (un <select> par VM dans _vm_list.html), impossibles à
    déclarer comme paramètres Form(...) classiques puisque leur nombre varie
    selon le scan. Ne garde que celles des VMs effectivement sélectionnées."""
    selected = set(selected_vms)
    prefix = "vm_image__"
    overrides = {}
    for key in form.keys():
        if not key.startswith(prefix):
            continue
        vm_id = key[len(prefix):]
        value = form.get(key)
        if vm_id in selected and value:
            overrides[vm_id] = value
    return overrides


def _extract_vm_restart_order(form, selected_vms: list[str]) -> dict:
    """Même convention que _extract_vm_image_overrides : un champ dynamique
    `vm_restart_order__{VmId}` par VM dans _vm_list.html. Ordre de
    démarrage à la bascule PRA (voir app/failover.py::resolve_start_order),
    plus petit démarré en premier."""
    selected = set(selected_vms)
    prefix = "vm_restart_order__"
    order = {}
    for key in form.keys():
        if not key.startswith(prefix):
            continue
        vm_id = key[len(prefix):]
        value = form.get(key)
        if vm_id in selected and value:
            try:
                order[vm_id] = int(value)
            except ValueError:
                pass
    return order


def _restart_order_for_vpc(plan, plan_vpc) -> dict:
    """Sous-ensemble de plans.vm_restart_order (flat, voir
    app/failover.py::resolve_start_order) restreint aux VM de CE VPC —
    affiché pré-rempli dans le bloc VPC du formulaire du plan."""
    order = json.loads(plan["vm_restart_order"] or "{}")
    return {vm_id: value for vm_id, value in order.items() if vm_id in set(selected_vms_of(plan_vpc))}


def _resolve_source_vpc_id(source_vpc_id: str, plan_vpc_id: str) -> str:
    """Idem _resolve_source_sk : si le VPC source n'a pas été (re)scanné dans
    ce chargement de page, on retombe sur celui déjà enregistré sur ce VPC
    du plan (voir app/plan_vpcs.py)."""
    if source_vpc_id or not plan_vpc_id:
        return source_vpc_id

    plan_vpc = get_plan_vpc(int(plan_vpc_id))
    return plan_vpc["source_vpc_id"] if plan_vpc and plan_vpc["source_vpc_id"] else source_vpc_id


@router.get("/plans")
def plans_list(request: Request, message: str | None = None, error: str | None = None):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plans = conn.execute("SELECT * FROM plans ORDER BY name").fetchall()
    conn.close()

    return templates.TemplateResponse(
        "admin/plans.html",
        {
            "request": request, "user": user, "plans": plans, "message": message, "error": error,
            "running_plan_ids": running_plan_ids(),
            "last_executions": last_executions_by_plan(),
        },
    )


@router.get("/plans/nouveau")
def plan_new_form(request: Request):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse(
        "admin/plan_form.html",
        {
            "request": request, "user": user, "error": None, "plan": None, "plan_vpcs": [],
            "snapshot_freq": scheduling.parse_cron(None),
        },
    )


@router.post("/plans/nouveau")
async def plan_new_submit(
    request: Request,
    name: str = Form(...),
    source_ak: str = Form(""),
    source_sk: str = Form(""),
    source_region: str = Form(""),
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
    auto_sync_target: str = Form(""),
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
                name, source_ak, source_sk_encrypted, source_region,
                target_type, target_region, target_ak, target_sk_encrypted,
                sync_endpoint, sync_bucket, sync_ak,
                sync_sk_encrypted, target_retain_count, snapshot_frequency, source_retain_count,
                auto_sync_target
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name, source_ak, encrypt(source_sk), source_region,
                target_type, target_region, target_ak, encrypt(target_sk),
                sync_endpoint, sync_bucket, sync_ak,
                encrypt(sync_sk), target_retain_count, snapshot_frequency, source_retain_count,
                1 if auto_sync_target else 0,
            ),
        )
        conn.commit()
    except Exception:
        conn.close()
        snapshot_freq = scheduling.parse_cron(snapshot_frequency)
        return templates.TemplateResponse(
            "admin/plan_form.html",
            {
                "request": request, "user": user, "error": "Un plan porte déjà ce nom", "plan": None, "plan_vpcs": [],
                "snapshot_freq": snapshot_freq,
            },
            status_code=400,
        )
    conn.close()
    log_event(request, user["username"], "plan_cree", name)

    error = _sync_crontab_or_error()
    if error:
        return RedirectResponse(f"/admin/plans?error={quote(error)}", status_code=303)
    return RedirectResponse("/admin/plans", status_code=303)


@router.get("/plans/{plan_id}/modifier")
def plan_edit_form(request: Request, plan_id: int):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return RedirectResponse("/admin/plans", status_code=303)

    plan_vpcs = list_plan_vpcs(plan_id)
    return templates.TemplateResponse(
        "admin/plan_form.html",
        {
            "request": request, "user": user, "error": None, "plan": plan,
            "plan_vpcs": plan_vpcs,
            "restart_orders_by_plan_vpc_id": {pv["id"]: _restart_order_for_vpc(plan, pv) for pv in plan_vpcs},
            "snapshot_freq": scheduling.parse_cron(plan["snapshot_frequency"]),
            "plan_running": plan_id in running_plan_ids(),
        },
    )


@router.post("/plans/{plan_id}/modifier")
async def plan_edit_submit(
    request: Request,
    plan_id: int,
    name: str = Form(...),
    source_ak: str = Form(""),
    source_sk: str = Form(""),
    source_region: str = Form(""),
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
    auto_sync_target: str = Form(""),
):
    user = require_operator(request)
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

    try:
        conn.execute(
            """
            UPDATE plans SET
                name = ?, source_ak = ?, source_sk_encrypted = ?, source_region = ?,
                target_type = ?, target_region = ?, target_ak = ?, target_sk_encrypted = ?,
                sync_endpoint = ?, sync_bucket = ?, sync_ak = ?,
                sync_sk_encrypted = ?, target_retain_count = ?, snapshot_frequency = ?, source_retain_count = ?,
                auto_sync_target = ?
            WHERE id = ?
            """,
            (
                name, source_ak, source_sk_encrypted, source_region,
                target_type, target_region, target_ak, target_sk_encrypted,
                sync_endpoint, sync_bucket, sync_ak,
                sync_sk_encrypted, target_retain_count, snapshot_frequency, source_retain_count,
                1 if auto_sync_target else 0,
                plan_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.close()
        snapshot_freq = scheduling.parse_cron(snapshot_frequency)
        edit_plan_vpcs = list_plan_vpcs(plan_id)
        return templates.TemplateResponse(
            "admin/plan_form.html",
            {
                "request": request, "user": user, "error": "Un plan porte déjà ce nom", "plan": existing,
                "plan_vpcs": edit_plan_vpcs,
                "restart_orders_by_plan_vpc_id": {pv["id"]: _restart_order_for_vpc(existing, pv) for pv in edit_plan_vpcs},
                "snapshot_freq": snapshot_freq,
                "plan_running": plan_id in running_plan_ids(),
            },
            status_code=400,
        )
    conn.close()
    log_event(request, user["username"], "plan_modifie", name)

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
    user = require_operator(request)
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
    source_vpc_id: str = Form(""),
    existing_selected_vms: str = Form("[]"),
    existing_vm_image_overrides: str = Form("{}"),
    existing_vm_restart_order: str = Form("{}"),
    target_type: str = Form("meme_region"),
    target_ak: str = Form(""),
    target_sk: str = Form(""),
    target_region: str = Form(""),
    plan_id: str = Form(""),
    plan_vpc_id: str = Form(""),
):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    source_sk = _resolve_source_sk(source_sk, plan_id)
    source_vpc_id = _resolve_source_vpc_id(source_vpc_id, plan_vpc_id)

    try:
        selected_ids = set(json.loads(existing_selected_vms))
    except (ValueError, TypeError):
        selected_ids = set()

    try:
        image_overrides = json.loads(existing_vm_image_overrides)
        if not isinstance(image_overrides, dict):
            image_overrides = {}
    except (ValueError, TypeError):
        image_overrides = {}

    try:
        restart_orders = json.loads(existing_vm_restart_order)
        if not isinstance(restart_orders, dict):
            restart_orders = {}
    except (ValueError, TypeError):
        restart_orders = {}

    if not (source_ak and source_sk and source_region):
        return templates.TemplateResponse(
            "admin/_vm_list.html",
            {"request": request, "vms": None, "error": "AK, SK et région requis pour scanner.", "selected_ids": selected_ids},
        )

    if not source_vpc_id:
        return templates.TemplateResponse(
            "admin/_vm_list.html",
            {"request": request, "vms": None, "error": "Sélectionne d'abord un VPC source (section ci-dessus) avant de scanner les VMs.", "selected_ids": selected_ids},
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_vm_list.html",
            {"request": request, "vms": None, "error": "octl n'est pas installé sur ce serveur.", "selected_ids": selected_ids},
        )

    try:
        vms = [vm for vm in octl.list_vms(source_ak, source_sk, source_region) if vm.get("NetId") == source_vpc_id]
        error = None
    except octl.OctlError as exc:
        vms = None
        error = str(exc)

    # Les OMI proposées viennent du compte cible (là où la VM cible sera
    # réellement créée — voir restore.py), pas du compte source : en
    # "autre région" ce sont deux comptes distincts, et une OMI du compte
    # source n'existe pas forcément côté cible.
    if target_type == "autre_region":
        image_ak, image_sk, image_region = target_ak, _resolve_target_sk(target_sk, plan_id), target_region
    else:
        image_ak, image_sk, image_region = source_ak, source_sk, source_region

    images, images_error = None, None
    if octl.is_available() and image_ak and image_sk and image_region:
        try:
            images = octl.list_images(image_ak, image_sk, image_region)
        except octl.OctlError as exc:
            images_error = str(exc)

    return templates.TemplateResponse(
        "admin/_vm_list.html",
        {
            "request": request, "vms": vms, "error": error, "selected_ids": selected_ids,
            "images": images, "images_error": images_error, "image_overrides": image_overrides,
            "restart_orders": restart_orders,
        },
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
    user = require_operator(request)
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


@router.post("/plans/{plan_id}/vpcs")
def plan_vpc_add(request: Request, plan_id: int):
    """Ajoute un nouveau bloc VPC à un plan (voir CLAUDE.MD, section « Plans
    multi-VPC ») — ligne `plan_vpcs` vide, à compléter (VPC source, AZ cible,
    VPC cible, VMs) depuis le bloc rendu ci-dessous."""
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return RedirectResponse("/admin/plans", status_code=303)

    plan_vpc_id = create_plan_vpc(plan_id)
    log_event(request, user["username"], "plan_vpc_ajoute", f"{plan['name']} : bloc #{plan_vpc_id}")
    return templates.TemplateResponse(
        "admin/_plan_vpc_block.html",
        {"request": request, "plan": plan, "plan_vpc": get_plan_vpc(plan_vpc_id), "existing_vm_restart_order": {}},
    )


@router.post("/plans/{plan_id}/vpcs/{plan_vpc_id}/vpc-source")
def plan_vpc_apply_source(request: Request, plan_id: int, plan_vpc_id: int, source_vpc_id: str = Form("")):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    if not source_vpc_id:
        return templates.TemplateResponse(
            "admin/_vpc_source_status.html",
            {"request": request, "error": "Sélectionne un VPC dans la liste avant d'appliquer.", "vpc_id": None},
        )

    plan_vpc = get_plan_vpc(plan_vpc_id)
    if plan_vpc is None or plan_vpc["plan_id"] != plan_id:
        return templates.TemplateResponse(
            "admin/_vpc_source_status.html",
            {"request": request, "error": "VPC de plan introuvable.", "vpc_id": None},
        )

    update_plan_vpc(plan_vpc_id, source_vpc_id=source_vpc_id)
    return templates.TemplateResponse(
        "admin/_vpc_source_status.html", {"request": request, "error": None, "vpc_id": source_vpc_id}
    )


@router.post("/plans/{plan_id}/vpcs/{plan_vpc_id}/az")
def plan_vpc_set_az(request: Request, plan_id: int, plan_vpc_id: int, target_subregion: str = Form("")):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    plan_vpc = get_plan_vpc(plan_vpc_id)
    if plan_vpc is None or plan_vpc["plan_id"] != plan_id:
        return templates.TemplateResponse(
            "admin/_vpc_az_status.html", {"request": request, "error": "VPC de plan introuvable.", "target_subregion": None}
        )

    update_plan_vpc(plan_vpc_id, target_subregion=target_subregion or None)
    return templates.TemplateResponse(
        "admin/_vpc_az_status.html", {"request": request, "error": None, "target_subregion": target_subregion}
    )


@router.post("/plans/{plan_id}/vpcs/{plan_vpc_id}/vpc-cible")
def plan_vpc_create_target(request: Request, plan_id: int, plan_vpc_id: int, delete_old: str = Form("")):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    plan_vpc = get_plan_vpc(plan_vpc_id)

    if plan is None or plan_vpc is None or plan_vpc["plan_id"] != plan_id or not plan_vpc["source_vpc_id"]:
        conn.close()
        return templates.TemplateResponse(
            "admin/_vpc_target_status.html",
            {"request": request, "error": "Sélectionne un VPC source pour ce bloc avant de créer le VPC cible.", "result": None},
        )

    if not plan_vpc["target_subregion"]:
        conn.close()
        return templates.TemplateResponse(
            "admin/_vpc_target_status.html",
            {"request": request, "error": "AZ cible requise (ci-dessus) avant de créer le VPC cible.", "result": None},
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

    old_vpc_id = plan_vpc["target_vpc_id"]
    old_vpc_deletion = None
    if old_vpc_id:
        # Un VPC cible existe déjà pour ce bloc : on en crée un autre plus
        # bas, donc l'ancien (et le mapping des VM de CE VPC uniquement) sort
        # de la configuration du plan dans tous les cas — supprimé en plus si
        # demandé, sinon simplement laissé orphelin sur le compte cible.
        vm_ids = selected_vms_of(plan_vpc)
        if delete_old:
            old_vpc_deletion = delete_target_vpc(plan, target_ak, target_sk, target_region, old_vpc_id, vm_ids=vm_ids)
        if vm_ids:
            conn.execute(
                f"DELETE FROM vm_targets WHERE plan_id = ? AND source_vm_id IN ({','.join('?' * len(vm_ids))})",
                [plan_id, *vm_ids],
            )
            conn.commit()

    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""
    result, error = None, None
    try:
        source_vpcs = octl.list_vpcs(plan["source_ak"], source_sk, plan["source_region"])
        source_vpc = next((v for v in source_vpcs if v.get("NetId") == plan_vpc["source_vpc_id"]), None)
        if source_vpc is None:
            raise octl.OctlError("VPC source introuvable sur le compte (supprimé ?).")

        name_tag = next(
            (t["Value"] for t in source_vpc.get("Tags", []) if t.get("Key") == "Name"),
            plan["name"],
        )
        target_vpc = octl.create_vpc(
            target_ak, target_sk, target_region, source_vpc["IpRange"], f"{name_tag}-pra",
            tags=source_vpc.get("Tags", []),
        )
        target_vpc_id = target_vpc.get("NetId") if isinstance(target_vpc, dict) else None
        update_plan_vpc(plan_vpc_id, target_vpc_id=target_vpc_id)
        log_event(request, user["username"], "plan_vpc_cible_cree", f"{plan['name']} : {target_vpc_id}")
        result = {
            "vpc_id": target_vpc_id, "ip_range": source_vpc["IpRange"], "sync_summary": None, "sync_error": None,
            "old_vpc_id": old_vpc_id, "old_vpc_deletion": old_vpc_deletion,
        }

        try:
            all_plan_vpcs = list_plan_vpcs(plan_id)
            peering_target_id_by_source_id = sync_net_peerings(plan, all_plan_vpcs, target_ak, target_sk, target_region)
            result["sync_summary"] = sync_target_network(
                plan, target_ak, target_sk, target_region,
                plan_vpc["source_vpc_id"], target_vpc_id, plan_vpc["target_subregion"],
                peering_target_id_by_source_id,
            )
        except octl.OctlError as exc:
            result["sync_error"] = str(exc)
    except octl.OctlError as exc:
        error = str(exc)

    conn.close()
    return templates.TemplateResponse(
        "admin/_vpc_target_status.html", {"request": request, "result": result, "error": error}
    )


@router.post("/plans/{plan_id}/vpcs/{plan_vpc_id}/vms")
async def plan_vpc_save_vms(request: Request, plan_id: int, plan_vpc_id: int, selected_vms: list[str] = Form([])):
    """Enregistre la sélection de VM (et l'ordre de démarrage à la bascule,
    et les OMI cible) pour CE VPC du plan uniquement. `vm_restart_order`
    reste un champ plan-level (flat — voir app/failover.py::resolve_start_order,
    l'ordre s'applique à toutes les VM du plan quel que soit leur VPC) :
    seules les clés des VM de ce bloc sont mises à jour, celles des autres
    VPC du plan sont préservées."""
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    plan_vpc = get_plan_vpc(plan_vpc_id)
    if plan_vpc is None or plan_vpc["plan_id"] != plan_id:
        return RedirectResponse(f"/admin/plans/{plan_id}/modifier", status_code=303)

    form = await request.form()
    vm_image_overrides = _extract_vm_image_overrides(form, selected_vms)
    block_restart_order = _extract_vm_restart_order(form, selected_vms)

    conn = get_connection()
    plan = conn.execute("SELECT vm_restart_order FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    restart_order = json.loads(plan["vm_restart_order"] or "{}") if plan else {}
    for vm_id in selected_vms_of(plan_vpc):
        restart_order.pop(vm_id, None)
    restart_order.update(block_restart_order)

    update_plan_vpc(
        plan_vpc_id,
        selected_vms=json.dumps(selected_vms), vm_image_overrides=json.dumps(vm_image_overrides),
    )
    conn = get_connection()
    conn.execute("UPDATE plans SET vm_restart_order = ? WHERE id = ?", (json.dumps(restart_order), plan_id))
    conn.commit()
    conn.close()
    log_event(request, user["username"], "plan_vpc_vms_enregistrees", f"plan_id={plan_id} plan_vpc_id={plan_vpc_id}")

    return templates.TemplateResponse("admin/_vpc_vms_status.html", {"request": request, "count": len(selected_vms)})


@router.post("/plans/{plan_id}/vpcs/{plan_vpc_id}/supprimer")
def plan_vpc_remove(request: Request, plan_id: int, plan_vpc_id: int):
    """Retire un VPC de la configuration du plan — supprime aussi son VPC
    cible et ses VM cible (best-effort, voir delete_target_vpc), scopé aux
    VM de CE VPC uniquement (les autres VPC du plan ne sont pas affectés)."""
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    plan_vpc = get_plan_vpc(plan_vpc_id)
    if plan is None or plan_vpc is None or plan_vpc["plan_id"] != plan_id:
        return RedirectResponse(f"/admin/plans/{plan_id}/modifier", status_code=303)

    if plan_vpc["target_vpc_id"] and octl.is_available():
        target_ak, target_sk, target_region, error = resolve_target_credentials(plan)
        if not error:
            delete_target_vpc(
                plan, target_ak, target_sk, target_region, plan_vpc["target_vpc_id"],
                vm_ids=selected_vms_of(plan_vpc),
            )

    delete_plan_vpc(plan_vpc_id)
    log_event(request, user["username"], "plan_vpc_supprime", f"{plan['name']} : bloc #{plan_vpc_id}")
    return RedirectResponse(f"/admin/plans/{plan_id}/modifier", status_code=303)


@router.post("/plans/{plan_id}/desactiver")
def plan_disable(request: Request, plan_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT name FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.execute("UPDATE plans SET active = 0 WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()
    if plan:
        log_event(request, user["username"], "plan_desactive", plan["name"])

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
    plan = conn.execute("SELECT name FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.execute("UPDATE plans SET active = 1 WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()
    if plan:
        log_event(request, user["username"], "plan_active", plan["name"])

    error = _sync_crontab_or_error()
    if error:
        return RedirectResponse(f"/admin/plans?error={quote(error)}", status_code=303)
    return RedirectResponse("/admin/plans", status_code=303)


@router.post("/plans/{plan_id}/lancer")
def plan_run_now(request: Request, plan_id: int):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "run_plan.py"), str(plan_id)])
    log_event(request, user["username"], "plan_lance_manuellement", f"plan_id={plan_id}")
    return RedirectResponse(f"/admin/plans/{plan_id}/visualiser", status_code=303)


# --- Bascule PRA (Activation / Test) ---------------------------------------

BASCULE_LABELS = {"activation": "Activation PRA", "test": "Test de PRA"}


@router.get("/plans/{plan_id}/bascule/{mode}")
def plan_bascule_confirm(request: Request, plan_id: int, mode: str):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    if mode not in BASCULE_LABELS:
        return RedirectResponse(f"/admin/plans/{plan_id}/visualiser", status_code=303)

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    vm_target_count = conn.execute(
        "SELECT COUNT(*) AS c FROM vm_targets WHERE plan_id = ?", (plan_id,)
    ).fetchone()["c"]
    conn.close()
    if plan is None:
        return RedirectResponse("/admin/plans", status_code=303)

    return templates.TemplateResponse(
        "admin/bascule_confirm.html",
        {"request": request, "user": user, "plan": plan, "mode": mode, "label": BASCULE_LABELS[mode], "vm_target_count": vm_target_count},
    )


@router.post("/plans/{plan_id}/bascule/terminer-test")
def plan_bascule_end_test(request: Request, plan_id: int):
    # Déclarée avant la route générique /bascule/{mode} ci-dessous : sinon
    # "terminer-test" serait capturé comme une valeur de {mode} (FastAPI
    # matche les routes dans l'ordre de déclaration) et cette route ne
    # serait jamais atteinte.
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "end_test.py"), str(plan_id)])
    log_event(request, user["username"], "bascule_test_termine", f"plan_id={plan_id}")
    return RedirectResponse(f"/admin/plans/{plan_id}/visualiser", status_code=303)


@router.post("/plans/{plan_id}/bascule/{mode}")
def plan_bascule_launch(request: Request, plan_id: int, mode: str):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    if mode not in BASCULE_LABELS:
        return RedirectResponse(f"/admin/plans/{plan_id}/visualiser", status_code=303)

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "run_bascule.py"), str(plan_id), mode])
    log_event(request, user["username"], f"bascule_{mode}_lancee", f"plan_id={plan_id}")
    return RedirectResponse(f"/admin/plans/{plan_id}/visualiser", status_code=303)


@router.get("/jobs/{job_id}/log")
def job_log(request: Request, job_id: int):
    """Journal détaillé d'un job (tail -f via polling htmx pendant qu'il
    tourne) — affiché sur la page Suivi et sur la page d'un plan."""
    user = require_login(request)
    if isinstance(user, RedirectResponse):
        return user

    job = get_job(job_id)
    logs = get_job_logs(job_id) if job else []
    return templates.TemplateResponse("admin/_job_log.html", {"request": request, "job": job, "logs": logs})


@router.post("/jobs/{job_id}/forcer-arret")
def job_force_stop(request: Request, job_id: int):
    """Force le passage en erreur d'un job resté bloqué à 'running' — sans
    attendre le nettoyage automatique au prochain redémarrage du service
    (voir app.jobs.reconcile_orphaned_jobs). Ne tue aucun processus réel :
    si le sous-processus tourne encore, il continuera en tâche de fond,
    mais son résultat ne sera plus reflété par ce job une fois marqué en
    erreur — à réserver aux jobs visiblement morts (bloqués depuis
    longtemps, plan ou sandbox coincé sur « En cours »)."""
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    job = get_job(job_id)
    if job is not None and job["status"] == "running":
        finish_job(job_id, "error", "Arrêt forcé manuellement par un administrateur.")
        conn = get_connection()
        conn.execute(
            "UPDATE sandboxes SET status = 'error', error = 'Arrêt forcé manuellement par un administrateur.' "
            "WHERE job_id = ? AND status IN ('creating', 'deleting')",
            (job_id,),
        )
        conn.commit()
        conn.close()
        log_event(request, user["username"], "job_arret_force", f"job_id={job_id}")

    referer = request.headers.get("referer") or "/suivi"
    return RedirectResponse(referer, status_code=303)


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
    plan = conn.execute("SELECT name FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()
    if plan:
        log_event(request, user["username"], "plan_supprime", plan["name"])

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


# --- Visualisation d'un plan (vue d'ensemble) --------------------------------

TARGET_RESOURCE_SCANS = {
    "subnets": (octl.list_subnets, "NetId"),
    "security-groups": (octl.list_security_groups, "NetId"),
    "route-tables": (octl.list_route_tables, "NetId"),
}


RESOURCE_LABELS = {
    "subnets": "Subnets",
    "security_groups": "Security groups",
    "route_tables": "Route tables",
    "internet_services": "Internet service",
}


def _resource_comparison(plan, plan_vpc) -> dict:
    """Construit le tableau de comparaison VPC source / VPC cible affiché
    sur la page Visualiser, pour UN VPC du plan (un plan peut en avoir
    plusieurs, voir app/plan_vpcs.py) : décompte par type de ressource
    (source depuis le cache rescanné toutes les heures, cible en direct) et
    détection d'un écart entre les deux."""
    cached = get_cached_source_counts(plan_vpc["id"])
    source_error = cached["error"] if cached else None
    source_counts = None
    if cached and cached["error"] is None:
        source_counts = {
            "subnets": cached["subnets_count"],
            "security_groups": cached["security_groups_count"],
            "route_tables": cached["route_tables_count"],
            "internet_services": cached["internet_services_count"],
        }
    scanned_at = cached["scanned_at"] if cached else None

    target_counts = None
    target_error = None
    if not plan_vpc["target_vpc_id"]:
        target_error = "VPC cible non créé."
    elif not octl.is_available():
        target_error = "octl n'est pas installé sur ce serveur."
    else:
        target_ak, target_sk, target_region, error = resolve_target_credentials(plan)
        if error:
            target_error = error
        else:
            try:
                target_counts = count_vpc_resources(target_ak, target_sk, target_region, plan_vpc["target_vpc_id"])
            except octl.OctlError as exc:
                target_error = str(exc)

    rows = []
    mismatch = False
    for key, label in RESOURCE_LABELS.items():
        source_value = source_counts[key] if source_counts else None
        target_value = target_counts[key] if target_counts else None
        row_mismatch = source_value is not None and target_value is not None and source_value != target_value
        if row_mismatch:
            mismatch = True
        rows.append({"label": label, "source": source_value, "target": target_value, "mismatch": row_mismatch})

    return {
        "rows": rows,
        "mismatch": mismatch,
        "source_error": source_error,
        "target_error": target_error,
        "scanned_at": scanned_at,
    }


def _enrich_vm_rows_with_eip(plan, vm_rows: list[dict]) -> None:
    """Ajoute l'IP publique (EIP) actuelle de chaque VM source aux lignes
    déjà calculées par _plan_vm_status, si octl est disponible — dégradation
    silencieuse sinon (colonne EIP affichée comme indisponible). L'EIP n'est
    pas répliquée par la resynchronisation des ressources PRA (voir
    app/target.py) : une VM avec EIP la récupère (même région) ou en reçoit
    une nouvelle (autre région) au moment de la bascule, pas avant."""
    if not octl.is_available() or not (plan["source_ak"] and plan["source_sk_encrypted"] and plan["source_region"]):
        for row in vm_rows:
            row["eip"] = None
        return
    try:
        sk = decrypt(plan["source_sk_encrypted"])
        vms_by_id = {vm.get("VmId"): vm for vm in octl.list_vms(plan["source_ak"], sk, plan["source_region"])}
    except octl.OctlError:
        for row in vm_rows:
            row["eip"] = None
        return
    for row in vm_rows:
        vm = vms_by_id.get(row["vm_id"])
        row["eip"] = vm.get("PublicIp") if vm else None


@router.get("/plans/{plan_id}/visualiser")
def plan_view(request: Request, plan_id: int):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return RedirectResponse("/admin/plans", status_code=303)

    vm_rows, last_full_snapshot = _plan_vm_status(plan)
    _enrich_vm_rows_with_eip(plan, vm_rows)

    plan_jobs = jobs_for_plan(plan_id)
    latest_plan_job = plan_jobs[0] if plan_jobs else None
    vpc_rows = [{"plan_vpc": pv, "resources": _resource_comparison(plan, pv)} for pv in list_plan_vpcs(plan_id)]
    failover_state = json.loads(plan["failover_state"] or "{}")
    # Un plan à plusieurs VPC a une NAT Gateway par VPC (voir
    # scripts/run_bascule.py) : failover_state["nats"] est une liste ;
    # l'ancien format à objet unique ("nat") est aussi lu pour un plan resté
    # en Test PRA/Basculé depuis avant la mise à jour vers le multi-VPC.
    failover_nats = failover_state.get("nats") or ([failover_state["nat"]] if failover_state.get("nat") else [])

    return templates.TemplateResponse(
        "admin/plan_view.html",
        {
            "request": request,
            "user": user,
            "failover_state": failover_state,
            "failover_nats": failover_nats,
            "plan": plan,
            "octl_available": octl.is_available(),
            "vm_rows": vm_rows,
            "last_full_snapshot": last_full_snapshot,
            "vpc_rows": vpc_rows,
            "plan_jobs": plan_jobs,
            "latest_plan_job": latest_plan_job,
            "latest_plan_job_logs": get_job_logs(latest_plan_job["id"]) if latest_plan_job else [],
            "plan_running": any(j["status"] == "running" for j in plan_jobs),
        },
    )


@router.post("/plans/{plan_id}/visualiser/rescan-source")
def plan_view_rescan_source(request: Request, plan_id: int):
    """Rescan manuel des VPC source du plan (le même rescan tourne
    automatiquement toutes les heures pour tous les plans — voir
    app/resource_scan.py)."""
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is not None:
        for plan_vpc in list_plan_vpcs(plan_id):
            scan_and_cache_source(plan, plan_vpc)

    return RedirectResponse(f"/admin/plans/{plan_id}/visualiser", status_code=303)


@router.post("/plans/{plan_id}/visualiser/{plan_vpc_id}/scan/{resource_type}")
def plan_view_scan_target(request: Request, plan_id: int, plan_vpc_id: int, resource_type: str):
    """Scanne une ressource (subnets/security groups/route tables) côté
    compte cible et ne garde que celles qui appartiennent au VPC de PRA
    correspondant à ce VPC du plan."""
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    entry = TARGET_RESOURCE_SCANS.get(resource_type)
    if entry is None:
        return templates.TemplateResponse(
            "admin/_resource_list.html", {"request": request, "items": None, "error": "Type de ressource inconnu."}
        )
    scan_fn, net_id_field = entry

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    plan_vpc = get_plan_vpc(plan_vpc_id)
    if plan is None or plan_vpc is None:
        return templates.TemplateResponse(
            "admin/_resource_list.html", {"request": request, "items": None, "error": "Plan ou VPC introuvable."}
        )

    if not plan_vpc["target_vpc_id"]:
        return templates.TemplateResponse(
            "admin/_resource_list.html",
            {"request": request, "items": None, "error": "VPC cible non créé — crée-le depuis la page Modifier du plan."},
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_resource_list.html",
            {"request": request, "items": None, "error": "octl n'est pas installé sur ce serveur."},
        )

    target_ak, target_sk, target_region, error = resolve_target_credentials(plan)
    if error:
        return templates.TemplateResponse(
            "admin/_resource_list.html", {"request": request, "items": None, "error": error}
        )

    try:
        items = scan_fn(target_ak, target_sk, target_region)
        items = [item for item in items if item.get(net_id_field) == plan_vpc["target_vpc_id"]]
        error = None
    except octl.OctlError as exc:
        items = None
        error = str(exc)

    return templates.TemplateResponse(
        "admin/_resource_list.html", {"request": request, "items": items, "error": error}
    )


@router.post("/plans/{plan_id}/visualiser/resync")
def plan_view_resync(request: Request, plan_id: int):
    """Resynchronise les ressources réseau de tous les VPC de PRA du plan
    depuis leurs VPC source respectifs (voir app.target.sync_target_network),
    puis recrée les Net Peering manquants entre eux (voir
    app.target.sync_net_peerings) — dans cet ordre, pour que les routes vers
    ces peerings puissent être recréées dans la même passe."""
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return templates.TemplateResponse(
            "admin/_resync_result.html", {"request": request, "error": "Plan introuvable.", "summaries": None}
        )

    plan_vpcs = [pv for pv in list_plan_vpcs(plan_id) if pv["source_vpc_id"] and pv["target_vpc_id"] and pv["target_subregion"]]
    if not plan_vpcs:
        return templates.TemplateResponse(
            "admin/_resync_result.html",
            {"request": request, "error": "Aucun VPC de ce plan n'a de VPC source, VPC cible et AZ cible complets.", "summaries": None},
        )

    if not octl.is_available():
        return templates.TemplateResponse(
            "admin/_resync_result.html",
            {"request": request, "error": "octl n'est pas installé sur ce serveur.", "summaries": None},
        )

    target_ak, target_sk, target_region, error = resolve_target_credentials(plan)
    if error:
        return templates.TemplateResponse(
            "admin/_resync_result.html", {"request": request, "error": error, "summaries": None}
        )

    try:
        peering_target_id_by_source_id = sync_net_peerings(plan, plan_vpcs, target_ak, target_sk, target_region)
        summaries = [
            {
                "plan_vpc": pv,
                "summary": sync_target_network(
                    plan, target_ak, target_sk, target_region,
                    pv["source_vpc_id"], pv["target_vpc_id"], pv["target_subregion"],
                    peering_target_id_by_source_id,
                ),
            }
            for pv in plan_vpcs
        ]
        error = None
        log_event(request, user["username"], "plan_resync_manuel", plan["name"])
    except octl.OctlError as exc:
        summaries = None
        error = str(exc)

    return templates.TemplateResponse(
        "admin/_resync_result.html", {"request": request, "summaries": summaries, "error": error}
    )


# --- Sandbox -----------------------------------------------------------------

SANDBOX_STATUS_LABELS = {
    "creating": "Création en cours", "running": "En cours", "stopped": "Arrêté",
    "deleting": "Suppression en cours", "deleted": "Supprimé", "error": "Erreur",
}


@router.post("/plans/{plan_id}/sandbox/nouveau")
def sandbox_create(request: Request, plan_id: int):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plan = conn.execute("SELECT name FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        conn.close()
        return RedirectResponse("/admin/plans", status_code=303)
    cur = conn.execute(
        "INSERT INTO sandboxes (plan_id, created_by) VALUES (?, ?)", (plan_id, user["username"])
    )
    sandbox_id = cur.lastrowid
    conn.commit()
    conn.close()

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "run_sandbox.py"), str(sandbox_id), "create"])
    log_event(request, user["username"], "sandbox_cree", f"plan={plan['name']} sandbox_id={sandbox_id}")
    return RedirectResponse("/admin/sandbox", status_code=303)


@router.get("/sandbox")
def sandbox_list(request: Request):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    sandboxes = conn.execute(
        """
        SELECT sandboxes.*, plans.name AS plan_name FROM sandboxes
        JOIN plans ON plans.id = sandboxes.plan_id
        WHERE sandboxes.status != 'deleted'
        ORDER BY sandboxes.created_at DESC
        """
    ).fetchall()
    vpc_ids_by_sandbox = {}
    for row in conn.execute("SELECT sandbox_id, vpc_id FROM sandbox_vpcs WHERE vpc_id IS NOT NULL").fetchall():
        vpc_ids_by_sandbox.setdefault(row["sandbox_id"], []).append(row["vpc_id"])
    conn.close()

    sandboxes = [{**dict(row), "vpc_ids": vpc_ids_by_sandbox.get(row["id"], [])} for row in sandboxes]

    return templates.TemplateResponse(
        "admin/sandbox_list.html",
        {"request": request, "user": user, "sandboxes": sandboxes, "status_labels": SANDBOX_STATUS_LABELS},
    )


@router.get("/sandbox/{sandbox_id}")
def sandbox_view(request: Request, sandbox_id: int):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    sandbox = conn.execute(
        """
        SELECT sandboxes.*, plans.name AS plan_name FROM sandboxes
        JOIN plans ON plans.id = sandboxes.plan_id
        WHERE sandboxes.id = ?
        """,
        (sandbox_id,),
    ).fetchone()
    vm_targets = conn.execute(
        "SELECT * FROM sandbox_vm_targets WHERE sandbox_id = ?", (sandbox_id,)
    ).fetchall() if sandbox else []
    sandbox_vpcs = conn.execute(
        "SELECT * FROM sandbox_vpcs WHERE sandbox_id = ?", (sandbox_id,)
    ).fetchall() if sandbox else []
    conn.close()
    if sandbox is None:
        return RedirectResponse("/admin/sandbox", status_code=303)

    return templates.TemplateResponse(
        "admin/sandbox_view.html",
        {
            "request": request, "user": user, "sandbox": sandbox, "vm_targets": vm_targets,
            "sandbox_vpcs": sandbox_vpcs,
            "state": json.loads(sandbox["state"] or "{}"), "status_labels": SANDBOX_STATUS_LABELS,
        },
    )


@router.post("/sandbox/{sandbox_id}/demarrer")
def sandbox_start(request: Request, sandbox_id: int):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "run_sandbox.py"), str(sandbox_id), "start"])
    log_event(request, user["username"], "sandbox_demarre", f"sandbox_id={sandbox_id}")
    return RedirectResponse("/admin/sandbox", status_code=303)


@router.post("/sandbox/{sandbox_id}/arreter")
def sandbox_stop(request: Request, sandbox_id: int):
    user = require_operator(request)
    if isinstance(user, RedirectResponse):
        return user

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "run_sandbox.py"), str(sandbox_id), "stop"])
    log_event(request, user["username"], "sandbox_arrete", f"sandbox_id={sandbox_id}")
    return RedirectResponse("/admin/sandbox", status_code=303)


@router.get("/sandbox/{sandbox_id}/supprimer")
def sandbox_delete_confirm(request: Request, sandbox_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    sandbox = conn.execute(
        """
        SELECT sandboxes.*, plans.name AS plan_name FROM sandboxes
        JOIN plans ON plans.id = sandboxes.plan_id
        WHERE sandboxes.id = ?
        """,
        (sandbox_id,),
    ).fetchone()
    conn.close()
    if sandbox is None:
        return RedirectResponse("/admin/sandbox", status_code=303)

    return templates.TemplateResponse(
        "admin/sandbox_confirm_delete.html", {"request": request, "user": user, "sandbox": sandbox}
    )


@router.post("/sandbox/{sandbox_id}/supprimer")
def sandbox_delete(request: Request, sandbox_id: int):
    user = require_admin(request)
    if isinstance(user, RedirectResponse):
        return user

    subprocess.Popen([cron.python_bin(), str(cron.APP_DIR / "scripts" / "run_sandbox.py"), str(sandbox_id), "delete"])
    log_event(request, user["username"], "sandbox_supprime", f"sandbox_id={sandbox_id}")
    return RedirectResponse("/admin/sandbox", status_code=303)
