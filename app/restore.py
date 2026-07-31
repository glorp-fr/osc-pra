"""Restauration des BSU sur la VM cible après chaque cycle de snapshot (voir
CLAUDE.MD, section « Même région », étapes 2 à 4).

La VM cible est créée automatiquement si elle n'existe pas encore (mapping
mémorisé dans la table vm_targets), avec la même configuration que la VM
source (type, security groups, subnet — ces deux derniers résolus côté cible
via leur tag/nom, donc après resynchronisation des ressources PRA depuis la
page Visualiser du plan — et tags, tous repris tels quels, y compris Name)
— sauf l'image, qui peut être remplacée par une OMI différente choisie par
VM (plans.vm_image_overrides, définie sur la page de config du plan). Les
tags sont remis en phase à chaque cycle (voir _refresh_target_vm), pas
seulement à la création.

Création du BSU racine en 5 étapes (voir _create_target_vm) : 1) restauration
du BSU depuis le snapshot AVANT toute action sur la VM (un échec ici ne crée
aucune VM) ; 2) création de la VM cible (boot normal depuis l'image) ;
3) attente de l'état running ; 4) extinction ; 5) détachement du BSU racine
fraîchement créé (depuis l'image) et attachement du BSU restauré à sa place.
À chaque cycle suivant (voir _refresh_target_vm), la VM cible est remise en
phase avec la config actuelle de la VM source : type de VM (CPU/RAM, via
UpdateVm — nécessite l'arrêt, déjà fait pour le remplacement des volumes),
volumes existants remplacés depuis leur dernier snapshot, volumes ajoutés
côté source depuis la création de la VM cible désormais attachés, volumes
retirés côté source détachés et supprimés côté cible. La VM cible reste à
l'arrêt entre deux cycles (réplique froide) : elle n'est démarrée que
manuellement, lors d'un vrai basculement.

La génération de snapshot actuellement restaurée sur le volume racine de la
VM cible est mémorisée (vm_targets.restored_snapshot_id/restored_at, voir
_record_root_restore) et affichée sur la page du plan.

Cross-région (nécessite l'export/import via S3) n'est pas géré ici — voir
CLAUDE.MD, section « Cross région ».
"""
import time

from app import octl
from app.db import get_connection
from app.jobs import log_step
from app.target import tag_name

POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 600


class RestoreError(Exception):
    pass


def _get_target_vm_id(plan_id: int, source_vm_id: str, sandbox_id: int | None = None) -> str | None:
    """`sandbox_id` bascule la lecture vers `sandbox_vm_targets` (mapping
    des VM d'un sandbox, indépendant du mapping PRA persistant du plan dans
    `vm_targets`) — voir app/failover.py et scripts/run_sandbox.py."""
    conn = get_connection()
    if sandbox_id is not None:
        row = conn.execute(
            "SELECT target_vm_id FROM sandbox_vm_targets WHERE sandbox_id = ? AND source_vm_id = ?",
            (sandbox_id, source_vm_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT target_vm_id FROM vm_targets WHERE plan_id = ? AND source_vm_id = ?",
            (plan_id, source_vm_id),
        ).fetchone()
    conn.close()
    return row["target_vm_id"] if row else None


def _save_target_vm_id(plan_id: int, source_vm_id: str, target_vm_id: str, sandbox_id: int | None = None) -> None:
    conn = get_connection()
    if sandbox_id is not None:
        conn.execute(
            "INSERT INTO sandbox_vm_targets (sandbox_id, source_vm_id, target_vm_id) VALUES (?, ?, ?) "
            "ON CONFLICT(sandbox_id, source_vm_id) DO UPDATE SET target_vm_id = excluded.target_vm_id",
            (sandbox_id, source_vm_id, target_vm_id),
        )
    else:
        conn.execute(
            "INSERT INTO vm_targets (plan_id, source_vm_id, target_vm_id) VALUES (?, ?, ?) "
            "ON CONFLICT(plan_id, source_vm_id) DO UPDATE SET target_vm_id = excluded.target_vm_id",
            (plan_id, source_vm_id, target_vm_id),
        )
    conn.commit()
    conn.close()


def _record_root_restore(plan_id: int, source_vm_id: str, snapshot_id: str, sandbox_id: int | None = None) -> None:
    """Note la génération de snapshot actuellement restaurée sur le volume
    racine de la VM cible — affiché sur la page du plan (VMs et points de
    sauvegarde) pour savoir à quelle date correspond l'état de la VM cible."""
    conn = get_connection()
    if sandbox_id is not None:
        conn.execute(
            "UPDATE sandbox_vm_targets SET restored_snapshot_id = ?, restored_at = CURRENT_TIMESTAMP "
            "WHERE sandbox_id = ? AND source_vm_id = ?",
            (snapshot_id, sandbox_id, source_vm_id),
        )
    else:
        conn.execute(
            "UPDATE vm_targets SET restored_snapshot_id = ?, restored_at = CURRENT_TIMESTAMP "
            "WHERE plan_id = ? AND source_vm_id = ?",
            (snapshot_id, plan_id, source_vm_id),
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
    """Retourne (subnet_id, subregion, sg_ids) du réseau cible. La subregion
    renvoyée est celle du subnet CIBLE réellement trouvé, pas celle du
    subnet source : les subnets cible sont tous créés dans l'AZ cible
    configurée sur le plan (plans.target_subregion), indépendamment de
    celle du subnet source équivalent — utiliser la subregion source pour
    placer la VM ou créer un volume cible fait échouer l'appel avec
    InvalidParameterValue (SubnetId/Placement ou SubnetId/volume
    incohérents, constaté en pratique)."""
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
    return target_subnet["SubnetId"], target_subnet["SubregionName"], [target_sg_by_name[n] for n in sg_names]


def _create_target_vm(
    plan, target_ak, target_sk, target_region, source_vm, source_subnet,
    source_volumes_by_id, snapshots_by_volume, image_id, job_id, target_vpc_id: str | None = None,
) -> tuple[str, str]:
    """Crée la VM cible en 5 étapes (voir CLAUDE.MD) :
    1) restaure le BSU racine depuis le snapshot — avant toute action sur la
       VM, pour qu'un échec ici ne crée/ne touche aucune VM ;
    2) crée la VM cible (boot normal depuis l'image) ;
    3) attend qu'elle soit `running` ;
    4) l'éteint ;
    5) détache son BSU racine fraîchement créé (depuis l'image) et attache
       à la place le BSU restauré à l'étape 1.

    `target_vpc_id` permet de cibler un VPC différent de celui du plan
    (`plan["target_vpc_id"]`, utilisé par défaut) — cas d'un sandbox, voir
    scripts/run_sandbox.py."""
    log_step(job_id, "Résolution du subnet et des security groups cible...")
    subnet_id, target_subregion, sg_ids = _resolve_target_network(
        target_ak, target_sk, target_region, target_vpc_id or plan["target_vpc_id"],
        source_subnet, source_vm.get("SecurityGroups", []),
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

    log_step(job_id, f"Restauration du BSU racine depuis le snapshot {root_snapshot_id}...")
    restored_volume = octl.create_volume(
        target_ak, target_sk, target_region, root_snapshot_id, target_subregion,
        volume_type=root_volume.get("VolumeType"), iops=root_volume.get("Iops"),
        tags=root_volume.get("Tags", []),
    )
    restored_volume_id = restored_volume.get("VolumeId")
    _wait_volume_available(target_ak, target_sk, target_region, restored_volume_id)
    log_step(job_id, f"BSU racine restauré : {restored_volume_id}.")

    log_step(job_id, f"Création de la VM cible (OMI {image_id}, type {source_vm['VmType']})...")
    target_vm = octl.create_vm(
        target_ak, target_sk, target_region,
        image_id=image_id,
        vm_type=source_vm["VmType"],
        subnet_id=subnet_id,
        security_group_ids=sg_ids,
        subregion=target_subregion,
        tags=source_vm.get("Tags", []),
    )
    target_vm_id = target_vm.get("VmId")
    if not target_vm_id:
        raise RestoreError("La création de la VM cible n'a pas renvoyé d'identifiant.")

    log_step(job_id, f"VM cible {target_vm_id} créée, attente de l'état running...")
    _wait_vm_state(target_ak, target_sk, target_region, target_vm_id, "running")

    log_step(job_id, f"VM cible {target_vm_id} : extinction avant remplacement du BSU racine...")
    octl.stop_vm(target_ak, target_sk, target_region, target_vm_id)
    fresh_vm = _wait_vm_state(target_ak, target_sk, target_region, target_vm_id, "stopped")
    log_step(job_id, f"VM cible {target_vm_id} arrêtée.")

    fresh_root_volume_id = next(
        (
            bdm["Bsu"]["VolumeId"] for bdm in fresh_vm.get("BlockDeviceMappings", [])
            if bdm.get("DeviceName") == root_device
        ),
        None,
    )
    if fresh_root_volume_id:
        octl.detach_volume(target_ak, target_sk, target_region, fresh_root_volume_id)
        log_step(job_id, f"BSU racine d'origine {fresh_root_volume_id} (depuis l'image) détaché.")
        octl.delete_volume(target_ak, target_sk, target_region, fresh_root_volume_id)
        log_step(job_id, f"BSU racine d'origine {fresh_root_volume_id} supprimé.")

    octl.attach_volume(target_ak, target_sk, target_region, restored_volume_id, target_vm_id, root_device)
    log_step(job_id, f"BSU restauré {restored_volume_id} attaché en tant que volume racine.")

    return target_vm_id, root_snapshot_id


def _refresh_target_vm(
    target_ak, target_sk, target_region, target_vm_id, source_vm,
    source_volumes_by_id, snapshots_by_volume, job_id, plan_id, source_vm_id, sandbox_id: int | None = None,
) -> set:
    """Remet la VM cible en phase avec la config actuelle de la VM source à
    chaque cycle : type de VM (CPU/RAM), et BSU — volumes existants
    remplacés depuis leur dernier snapshot, volumes ajoutés côté source
    depuis la création de la VM cible désormais attachés, volumes retirés
    côté source détachés et supprimés côté cible."""
    target_vm = octl.get_vm(target_ak, target_sk, target_region, target_vm_id)
    if target_vm is None:
        raise RestoreError(f"VM cible {target_vm_id} introuvable (supprimée manuellement ?).")

    if target_vm.get("State") != "stopped":
        log_step(job_id, f"VM cible {target_vm_id} : arrêt avant remplacement des volumes...")
        octl.stop_vm(target_ak, target_sk, target_region, target_vm_id)
        target_vm = _wait_vm_state(target_ak, target_sk, target_region, target_vm_id, "stopped")
        log_step(job_id, f"VM cible {target_vm_id} arrêtée.")

    if source_vm.get("VmType") and target_vm.get("VmType") != source_vm["VmType"]:
        log_step(
            job_id,
            f"VM cible {target_vm_id} : type {target_vm.get('VmType')} -> {source_vm['VmType']} "
            "(changement détecté côté source)...",
        )
        octl.update_vm_type(target_ak, target_sk, target_region, target_vm_id, source_vm["VmType"])
        log_step(job_id, f"VM cible {target_vm_id} : type mis à jour.")

    # Tags remis en phase à chaque cycle (pas seulement à la création) pour
    # suivre un renommage/retaguage côté source — CreateTags est idempotent
    # (écrase la valeur d'un tag déjà présent, n'échoue pas si rien ne change).
    octl.create_tags(target_ak, target_sk, target_region, target_vm_id, source_vm.get("Tags", []))

    # Subregion réelle de la VM cible (pas celle de source_subnet) : un volume
    # créé dans une autre subregion que la VM ne peut pas s'y attacher, et
    # rien ne garantit que le subnet cible partage la subregion du subnet
    # source (voir _resolve_target_network).
    target_subregion = target_vm.get("Placement", {}).get("SubregionName")

    root_device = target_vm.get("RootDeviceName")
    target_bdms_by_device = {bdm.get("DeviceName"): bdm for bdm in target_vm.get("BlockDeviceMappings", [])}
    source_bdms_by_device = {bdm.get("DeviceName"): bdm for bdm in source_vm.get("BlockDeviceMappings", [])}

    restored_devices = set()
    for device_name, bdm in target_bdms_by_device.items():
        source_bdm = source_bdms_by_device.get(device_name)
        if source_bdm is None:
            continue  # device retiré côté source, traité plus bas

        old_volume_id = bdm.get("Bsu", {}).get("VolumeId")
        source_volume_id = source_bdm.get("Bsu", {}).get("VolumeId")
        snapshot_id = snapshots_by_volume.get(source_volume_id)
        if snapshot_id is None:
            continue

        log_step(job_id, f"Volume {device_name} : restauration depuis le snapshot {snapshot_id}...")

        if old_volume_id:
            octl.detach_volume(target_ak, target_sk, target_region, old_volume_id)
            log_step(job_id, f"Volume {device_name} : ancien volume {old_volume_id} détaché.")

        source_volume = source_volumes_by_id.get(source_volume_id, {})
        new_volume = octl.create_volume(
            target_ak, target_sk, target_region, snapshot_id, target_subregion,
            volume_type=source_volume.get("VolumeType"), iops=source_volume.get("Iops"),
            tags=source_volume.get("Tags", []),
        )
        new_volume_id = new_volume.get("VolumeId")
        log_step(job_id, f"Volume {device_name} : nouveau volume {new_volume_id} en cours de restauration...")
        _wait_volume_available(target_ak, target_sk, target_region, new_volume_id)
        octl.attach_volume(target_ak, target_sk, target_region, new_volume_id, target_vm_id, device_name)
        log_step(job_id, f"Volume {device_name} : {new_volume_id} attaché.")

        if old_volume_id:
            octl.delete_volume(target_ak, target_sk, target_region, old_volume_id)
            log_step(job_id, f"Volume {device_name} : ancien volume {old_volume_id} supprimé.")

        if device_name == root_device:
            _record_root_restore(plan_id, source_vm_id, snapshot_id, sandbox_id)

        restored_devices.add(device_name)

    for device_name in set(source_bdms_by_device) - set(target_bdms_by_device):
        source_bdm = source_bdms_by_device[device_name]
        source_volume_id = source_bdm.get("Bsu", {}).get("VolumeId")
        snapshot_id = snapshots_by_volume.get(source_volume_id)
        if snapshot_id is None:
            continue

        log_step(job_id, f"Volume {device_name} : nouveau côté source, restauration depuis le snapshot {snapshot_id}...")
        source_volume = source_volumes_by_id.get(source_volume_id, {})
        new_volume = octl.create_volume(
            target_ak, target_sk, target_region, snapshot_id, target_subregion,
            volume_type=source_volume.get("VolumeType"), iops=source_volume.get("Iops"),
            tags=source_volume.get("Tags", []),
        )
        new_volume_id = new_volume.get("VolumeId")
        _wait_volume_available(target_ak, target_sk, target_region, new_volume_id)
        octl.attach_volume(target_ak, target_sk, target_region, new_volume_id, target_vm_id, device_name)
        log_step(job_id, f"Volume {device_name} : {new_volume_id} attaché (nouveau device).")
        restored_devices.add(device_name)

    for device_name in set(target_bdms_by_device) - set(source_bdms_by_device):
        old_volume_id = target_bdms_by_device[device_name].get("Bsu", {}).get("VolumeId")
        if not old_volume_id:
            continue
        log_step(job_id, f"Volume {device_name} : retiré côté source, détachement et suppression de {old_volume_id}...")
        octl.detach_volume(target_ak, target_sk, target_region, old_volume_id)
        octl.delete_volume(target_ak, target_sk, target_region, old_volume_id)
        log_step(job_id, f"Volume {device_name} : {old_volume_id} supprimé côté cible.")

    return restored_devices


def restore_vm(
    plan, target_ak: str, target_sk: str, target_region: str, source_vm: dict, source_subnet: dict,
    source_volumes_by_id: dict, snapshots_by_volume: dict, job_id: int,
    target_vpc_id: str | None = None, sandbox_id: int | None = None, image_override: str | None = None,
) -> str:
    """Point d'entrée appelé par scripts/run_plan.py après un snapshot
    réussi : crée la VM cible si besoin, puis remplace ses volumes par des
    volumes restaurés depuis les snapshots qui viennent d'être créés.
    Retourne un message récapitulatif ; lève RestoreError en cas d'échec.
    `job_id` sert à journaliser chaque étape (voir app/jobs.py, log_step),
    affiché en direct sur la page Suivi et la page du plan.

    `target_vpc_id`/`sandbox_id` sont utilisés ensemble par
    scripts/run_sandbox.py pour restaurer une VM dans un VPC sandbox
    (indépendant du VPC de PRA persistant du plan) : le mapping VM
    source -> VM sandbox est alors mémorisé dans `sandbox_vm_targets`
    plutôt que `vm_targets`. Sans ces paramètres, comportement inchangé
    (VPC et mapping persistants du plan).

    `image_override` : OMI cible pour CETTE VM (résolue par l'appelant
    depuis `plan_vpcs.vm_image_overrides` — un plan pouvant avoir plusieurs
    VPC, voir app/plan_vpcs.py, l'override est propre au VPC de cette VM,
    pas au plan) — `None` si aucune n'est configurée, l'OMI source est
    alors reprise telle quelle."""
    effective_target_vpc_id = target_vpc_id or plan["target_vpc_id"]
    if not effective_target_vpc_id:
        raise RestoreError(
            "VPC cible non créé pour ce plan — crée-le depuis la page Modifier avant d'activer la restauration."
        )

    source_vm_id = source_vm["VmId"]
    target_vm_id = _get_target_vm_id(plan["id"], source_vm_id, sandbox_id)

    if target_vm_id is None:
        log_step(job_id, "Aucune VM cible existante — création d'une nouvelle VM cible.")
        image_id = image_override or source_vm["ImageId"]
        target_vm_id, root_snapshot_id = _create_target_vm(
            plan, target_ak, target_sk, target_region, source_vm, source_subnet,
            source_volumes_by_id, snapshots_by_volume, image_id, job_id, effective_target_vpc_id,
        )
        _save_target_vm_id(plan["id"], source_vm_id, target_vm_id, sandbox_id)
        _record_root_restore(plan["id"], source_vm_id, root_snapshot_id, sandbox_id)
        restored_count = 1
    else:
        log_step(job_id, f"VM cible existante {target_vm_id} — mise à jour de ses volumes.")
        restored_devices = _refresh_target_vm(
            target_ak, target_sk, target_region, target_vm_id, source_vm,
            source_volumes_by_id, snapshots_by_volume, job_id, plan["id"], source_vm_id, sandbox_id,
        )
        restored_count = len(restored_devices)

    return f"VM cible {target_vm_id} : {restored_count} volume(s) restauré(s)."
