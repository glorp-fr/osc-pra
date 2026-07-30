from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import octl
from app.auth import require_login
from app.crypto import decrypt
from app.db import get_connection
from app.jobs import get_job_logs, last_executions_by_plan, recent_jobs, running_plan_ids
from app.templates_env import templates

router = APIRouter()

AK_WARNING_DAYS = 7


def _days_remaining(expiration_date: str | None) -> int | None:
    if not expiration_date:
        return None
    try:
        expires = datetime.fromisoformat(expiration_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return (expires - datetime.now(timezone.utc)).days


def _check_plan_ak(plan) -> dict | None:
    if not (plan["source_ak"] and plan["source_sk_encrypted"] and plan["source_region"]):
        return None
    sk = decrypt(plan["source_sk_encrypted"])
    result = octl.check_access_key(plan["source_ak"], sk, plan["source_region"])
    result["days_remaining"] = _days_remaining(result.get("expiration_date"))
    return result


@router.get("/suivi")
def suivi(request: Request):
    user = require_login(request)
    if isinstance(user, RedirectResponse):
        return user

    conn = get_connection()
    plans = conn.execute(
        "SELECT id, name, active, source_ak, source_sk_encrypted, source_region FROM plans ORDER BY name"
    ).fetchall()
    conn.close()

    octl_available = octl.is_available()
    ak_statuses = {plan["id"]: (_check_plan_ak(plan) if octl_available else None) for plan in plans}
    ak_alerts = [
        {"plan": plan, "status": ak_statuses[plan["id"]]}
        for plan in plans
        if ak_statuses[plan["id"]] and (
            not ak_statuses[plan["id"]]["valid"]
            or (
                ak_statuses[plan["id"]]["days_remaining"] is not None
                and ak_statuses[plan["id"]]["days_remaining"] < AK_WARNING_DAYS
            )
        )
    ]

    jobs = recent_jobs()
    latest_job = jobs[0] if jobs else None

    return templates.TemplateResponse(
        "suivi.html",
        {
            "request": request,
            "user": user,
            "plans": plans,
            "jobs": jobs,
            "latest_job": latest_job,
            "latest_job_logs": get_job_logs(latest_job["id"]) if latest_job else [],
            "octl_available": octl_available,
            "ak_statuses": ak_statuses,
            "ak_alerts": ak_alerts,
            "ak_warning_days": AK_WARNING_DAYS,
            "running_plan_ids": running_plan_ids(),
            "last_executions": last_executions_by_plan(),
        },
    )
