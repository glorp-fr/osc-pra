#!/usr/bin/env python3
"""Exécute le job de snapshot planifié pour un plan de reprise.

Appelé par cron (voir app/cron.py) selon la fréquence de snapshot configurée
sur le plan, ou directement depuis l'admin pour un lancement manuel. Un job
est créé par VM sélectionnée, pour permettre un suivi individuel (voir
CLAUDE.MD, section « Suivi des jobs »).

Seule l'étape 1 du mécanisme de réplication est implémentée ici (snapshot
des disques de la VM source, avec purge au-delà de source_retain_count).
La création de la VM cible, la restauration des disques dans l'ordre et,
pour une cible cross-région, l'export/import via S3, restent à implémenter.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import octl  # noqa: E402
from app.crypto import decrypt  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.jobs import finish_job, start_job  # noqa: E402


def main(plan_id: int) -> None:
    conn = get_connection()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    conn.close()
    if plan is None or not plan["active"]:
        return

    selected_vms = json.loads(plan["selected_vms"] or "[]")
    if not selected_vms:
        return

    if not octl.is_available():
        job_id = start_job("snapshot", plan_id, plan["name"])
        finish_job(job_id, "error", "octl n'est pas installé sur ce serveur.")
        return

    sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""

    try:
        vms = octl.list_vms(plan["source_ak"], sk, plan["source_region"])
    except octl.OctlError as exc:
        job_id = start_job("snapshot", plan_id, plan["name"])
        finish_job(job_id, "error", f"Scan des VMs impossible : {exc}")
        return

    vms_by_id = {vm.get("VmId"): vm for vm in vms}

    for vm_id in selected_vms:
        _run_vm_snapshot(plan, sk, vm_id, vms_by_id.get(vm_id))


def _run_vm_snapshot(plan, sk: str, vm_id: str, vm: dict | None) -> None:
    job_id = start_job("snapshot", plan["id"], plan["name"], vm_id)

    if vm is None:
        finish_job(job_id, "error", "VM introuvable sur le compte source (supprimée ?).")
        return

    volume_ids = [
        bdm["Bsu"]["VolumeId"]
        for bdm in vm.get("BlockDeviceMappings", [])
        if bdm.get("Bsu", {}).get("VolumeId")
    ]
    if not volume_ids:
        finish_job(job_id, "error", "Aucun volume trouvé pour cette VM.")
        return

    try:
        snapshot_ids = [
            octl.create_snapshot(
                plan["source_ak"], sk, plan["source_region"], volume_id,
                f"osc-pra plan={plan['name']} vm={vm_id}",
            ).get("SnapshotId")
            for volume_id in volume_ids
        ]
        _prune_old_snapshots(plan, sk, volume_ids)
    except octl.OctlError as exc:
        finish_job(job_id, "error", str(exc))
        return

    finish_job(
        job_id, "success",
        f"Snapshots créés : {', '.join(s for s in snapshot_ids if s)}. "
        "Création de la VM cible / restauration des disques : à implémenter.",
    )


def _prune_old_snapshots(plan, sk: str, volume_ids: list[str]) -> None:
    retain = plan["source_retain_count"] or 7
    for volume_id in volume_ids:
        try:
            snapshots = octl.list_snapshots(plan["source_ak"], sk, plan["source_region"], volume_id)
        except octl.OctlError:
            continue
        snapshots.sort(key=lambda s: s.get("CreationDate", ""), reverse=True)
        for old in snapshots[retain:]:
            snapshot_id = old.get("SnapshotId")
            if snapshot_id:
                try:
                    octl.delete_snapshot(plan["source_ak"], sk, plan["source_region"], snapshot_id)
                except octl.OctlError:
                    pass


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: run_plan.py <plan_id>", file=sys.stderr)
        sys.exit(1)
    main(int(sys.argv[1]))
