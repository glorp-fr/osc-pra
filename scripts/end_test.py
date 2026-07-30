#!/usr/bin/env python3
"""Termine un Test de PRA en cours : démonte les EIP et la NAT Gateway de
test, stoppe les VM cible et remet le plan en réplique froide normale (voir
app/failover.py::end_test). Lancé en arrière-plan depuis le bouton
« Terminer le test » de la page Visualiser du plan (app/routers/admin.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import failover, octl  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.jobs import finish_job, start_job  # noqa: E402
from app.target import resolve_target_credentials  # noqa: E402


def main(plan_id: int) -> None:
    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None or plan["failover_status"] != "test_en_cours":
        return

    job_id = start_job("bascule_fin", plan_id, plan["name"])

    target_ak, target_sk, target_region, target_error = resolve_target_credentials(plan)
    if target_error:
        finish_job(job_id, "error", target_error)
        return

    try:
        failover.end_test(plan, target_ak, target_sk, target_region, job_id)
    except octl.OctlError as exc:
        finish_job(job_id, "error", str(exc))
        return

    conn = get_connection()
    conn.execute("UPDATE plans SET failover_status = 'normal', failover_state = NULL WHERE id = ?", (plan_id,))
    conn.commit()
    conn.close()

    finish_job(job_id, "success", "Test de PRA terminé, VPC de PRA remis en réplique froide.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: end_test.py <plan_id>", file=sys.stderr)
        sys.exit(1)
    main(int(sys.argv[1]))
