"""Restauration des BSU sur la VM cible après chaque cycle de snapshot (voir
CLAUDE.MD, section « Même région », étapes 2 à 4).

La VM cible est créée automatiquement si elle n'existe pas encore (mapping
mémorisé dans la table vm_targets), avec la même configuration que la VM
source (image, type, security groups, subnet — ces deux derniers résolus
côté cible via leur tag/nom, donc après resynchronisation des ressources PRA
depuis la page Visualiser du plan). À chaque cycle, ses volumes sont
remplacés par des volumes restaurés depuis les derniers snapshots. La VM
cible reste à l'arrêt entre deux cycles (réplique froide) : elle n'est
démarrée que manuellement, lors d'un vrai basculement.

Cross-région (nécessite l'export/import via S3) n'est pas géré ici — voir
CLAUDE.MD, section « Cross région ».

Ne gère que les devices déjà présents sur la VM cible lors d'un cycle de
mise à jour (un nouveau volume attaché côté source après la création de la
VM cible ne sera pas répliqué automatiquement) — limitation connue.
"""
import time

from app import octl
from app.db import get_connection
from app.target import tag_name

POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 600


class RestoreError(Exception):
    pass


def _get_target_vm_id(plan_id: int, source_vm_id: str) -> str | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT target_vm_id FROM vm_targets WHERE plan_id = ? AND source_vm_id = ?",
        (plan_id, source_vm_id),
    ).fetchone()
    conn.close()
    return row["target_vm_id"] if row else None


def _save_target_vm_id(plan_id: int, source_vm_id: str, target_vm_id: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO vm_targets (plan_id, source_vm_id, target_vm_id) VALUES (?, ?, ?) "
        "ON CONFLICT(plan_id, source_vm_id) DO UPDATE SET target_vm_id = excluded.target_vm_id",
        (plan_id, source_vm_id, target_vm_id),
    )
    conn.commit()
    conn.close()


def _wait_volume_available(ak: str, sk: str, region: str, volume_id: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        volumes = octl.list_volumes(ak, sk, region, [volume_id])
        volume = volumes[0] if volumes else None
        if volume and volume.get("State") == "available":
            return volume
        if volume and volume.get("State") == "error":
            raise RestoreError(f"Le volume {volume_id} est passé en erreur après restauration.")
        if time.monotonic() >= deadline:
            raise RestoreError(f"Délai dépassé en attendant la disponibilité du volume {volume_id}.")
        time.sleep(POLL_INTERVAL_SECONDS)


def _wait_vm_state(ak: str, sk: str, region: str, vm_id: str, state: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        vm = octl.get_vm(ak, sk, region, vm_id)
        if vm and vm.get("State") == state:
            return vm
        if time.monotonic() >= deadline:
            raise RestoreError(f"Délai dépassé en attendant que la VM {vm_id} soit à l'état « {state} ».")
        time.sleep(POLL_INTERVAL_SECONDS)


def _resolve_target_network(target_ak, target_sk, target_region, target_vpc_id, source_subnet, source_sgs):
    target_subnets = octl.list_subnets(target_ak, target_sk, target_region)
    subnet_name = tag_name(source_subnet, source_subnet.get("SubnetId"))
    target_subnet = next(
        (
            s for s in target_subnets
            if s.get("NetId") == target_vpc_id and tag_name(s, s.get("SubnetId")) == subnet_name
        ),
        None,
    )
    if target_subnet is None:
        raise RestoreError(
            f"Subnet cible correspondant à « {subnet_name} » introuvable — "
            "resynchronise les ressources PRA du plan (page Visualiser) avant de relancer."
        )

    target_sg_by_name = {
        g.get("SecurityGroupName"): g.get("SecurityGroupId")
        for g in octl.list_security_groups(target_ak, target_sk, target_region)
        if g.get("NetId") == target_vpc_id
    }
    sg_names = {sg.get("SecurityGroupName") for sg in source_sgs if sg.get("SecurityGroupName")}
    missing = sg_names - target_sg_by_name.keys()
    if missing:
        raise RestoreError(
            f"Security group(s) cible introuvable(s) : {', '.join(sorted(missing))} — "
            "resynchronise les ressources PRA du plan (page Visualiser) avant de relancer."
        )
    return target_subnet["SubnetId"], [target_sg_by_name[n] for n in sg_names]


def _create_target_vm(
    plan, target_ak, target_sk, target_region, source_vm, source_subnet,
    source_volumes_by_id, snapshots_by_volume,
) -> str:
    subnet_id, sg_ids = _resolve_target_network(
        target_ak, target_sk, target_region, plan["target_vpc_id"], source_subnet, source_vm.get("SecurityGroups", [])
    )

    root_device = source_vm.get("RootDeviceName")
    root_volume_id = next(
        (
            bdm["Bsu"]["VolumeId"] for bdm in source_vm.get("BlockDeviceMappings", [])
            if bdm.get("DeviceName") == root_device
        ),
        None,
    )
    root_snapshot_id = snapshots_by_volume.get(root_volume_id)
    if root_snapshot_id is None:
        raise RestoreError("Snapshot du volume racine introuvable pour créer la VM cible.")
    root_volume = source_volumes_by_id.get(root_volume_id, {})

    target_vm = octl.create_vm(
        target_ak, target_sk, target_region,
        image_id=source_vm["ImageId"],
        vm_type=source_vm["VmType"],
        subnet_id=subnet_id,
        security_group_ids=sg_ids,
        subregion=source_subnet["SubregionName"],
        root_device_name=root_device,
        root_snapshot_id=root_snapshot_id,
        root_volume_type=root_volume.get("VolumeType"),
        root_iops=root_volume.get("Iops"),
    )
    target_vm_id = target_vm.get("VmId")
    if not target_vm_id:
        raise RestoreError("La création de la VM cible n'a pas renvoyé d'identifiant.")

    _wait_vm_state(target_ak, target_sk, target_region, target_vm_id, "stopped")
    return target_vm_id


def _refresh_target_vm(target_ak, target_sk, target_region, target_vm_id, source_vm, source_subnet, source_volumes_by_id, snapshots_by_volume) -> set:
    target_vm = octl.get_vm(target_ak, target_sk, target_region, target_vm_id)
    if target_vm is None:
        raise RestoreError(f"VM cible {target_vm_id} introuvable (supprimée manuellement ?).")

    if target_vm.get("State") != "stopped":
        octl.stop_vm(target_ak, target_sk, target_region, target_vm_id)
        target_vm = _wait_vm_state(target_ak, target_sk, target_region, target_vm_id, "stopped")

    restored_devices = set()
    for bdm in target_vm.get("BlockDeviceMappings", []):
        device_name = bdm.get("DeviceName")
        old_volume_id = bdm.get("Bsu", {}).get("VolumeId")
        source_volume_id = next(
            (
                bdm2["Bsu"]["VolumeId"] for bdm2 in source_vm.get("BlockDeviceMappings", [])
                if bdm2.get("DeviceName") == device_name
            ),
            None,
        )
        snapshot_id = snapshots_by_volume.get(source_volume_id)
        if snapshot_id is None:
            continue

        if old_volume_id:
            octl.detach_volume(target_ak, target_sk, target_region, old_volume_id)

        source_volume = source_volumes_by_id.get(source_volume_id, {})
        new_volume = octl.create_volume(
            target_ak, target_sk, target_region, snapshot_id, source_subnet["SubregionName"],
            volume_type=source_volume.get("VolumeType"), iops=source_volume.get("Iops"),
        )
        new_volume_id = new_volume.get("VolumeId")
        _wait_volume_available(target_ak, target_sk, target_region, new_volume_id)
        octl.attach_volume(target_ak, target_sk, target_region, new_volume_id, target_vm_id, device_name)

        if old_volume_id:
            octl.delete_volume(target_ak, target_sk, target_region, old_volume_id)

        restored_devices.add(device_name)

    return restored_devices


def restore_vm(
    plan, target_ak: str, target_sk: str, target_region: str, source_vm: dict, source_subnet: dict,
    source_volumes_by_id: dict, snapshots_by_volume: dict,
) -> str:
    """Point d'entrée appelé par scripts/run_plan.py après un snapshot
    réussi : crée la VM cible si besoin, puis remplace ses volumes par des
    volumes restaurés depuis les snapshots qui viennent d'être créés.
    Retourne un message récapitulatif ; lève RestoreError en cas d'échec."""
    if not plan["target_vpc_id"]:
        raise RestoreError(
            "VPC cible non créé pour ce plan — crée-le depuis la page Modifier avant d'activer la restauration."
        )

    source_vm_id = source_vm["VmId"]
    target_vm_id = _get_target_vm_id(plan["id"], source_vm_id)

    if target_vm_id is None:
        target_vm_id = _create_target_vm(
            plan, target_ak, target_sk, target_region, source_vm, source_subnet,
            source_volumes_by_id, snapshots_by_volume,
        )
        _save_target_vm_id(plan["id"], source_vm_id, target_vm_id)
        restored_count = 1
    else:
        restored_devices = _refresh_target_vm(
            target_ak, target_sk, target_region, target_vm_id, source_vm, source_subnet,
            source_volumes_by_id, snapshots_by_volume,
        )
        restored_count = len(restored_devices)

    return f"VM cible {target_vm_id} : {restored_count} volume(s) restauré(s)."
