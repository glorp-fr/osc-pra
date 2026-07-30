#!/usr/bin/env python3
"""Exécute le job de snapshot planifié pour un plan de reprise.

Appelé par cron (voir app/cron.py) selon la fréquence de snapshot configurée
sur le plan, ou directement depuis l'admin pour un lancement manuel. Un job
est créé par VM sélectionnée, pour permettre un suivi individuel (voir
CLAUDE.MD, section « Suivi des jobs »).

Pour la cible « même région », les étapes 1 à 4 du mécanisme de réplication
sont implémentées : snapshot des disques source (avec purge au-delà de
source_retain_count), création de la VM cible si besoin puis restauration
de ses volumes depuis les derniers snapshots à chaque cycle (voir
app/restore.py). Pour une cible cross-région, l'export/import via S3 reste
à implémenter : la restauration est ignorée avec un message explicite.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import octl, restore  # noqa: E402
from app.crypto import decrypt  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.jobs import finish_job, log_step, start_job  # noqa: E402
from app.target import resolve_target_credentials, sync_target_network  # noqa: E402


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
        subnets = octl.list_subnets(plan["source_ak"], sk, plan["source_region"])
    except octl.OctlError as exc:
        job_id = start_job("snapshot", plan_id, plan["name"])
        finish_job(job_id, "error", f"Scan des VMs impossible : {exc}")
        return

    vms_by_id = {vm.get("VmId"): vm for vm in vms}
    subnets_by_id = {subnet.get("SubnetId"): subnet for subnet in subnets}

    target_ak, target_sk, target_region, target_error = resolve_target_credentials(plan)

    sync_log = None
    if (
        plan["auto_sync_target"]
        and not target_error
        and plan["source_vpc_id"]
        and plan["target_vpc_id"]
        and plan["target_subregion"]
    ):
        sync_log = _sync_target_vpc(plan, target_ak, target_sk, target_region)

    for vm_id in selected_vms:
        _run_vm_snapshot(
            plan, sk, vm_id, vms_by_id.get(vm_id), subnets_by_id,
            target_ak, target_sk, target_region, target_error, sync_log,
        )


def _sync_target_vpc(plan, target_ak: str, target_sk: str, target_region: str) -> tuple[str, str]:
    """Reconstruit les objets manquants sur le VPC cible avant de répliquer
    les VMs — une seule fois par exécution du plan (pas par VM), voir
    app.target.sync_target_network. Contrôlé par l'option « Mise à jour du
    VPC cible automatique » du plan (auto_sync_target, cochée par défaut)."""
    try:
        summary = sync_target_network(plan, target_ak, target_sk, target_region, plan["target_vpc_id"])
        created = (
            summary["subnets_created"] + summary["sgs_created"] + summary["route_tables_created"]
            + (1 if summary["internet_service_created"] else 0)
        )
        if created:
            return "info", f"VPC cible resynchronisé automatiquement : {created} objet(s) manquant(s) recréé(s)."
        return "info", "VPC cible resynchronisé automatiquement : aucun objet manquant."
    except octl.OctlError as exc:
        return "error", f"Échec de la resynchronisation automatique du VPC cible : {exc}"


def _run_vm_snapshot(
    plan, sk: str, vm_id: str, vm: dict | None, subnets_by_id: dict,
    target_ak: str, target_sk: str, target_region: str, target_error: str | None,
    sync_log: tuple[str, str] | None = None,
) -> None:
    job_id = start_job("snapshot", plan["id"], plan["name"], vm_id)

    if sync_log:
        log_step(job_id, sync_log[1], level=sync_log[0])

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

    log_step(job_id, f"{len(volume_ids)} volume(s) à sauvegarder : {', '.join(volume_ids)}.")

    try:
        snapshots_by_volume = {}
        for volume_id in volume_ids:
            log_step(job_id, f"Snapshot du volume {volume_id} en cours...")
            snapshot_id = octl.create_snapshot(
                plan["source_ak"], sk, plan["source_region"], volume_id,
                f"osc-pra plan={plan['name']} vm={vm_id}",
            ).get("SnapshotId")
            snapshots_by_volume[volume_id] = snapshot_id
            log_step(job_id, f"Snapshot créé pour {volume_id} : {snapshot_id}.")

        log_step(job_id, f"Purge des anciens snapshots (conservation : {plan['source_retain_count'] or 7})...")
        pruned = _prune_old_snapshots(plan, sk, volume_ids)
        log_step(job_id, f"{pruned} ancien(s) snapshot(s) purgé(s)." if pruned else "Aucun ancien snapshot à purger.")
    except octl.OctlError as exc:
        log_step(job_id, f"Échec du snapshot : {exc}", level="error")
        finish_job(job_id, "error", str(exc))
        return

    snapshot_summary = f"Snapshots créés : {', '.join(s for s in snapshots_by_volume.values() if s)}."

    if plan["target_type"] == "autre_region":
        finish_job(
            job_id, "success",
            f"{snapshot_summary} Restauration sur la VM cible non disponible pour une cible cross-région "
            "(export/import S3 à implémenter).",
        )
        return

    if target_error:
        finish_job(job_id, "error", f"{snapshot_summary} Restauration sur la VM cible impossible : {target_error}")
        return

    try:
        log_step(job_id, "Résolution des volumes et du subnet source...")
        source_volumes = octl.list_volumes(plan["source_ak"], sk, plan["source_region"], volume_ids)
        source_volumes_by_id = {v["VolumeId"]: v for v in source_volumes}
        source_subnet = subnets_by_id.get(vm.get("SubnetId"))
        if source_subnet is None:
            raise restore.RestoreError("Subnet source introuvable pour cette VM.")

        restore_message = restore.restore_vm(
            plan, target_ak, target_sk, target_region, vm, source_subnet,
            source_volumes_by_id, snapshots_by_volume, job_id,
        )
        finish_job(job_id, "success", f"{snapshot_summary} {restore_message}")
    except (restore.RestoreError, octl.OctlError) as exc:
        log_step(job_id, f"Échec de la restauration sur la VM cible : {exc}", level="error")
        finish_job(job_id, "error", f"{snapshot_summary} Restauration sur la VM cible échouée : {exc}")


def _prune_old_snapshots(plan, sk: str, volume_ids: list[str]) -> int:
    retain = plan["source_retain_count"] or 7
    pruned = 0
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
                    pruned += 1
                except octl.OctlError:
                    pass
    return pruned


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: run_plan.py <plan_id>", file=sys.stderr)
        sys.exit(1)
    main(int(sys.argv[1]))
