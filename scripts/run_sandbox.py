#!/usr/bin/env python3
"""Cycle de vie d'un sandbox : clone un VPC indépendant du VPC de PRA
persistant du plan et le construit comme un Test de PRA (VM restaurées
depuis les derniers snapshots disponibles, démarrées, EIP/NAT neuves,
jamais de modification côté source) — pour expérimenter sans jamais
toucher au VPC de PRA officiel tenu à jour par la sync planifiée. Lancé en
arrière-plan depuis app/routers/admin.py (page Sandbox).

Actions :
- create : construction initiale (VPC, réseau, VM, EIP, NAT).
- start/stop : VM du sandbox uniquement (VPC/EIP/NAT conservés — un tag
  osc.fcu.eip.auto-attach posé par app/failover.py::assign_eips évite que
  l'EIP se détache au passage stopped/running).
- delete : démontage complet (VM, EIP, NAT, réseau, VPC), voir
  app/target.py::delete_target_vpc (paramétré par sandbox_id).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import failover, octl, restore  # noqa: E402
from app.crypto import decrypt  # noqa: E402
from app.db import get_connection  # noqa: E402
from app.jobs import finish_job, log_step, start_job  # noqa: E402
from app.target import delete_target_vpc, resolve_target_credentials, sync_target_network  # noqa: E402


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


def create(sandbox_id: int) -> None:
    sandbox, plan = _load(sandbox_id)
    if sandbox is None or plan is None:
        return
    job_id = start_job("sandbox", plan["id"], plan["name"])
    _set_job(sandbox_id, job_id)

    def fail(message: str) -> None:
        conn = get_connection()
        conn.execute("UPDATE sandboxes SET status = 'error', error = ? WHERE id = ?", (message, sandbox_id))
        conn.commit()
        conn.close()
        finish_job(job_id, "error", message)

    if not octl.is_available():
        fail("octl n'est pas installé sur ce serveur.")
        return
    if not plan["source_vpc_id"]:
        fail("VPC source non sélectionné pour ce plan.")
        return

    target_ak, target_sk, target_region, target_error = resolve_target_credentials(plan)
    if target_error:
        fail(target_error)
        return

    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""
    selected_vms = json.loads(plan["selected_vms"] or "[]")
    if not selected_vms:
        fail("Aucune VM sélectionnée pour ce plan.")
        return

    try:
        log_step(job_id, "Création du VPC du sandbox...")
        source_vpcs = octl.list_vpcs(plan["source_ak"], source_sk, plan["source_region"])
        source_vpc = next((v for v in source_vpcs if v.get("NetId") == plan["source_vpc_id"]), None)
        if source_vpc is None:
            raise octl.OctlError("VPC source introuvable sur le compte (supprimé ?).")
        name_tag = next(
            (t["Value"] for t in source_vpc.get("Tags", []) if t.get("Key") == "Name"), plan["name"]
        )
        sandbox_vpc = octl.create_vpc(
            target_ak, target_sk, target_region, source_vpc["IpRange"], f"{name_tag}-sandbox-{sandbox_id}",
            tags=source_vpc.get("Tags", []),
        )
        sandbox_vpc_id = sandbox_vpc.get("NetId")
        if not sandbox_vpc_id:
            raise octl.OctlError("La création du VPC du sandbox n'a pas renvoyé d'identifiant.")
        conn = get_connection()
        conn.execute("UPDATE sandboxes SET vpc_id = ? WHERE id = ?", (sandbox_vpc_id, sandbox_id))
        conn.commit()
        conn.close()
        log_step(job_id, f"VPC {sandbox_vpc_id} créé.")

        log_step(job_id, "Resynchronisation du réseau (subnets, security groups, internet service, routes)...")
        sync_target_network(plan, target_ak, target_sk, target_region, sandbox_vpc_id)
        log_step(job_id, "Réseau du sandbox prêt.")

        source_vms_by_id = {vm.get("VmId"): vm for vm in octl.list_vms(plan["source_ak"], source_sk, plan["source_region"])}
        source_subnets_by_id = {s.get("SubnetId"): s for s in octl.list_subnets(plan["source_ak"], source_sk, plan["source_region"])}

        restored_vms = []
        for vm_id in selected_vms:
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

        vm_eips = failover.assign_eips(
            target_ak, target_sk, target_region, plan["source_ak"], source_sk, plan["source_region"],
            restored_vms, target_vm_id_by_source, "test", job_id,
        )
        nat = failover.assign_nat(
            plan["source_ak"], source_sk, plan["source_region"], plan["source_vpc_id"],
            target_ak, target_sk, target_region, sandbox_vpc_id, "test", job_id,
        )
    except (octl.OctlError, restore.RestoreError) as exc:
        fail(str(exc))
        return

    state = {"vm_eips": vm_eips, "nat": nat}
    conn = get_connection()
    conn.execute("UPDATE sandboxes SET status = 'running', state = ?, error = NULL WHERE id = ?", (json.dumps(state), sandbox_id))
    conn.commit()
    conn.close()
    finish_job(job_id, "success", f"Sandbox prêt : {len(restored_vms)} VM(s) démarrée(s) dans {sandbox_vpc_id}.")


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

    nat = state.get("nat") or {}
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

    if sandbox["vpc_id"]:
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
