#!/usr/bin/env python3
"""Exécute une bascule PRA (Activation ou Test) pour un plan — lancé en
arrière-plan depuis app/routers/admin.py (boutons de la page Visualiser).

Activation : démarre les VM cible dans l'ordre configuré
(plans.vm_restart_order), réutilise les EIP de production (bascule) et
reconstruit la NAT Gateway avec l'ancienne IP publique (nouvelle IP si ça
échoue — voir app/failover.py::assign_nat). Le plan est désactivé à la fin
(source présumée hors service).

Test : même mécanique de démarrage, mais avec des EIP et une NAT neuves,
jamais de modification côté source. Le plan passe en `failover_status =
'test_en_cours'` (sync automatique suspendue, voir scripts/run_plan.py) —
à terminer explicitement via scripts/end_test.py.

Ne fait rien pour les VM sans VM cible déjà répliquée (vm_targets) : la
bascule agit sur ce qui a déjà été répliqué par la sync normale, elle ne
crée pas de nouvelle VM cible.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import failover, octl  # noqa: E402
from app.crypto import decrypt  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.jobs import finish_job, log_step, start_job  # noqa: E402
from app.target import resolve_target_credentials  # noqa: E402


def main(plan_id: int, mode: str) -> None:
    if mode not in ("activation", "test"):
        print("Usage: run_bascule.py <plan_id> <activation|test>", file=sys.stderr)
        sys.exit(1)

    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None:
        return

    job_id = start_job("bascule", plan_id, plan["name"])

    if not octl.is_available():
        finish_job(job_id, "error", "octl n'est pas installé sur ce serveur.")
        return
    if not plan["target_vpc_id"]:
        finish_job(job_id, "error", "VPC cible non créé pour ce plan.")
        return

    target_ak, target_sk, target_region, target_error = resolve_target_credentials(plan)
    if target_error:
        finish_job(job_id, "error", target_error)
        return

    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""
    selected_vms = json.loads(plan["selected_vms"] or "[]")

    conn = get_connection()
    vm_target_rows = conn.execute(
        "SELECT source_vm_id, target_vm_id FROM vm_targets WHERE plan_id = ?", (plan_id,)
    ).fetchall()
    conn.close()
    target_vm_id_by_source = {row["source_vm_id"]: row["target_vm_id"] for row in vm_target_rows}

    replicated_vms = [vm_id for vm_id in selected_vms if vm_id in target_vm_id_by_source]
    if not replicated_vms:
        finish_job(job_id, "error", "Aucune VM cible déjà répliquée pour ce plan — rien à basculer.")
        return

    log_step(job_id, f"{'Activation PRA' if mode == 'activation' else 'Test de PRA'} — {len(replicated_vms)} VM(s).")

    try:
        order = failover.resolve_start_order(plan, replicated_vms)
        failover.start_vms_in_order(target_ak, target_sk, target_region, order, target_vm_id_by_source, job_id)

        vm_eips = failover.assign_eips(
            target_ak, target_sk, target_region,
            plan["source_ak"], source_sk, plan["source_region"],
            replicated_vms, target_vm_id_by_source, mode, job_id,
        )
        nat = failover.assign_nat(
            plan["source_ak"], source_sk, plan["source_region"], plan["source_vpc_id"],
            target_ak, target_sk, target_region, plan["target_vpc_id"], mode, job_id,
        )
    except octl.OctlError as exc:
        log_step(job_id, f"Échec de la bascule : {exc}", level="error")
        finish_job(job_id, "error", str(exc))
        return

    state = {
        "type": mode, "started_at": datetime.now(timezone.utc).isoformat(), "vm_eips": vm_eips, "nat": nat,
    }
    new_status = "bascule" if mode == "activation" else "test_en_cours"
    conn = get_connection()
    if mode == "activation":
        conn.execute(
            "UPDATE plans SET failover_status = ?, failover_state = ?, active = 0 WHERE id = ?",
            (new_status, json.dumps(state), plan_id),
        )
    else:
        conn.execute(
            "UPDATE plans SET failover_status = ?, failover_state = ? WHERE id = ?",
            (new_status, json.dumps(state), plan_id),
        )
    conn.commit()
    conn.close()

    finish_job(job_id, "success", f"{'Activation' if mode == 'activation' else 'Test'} PRA terminé(e) : {len(order)} VM(s) traitée(s).")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: run_bascule.py <plan_id> <activation|test>", file=sys.stderr)
        sys.exit(1)
    main(int(sys.argv[1]), sys.argv[2])
