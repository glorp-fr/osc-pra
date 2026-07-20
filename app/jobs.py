"""Suivi des jobs (snapshots planifiés, sauvegardes) exécutés par les scripts
cron (voir scripts/run_plan.py, scripts/run_backup.py et app/cron.py)."""
from app.db import get_connection


def start_job(job_type: str, plan_id: int | None = None, plan_name: str | None = None, vm_id: str | None = None) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO jobs (job_type, plan_id, plan_name, vm_id, status) VALUES (?, ?, ?, ?, 'running')",
        (job_type, plan_id, plan_name, vm_id),
    )
    conn.commit()
    job_id = cur.lastrowid
    conn.close()
    return job_id


def finish_job(job_id: int, status: str, message: str = "") -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET status = ?, message = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, message, job_id),
    )
    conn.commit()
    conn.close()


def recent_jobs(limit: int = 50) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows


def last_job(job_type: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_type = ? ORDER BY started_at DESC, id DESC LIMIT 1", (job_type,)
    ).fetchone()
    conn.close()
    return row
