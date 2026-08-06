#!/usr/bin/env python3
"""Cycle de vie d'un sandbox : clone chaque VPC du plan (voir
app/plan_vpcs.py — un plan peut en avoir plusieurs) dans un VPC indépendant
du VPC de PRA persistant, et le construit comme un Test de PRA (VM
restaurées depuis les derniers snapshots disponibles, démarrées, EIP/NAT
neuves, jamais de modification côté source, Net Peering recréé entre les
VPC du sandbox si les VPC source correspondants sont peerés) — pour
expérimenter sans jamais toucher au VPC de PRA officiel tenu à jour par la
sync planifiée. Lancé en arrière-plan depuis app/routers/admin.py (page
Sandbox).

Actions :
- create : construction initiale (VPC, réseau, VM, EIP, NAT — un jeu par
  VPC du plan).
- start/stop : VM du sandbox uniquement (VPC/EIP/NAT conservés — un tag
  osc.fcu.eip.auto-attach posé par app/failover.py::assign_eips évite que
  l'EIP se détache au passage stopped/running). Déjà VPC-agnostiques (VM de
  tous les VPC du sandbox traitées en une fois), aucun changement lié au
  multi-VPC nécessaire ici.
- delete : démontage complet (VM, EIP, NAT, réseau, VPC) de chaque VPC du
  sandbox, voir app/target.py::delete_target_vpc (paramétré par
  sandbox_id). Un sandbox créé avant la prise en charge du multi-VPC (pas
  de ligne `sandbox_vpcs`) reste géré via l'ancienne colonne
  `sandboxes.vpc_id`/`state` — jamais recréé sous la nouvelle forme.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cron, failover, octl, restore, vpc_registry  # noqa: E402
from app.crypto import decrypt  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.jobs import finish_job, log_step, start_job  # noqa: E402
from app.plan_vpcs import get_plan_vpc, list_plan_vpcs, selected_vms_of, vm_image_overrides_of  # noqa: E402
from app.target import delete_target_vpc, resolve_target_credentials, sync_net_peerings, sync_target_network  # noqa: E402


def _load(sandbox_id: int):
    conn = get_connection()
    sandbox = conn.execute("SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,)).fetchone()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (sandbox["plan_id"],)).fetchone() if sandbox else None
    conn.close()
    return sandbox, plan


def _set_job(sandbox_id: int, job_id: int) -> None:
    conn = get_connection()
    conn.execute("UPDATE sandboxes SET job_id = ? WHERE id = ?", (job_id, sandbox_id))
    conn.commit()
    conn.close()


def _sandbox_vpcs(sandbox_id: int) -> list:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM sandbox_vpcs WHERE sandbox_id = ?", (sandbox_id,)).fetchall()
    conn.close()
    return rows


def create(sandbox_id: int) -> None:
    sandbox, plan = _load(sandbox_id)
    if sandbox is None or plan is None:
        return
    job_id = start_job("sandbox", plan["id"], plan["name"])
    _set_job(sandbox_id, job_id)

    def fail(message: str, state: dict | None = None) -> None:
        conn = get_connection()
        if state is not None:
            conn.execute(
                "UPDATE sandboxes SET status = 'error', error = ?, state = ? WHERE id = ?",
                (message, json.dumps(state), sandbox_id),
            )
        else:
            conn.execute("UPDATE sandboxes SET status = 'error', error = ? WHERE id = ?", (message, sandbox_id))
        conn.commit()
        conn.close()
        finish_job(job_id, "error", message)

    if not octl.is_available():
        fail("octl n'est pas installé sur ce serveur.")
        return

    plan_vpcs = [pv for pv in list_plan_vpcs(plan["id"]) if pv["source_vpc_id"] and selected_vms_of(pv)]
    if not plan_vpcs:
        fail("Aucun VPC avec au moins une VM sélectionnée pour ce plan.")
        return

    target_ak, target_sk, target_region, target_error = resolve_target_credentials(plan)
    if target_error:
        fail(target_error)
        return

    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""

    # Alimentés au fil de l'eau pendant le try (pas seulement à la toute
    # fin) : en cas d'échec en cours de route, ce qui a déjà été créé
    # (EIP/NAT) est quand même sauvé dans sandboxes.state pour que
    # delete() puisse le nettoyer plutôt que de laisser des ressources
    # orphelines dans le compte cible.
    vm_eips: dict = {}
    nats: list = []

    try:
        source_vpcs_by_id = {v.get("NetId"): v for v in octl.list_vpcs(plan["source_ak"], source_sk, plan["source_region"])}
        source_vms_by_id = {vm.get("VmId"): vm for vm in octl.list_vms(plan["source_ak"], source_sk, plan["source_region"])}
        source_subnets_by_id = {s.get("SubnetId"): s for s in octl.list_subnets(plan["source_ak"], source_sk, plan["source_region"])}

        sandbox_vpc_by_plan_vpc_id = {}
        for plan_vpc in plan_vpcs:
            source_vpc = source_vpcs_by_id.get(plan_vpc["source_vpc_id"])
            if source_vpc is None:
                log_step(job_id, f"VPC source {plan_vpc['source_vpc_id']} introuvable sur le compte, ignoré.", level="error")
                continue
            name_tag = next(
                (t["Value"] for t in source_vpc.get("Tags", []) if t.get("Key") == "Name"), plan["name"]
            )
            log_step(job_id, f"Création du VPC du sandbox pour {plan_vpc['source_vpc_id']}...")
            sandbox_vpc = octl.create_vpc(
                target_ak, target_sk, target_region, source_vpc["IpRange"],
                f"{name_tag}-sandbox-{sandbox_id}-{plan_vpc['id']}", tags=source_vpc.get("Tags", []),
            )
            sandbox_vpc_id = sandbox_vpc.get("NetId")
            if not sandbox_vpc_id:
                raise octl.OctlError("La création du VPC du sandbox n'a pas renvoyé d'identifiant.")

            conn = get_connection()
            conn.execute(
                "INSERT INTO sandbox_vpcs (sandbox_id, plan_vpc_id, vpc_id) VALUES (?, ?, ?)",
                (sandbox_id, plan_vpc["id"], sandbox_vpc_id),
            )
            conn.commit()
            conn.close()
            vpc_registry.record_created(
                sandbox_vpc_id, "sandbox", plan["id"], plan["name"],
                target_ak, target_region, sandbox["created_by"],
                plan_vpc_id=plan_vpc["id"], sandbox_id=sandbox_id,
            )
            try:
                cron.sync_crontab()  # (re)programme le rescan horaire du registre VPC dès la 1ère ligne
            except cron.CronError:
                pass
            sandbox_vpc_by_plan_vpc_id[plan_vpc["id"]] = sandbox_vpc_id
            log_step(job_id, f"VPC {sandbox_vpc_id} créé.")

        if not sandbox_vpc_by_plan_vpc_id:
            raise octl.OctlError("Aucun VPC du sandbox n'a pu être créé.")

        log_step(job_id, "Recréation des Net Peering entre les VPC du sandbox (si peerés côté source)...")
        pseudo_plan_vpcs = [
            {"source_vpc_id": pv["source_vpc_id"], "target_vpc_id": sandbox_vpc_by_plan_vpc_id[pv["id"]]}
            for pv in plan_vpcs if pv["id"] in sandbox_vpc_by_plan_vpc_id
        ]
        peering_target_id_by_source_id = sync_net_peerings(plan, pseudo_plan_vpcs, target_ak, target_sk, target_region)

        restored_vms = []
        for plan_vpc in plan_vpcs:
            sandbox_vpc_id = sandbox_vpc_by_plan_vpc_id.get(plan_vpc["id"])
            if sandbox_vpc_id is None:
                continue

            log_step(job_id, f"Resynchronisation du réseau du VPC {sandbox_vpc_id}...")
            sync_target_network(
                plan, target_ak, target_sk, target_region,
                plan_vpc["source_vpc_id"], sandbox_vpc_id, plan_vpc["target_subregion"] or "",
                peering_target_id_by_source_id,
            )

            for vm_id in selected_vms_of(plan_vpc):
                vm = source_vms_by_id.get(vm_id)
                if vm is None:
                    log_step(job_id, f"VM {vm_id} introuvable sur le compte source, ignorée.", level="error")
                    continue
                volume_ids = [
                    bdm["Bsu"]["VolumeId"] for bdm in vm.get("BlockDeviceMappings", [])
                    if bdm.get("Bsu", {}).get("VolumeId")
                ]
                snapshots_by_volume = {}
                for volume_id in volume_ids:
                    snapshots = octl.list_snapshots(plan["source_ak"], source_sk, plan["source_region"], volume_id)
                    snapshots.sort(key=lambda s: s.get("CreationDate", ""), reverse=True)
                    if snapshots:
                        snapshots_by_volume[volume_id] = snapshots[0].get("SnapshotId")
                if len(snapshots_by_volume) != len(volume_ids):
                    log_step(job_id, f"VM {vm_id} : snapshot manquant pour au moins un volume, ignorée.", level="error")
                    continue

                source_volumes_by_id = {v["VolumeId"]: v for v in octl.list_volumes(plan["source_ak"], source_sk, plan["source_region"], volume_ids)}
                source_subnet = source_subnets_by_id.get(vm.get("SubnetId"))
                if source_subnet is None:
                    log_step(job_id, f"VM {vm_id} : subnet source introuvable, ignorée.", level="error")
                    continue

                log_step(job_id, f"Restauration de la VM {vm_id} dans le sandbox...")
                restore.restore_vm(
                    plan, target_ak, target_sk, target_region, vm, source_subnet,
                    source_volumes_by_id, snapshots_by_volume, job_id,
                    target_vpc_id=sandbox_vpc_id, sandbox_id=sandbox_id,
                    image_override=vm_image_overrides_of(plan_vpc).get(vm_id),
                )
                restored_vms.append(vm_id)

        if not restored_vms:
            raise octl.OctlError("Aucune VM n'a pu être restaurée dans le sandbox.")

        conn = get_connection()
        rows = conn.execute(
            "SELECT source_vm_id, target_vm_id FROM sandbox_vm_targets WHERE sandbox_id = ?", (sandbox_id,)
        ).fetchall()
        conn.close()
        target_vm_id_by_source = {row["source_vm_id"]: row["target_vm_id"] for row in rows}

        order = failover.resolve_start_order(plan, restored_vms)
        failover.start_vms_in_order(target_ak, target_sk, target_region, order, target_vm_id_by_source, job_id)

        vm_eips.update(failover.assign_eips(
            target_ak, target_sk, target_region, plan["source_ak"], source_sk, plan["source_region"],
            restored_vms, target_vm_id_by_source, "test", job_id,
        ))
        for plan_vpc in plan_vpcs:
            sandbox_vpc_id = sandbox_vpc_by_plan_vpc_id.get(plan_vpc["id"])
            if sandbox_vpc_id is None:
                continue
            nat = failover.assign_nat(
                plan["source_ak"], source_sk, plan["source_region"], plan_vpc["source_vpc_id"],
                target_ak, target_sk, target_region, sandbox_vpc_id, "test", job_id,
            )
            if nat:
                nats.append({"plan_vpc_id": plan_vpc["id"], "vpc_id": sandbox_vpc_id, **nat})
    except (octl.OctlError, restore.RestoreError) as exc:
        fail(str(exc), state={"vm_eips": vm_eips, "nats": nats})
        return

    state = {"vm_eips": vm_eips, "nats": nats}
    conn = get_connection()
    conn.execute("UPDATE sandboxes SET status = 'running', state = ?, error = NULL WHERE id = ?", (json.dumps(state), sandbox_id))
    conn.commit()
    conn.close()
    finish_job(job_id, "success", f"Sandbox prêt : {len(restored_vms)} VM(s) démarrée(s) dans {len(sandbox_vpc_by_plan_vpc_id)} VPC.")


def start(sandbox_id: int) -> None:
    sandbox, plan = _load(sandbox_id)
    if sandbox is None or plan is None:
        return
    job_id = start_job("sandbox_demarrage", plan["id"], plan["name"])
    _set_job(sandbox_id, job_id)

    target_ak, target_sk, target_region, target_error = resolve_target_credentials(plan)
    if target_error:
        finish_job(job_id, "error", target_error)
        return

    conn = get_connection()
    rows = conn.execute(
        "SELECT source_vm_id, target_vm_id FROM sandbox_vm_targets WHERE sandbox_id = ?", (sandbox_id,)
    ).fetchall()
    conn.close()
    target_vm_id_by_source = {row["source_vm_id"]: row["target_vm_id"] for row in rows}

    order = failover.resolve_start_order(plan, list(target_vm_id_by_source.keys()))
    failover.start_vms_in_order(target_ak, target_sk, target_region, order, target_vm_id_by_source, job_id)

    conn = get_connection()
    conn.execute("UPDATE sandboxes SET status = 'running' WHERE id = ?", (sandbox_id,))
    conn.commit()
    conn.close()
    finish_job(job_id, "success", "Sandbox démarré.")


def stop(sandbox_id: int) -> None:
    sandbox, plan = _load(sandbox_id)
    if sandbox is None or plan is None:
        return
    job_id = start_job("sandbox_arret", plan["id"], plan["name"])
    _set_job(sandbox_id, job_id)

    target_ak, target_sk, target_region, target_error = resolve_target_credentials(plan)
    if target_error:
        finish_job(job_id, "error", target_error)
        return

    conn = get_connection()
    rows = conn.execute("SELECT target_vm_id FROM sandbox_vm_targets WHERE sandbox_id = ?", (sandbox_id,)).fetchall()
    conn.close()

    for row in rows:
        try:
            log_step(job_id, f"Arrêt de la VM {row['target_vm_id']}...")
            octl.stop_vm(target_ak, target_sk, target_region, row["target_vm_id"])
        except octl.OctlError as exc:
            log_step(job_id, f"Échec de l'arrêt de {row['target_vm_id']} : {exc}", level="error")

    conn = get_connection()
    conn.execute("UPDATE sandboxes SET status = 'stopped' WHERE id = ?", (sandbox_id,))
    conn.commit()
    conn.close()
    finish_job(job_id, "success", "Sandbox arrêté (VPC, EIP et NAT conservés).")


def delete(sandbox_id: int) -> None:
    sandbox, plan = _load(sandbox_id)
    if sandbox is None or plan is None:
        return
    job_id = start_job("sandbox_suppression", plan["id"], plan["name"])
    _set_job(sandbox_id, job_id)

    conn = get_connection()
    conn.execute("UPDATE sandboxes SET status = 'deleting' WHERE id = ?", (sandbox_id,))
    conn.commit()
    conn.close()

    target_ak, target_sk, target_region, target_error = resolve_target_credentials(plan)
    if target_error:
        finish_job(job_id, "error", target_error)
        return

    state = json.loads(sandbox["state"] or "{}")

    for source_vm_id, eip in state.get("vm_eips", {}).items():
        if eip.get("link_id"):
            try:
                octl.unlink_public_ip(target_ak, target_sk, target_region, eip["link_id"])
            except octl.OctlError:
                pass
        if eip.get("public_ip_id"):
            try:
                octl.delete_public_ip(target_ak, target_sk, target_region, eip["public_ip_id"])
            except octl.OctlError as exc:
                log_step(job_id, f"Échec de la suppression de l'EIP {eip.get('public_ip')} : {exc}", level="error")

    # Un sandbox à plusieurs VPC (voir app/plan_vpcs.py) a une NAT Gateway
    # par VPC : state["nats"] est une liste ; l'ancien format state["nat"]
    # (objet unique, sandbox créé avant la prise en charge du multi-VPC)
    # est aussi lu pour ne pas laisser un sandbox déjà en cours bloqué.
    nats = state.get("nats") or ([state["nat"]] if state.get("nat") else [])
    for nat in nats:
        if nat.get("nat_service_id"):
            try:
                octl.delete_nat_service(target_ak, target_sk, target_region, nat["nat_service_id"])
            except octl.OctlError as exc:
                log_step(job_id, f"Échec de la suppression de la NAT Gateway : {exc}", level="error")
            if nat.get("public_ip_id"):
                try:
                    octl.delete_public_ip(target_ak, target_sk, target_region, nat["public_ip_id"])
                except octl.OctlError:
                    pass

    sandbox_vpcs = _sandbox_vpcs(sandbox_id)
    if sandbox_vpcs:
        for sandbox_vpc in sandbox_vpcs:
            plan_vpc = get_plan_vpc(sandbox_vpc["plan_vpc_id"])
            vm_ids = selected_vms_of(plan_vpc) if plan_vpc else None
            log_step(job_id, f"Suppression du VPC {sandbox_vpc['vpc_id']} et de ses ressources...")
            result = delete_target_vpc(
                plan, target_ak, target_sk, target_region, sandbox_vpc["vpc_id"],
                sandbox_id=sandbox_id, vm_ids=vm_ids,
            )
            for error in result["errors"]:
                log_step(job_id, error, level="error")
            log_step(
                job_id,
                f"VPC {sandbox_vpc['vpc_id']} : {result['vms_deleted']}/{result['vms_total']} VM(s), "
                f"{result['sgs_deleted']} security group(s), {result['route_tables_deleted']} table(s) de routage, "
                f"{result['subnets_deleted']} subnet(s) supprimé(s) — VPC {'supprimé' if result['vpc_deleted'] else 'non supprimé'}.",
            )
    elif sandbox["vpc_id"]:
        # Sandbox créé avant la prise en charge du multi-VPC (pas de ligne
        # sandbox_vpcs) — même comportement qu'avant, sur l'ancienne colonne.
        log_step(job_id, f"Suppression du VPC {sandbox['vpc_id']} et de ses ressources...")
        result = delete_target_vpc(plan, target_ak, target_sk, target_region, sandbox["vpc_id"], sandbox_id=sandbox_id)
        for error in result["errors"]:
            log_step(job_id, error, level="error")
        log_step(
            job_id,
            f"VPC : {result['vms_deleted']}/{result['vms_total']} VM(s), {result['sgs_deleted']} security group(s), "
            f"{result['route_tables_deleted']} table(s) de routage, {result['subnets_deleted']} subnet(s) supprimé(s) — "
            f"VPC {'supprimé' if result['vpc_deleted'] else 'non supprimé'}.",
        )

    conn = get_connection()
    conn.execute("DELETE FROM sandbox_vm_targets WHERE sandbox_id = ?", (sandbox_id,))
    conn.execute("DELETE FROM sandbox_vpcs WHERE sandbox_id = ?", (sandbox_id,))
    conn.execute("UPDATE sandboxes SET status = 'deleted' WHERE id = ?", (sandbox_id,))
    conn.commit()
    conn.close()
    finish_job(job_id, "success", "Sandbox supprimé.")


ACTIONS = {"create": create, "start": start, "stop": stop, "delete": delete}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in ACTIONS:
        print(f"Usage: run_sandbox.py <sandbox_id> <{'|'.join(ACTIONS)}>", file=sys.stderr)
        sys.exit(1)
    ACTIONS[sys.argv[2]](int(sys.argv[1]))
