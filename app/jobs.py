"""Suivi des jobs (snapshots planifiés, sauvegardes) exécutés par les scripts
cron (voir scripts/run_plan.py, scripts/run_backup.py et app/cron.py).

Chaque job accumule aussi un journal détaillé de son déroulé (job_logs) —
une ligne par action significative (snapshot d'un volume, création/mise à
jour de la VM cible, attachement d'un volume...), en plus du message final
résumé sur `jobs.message`. Affiché en direct (tail -f, via polling htmx)
sur la page Suivi et sur la page d'un plan — voir log_step/get_job_logs et
app/routers/admin.py (endpoint /admin/jobs/{id}/log)."""
from app.db import get_connection


def start_job(job_type: str, plan_id: int | None = None, plan_name: str | None = None, vm_id: str | None = None) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO jobs (job_type, plan_id, plan_name, vm_id, status) VALUES (?, ?, ?, ?, 'running')",
        (job_type, plan_id, plan_name, vm_id),
    )
    job_id = cur.lastrowid
    label = f"VM {vm_id}" if vm_id else (plan_name or job_type)
    conn.execute(
        "INSERT INTO job_logs (job_id, level, message) VALUES (?, 'info', ?)",
        (job_id, f"Démarrage — {label}."),
    )
    conn.commit()
    conn.close()
    return job_id


def finish_job(job_id: int, status: str, message: str = "") -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET status = ?, message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, message, job_id),
    )
    conn.execute(
        "INSERT INTO job_logs (job_id, level, message) VALUES (?, ?, ?)",
        (job_id, "error" if status == "error" else "success", message or f"Terminé ({status})."),
    )
    conn.commit()
    conn.close()


def log_step(job_id: int, message: str, level: str = "info") -> None:
    """Ajoute une ligne au journal du job — appelé à chaque action
    significative pendant l'exécution (voir scripts/run_plan.py et
    app/restore.py), affichée en direct pendant que le job tourne."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO job_logs (job_id, level, message) VALUES (?, ?, ?)",
        (job_id, level, message),
    )
    conn.commit()
    conn.close()


def get_job(job_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return row


def get_job_logs(job_id: int) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM job_logs WHERE job_id = ? ORDER BY id ASC", (job_id,)
    ).fetchall()
    conn.close()
    return rows


def recent_jobs(limit: int = 50) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def jobs_for_plan(plan_id: int, limit: int = 20) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE plan_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
        (plan_id, limit),
    ).fetchall()
    conn.close()
    return rows


def last_executions_by_plan() -> dict[int, dict]:
    """Pour chaque plan, résumé de la dernière exécution : date/heure du
    job de snapshot le plus récent par VM, et statut agrégé (erreur si au
    moins une VM est en erreur sur son dernier job, en cours si au moins une
    VM tourne encore, sinon succès) — affiché sur le Dashboard et la liste
    des plans (voir aussi _plan_vm_status dans app/routers/admin.py, qui
    calcule le même statut par VM pour la page d'un plan)."""
    conn = get_connection()
    jobs = conn.execute(
        "SELECT plan_id, vm_id, status, started_at FROM jobs "
        "WHERE job_type = 'snapshot' AND plan_id IS NOT NULL AND vm_id IS NOT NULL "
        "ORDER BY started_at"
    ).fetchall()
    conn.close()

    last_per_vm: dict[tuple[int, str], dict] = {}
    for job in jobs:
        last_per_vm[(job["plan_id"], job["vm_id"])] = job

    by_plan: dict[int, list[dict]] = {}
    for (plan_id, _vm_id), job in last_per_vm.items():
        by_plan.setdefault(plan_id, []).append(job)

    summaries = {}
    for plan_id, plan_jobs in by_plan.items():
        if any(j["status"] == "error" for j in plan_jobs):
            status = "error"
        elif any(j["status"] == "running" for j in plan_jobs):
            status = "running"
        else:
            status = "success"
        summaries[plan_id] = {
            "started_at": max(j["started_at"] for j in plan_jobs),
            "status": status,
        }
    return summaries


def running_plan_ids() -> set[int]:
    """IDs des plans ayant au moins un job en cours — utilisé pour afficher
    le badge « En cours » à côté du statut Actif/Désactivé sur la liste des
    plans, la page Suivi et la page d'un plan."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT plan_id FROM jobs WHERE status = 'running' AND plan_id IS NOT NULL"
    ).fetchall()
    conn.close()
    return {row["plan_id"] for row in rows}


def last_job(job_type: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_type = ? ORDER BY started_at DESC, id DESC LIMIT 1", (job_type,)
    ).fetchone()
    conn.close()
    return row
