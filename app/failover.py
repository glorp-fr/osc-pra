"""Orchestration de la bascule PRA (Activation / Test) et des sandbox :
détermine l'ordre de démarrage des VM cible, bascule ou alloue les EIP,
reconstruit la NAT Gateway côté cible. Utilisé par scripts/run_bascule.py,
scripts/end_test.py et scripts/run_sandbox.py — voir CLAUDE.MD, section
« Bascule PRA ».

Les fonctions de ce module ne touchent jamais au compte source en mode
"test" (VMs/EIP/NAT toutes neuves côté cible) ; en mode "activation", elles
réutilisent délibérément les EIP de production et suppriment la NAT
Gateway source pour récupérer son EIP (voir assign_nat) — c'est la seule
partie de l'application qui modifie des ressources côté source.
"""
import json

from app import octl, restore, target
from app.db import get_connection
from app.jobs import log_step


def resolve_start_order(plan, selected_vms: list[str]) -> list[str]:
    """Trie les VM sélectionnées selon plans.vm_restart_order (JSON
    {VmId: ordre}, la plus petite valeur démarrée en premier) ; une VM sans
    ordre explicite démarre après toutes celles qui en ont un, par ordre
    alphabétique entre elles."""
    order = json.loads(plan["vm_restart_order"] or "{}")

    def sort_key(vm_id):
        if vm_id in order:
            return (0, order[vm_id], vm_id)
        return (1, 0, vm_id)

    return sorted(selected_vms, key=sort_key)


def start_vms_in_order(
    target_ak: str, target_sk: str, target_region: str,
    vm_ids_ordered: list[str], target_vm_id_by_source: dict, job_id: int,
) -> None:
    """Démarre les VM cible dans l'ordre donné et attend chacune `running`
    avant de passer à la suivante. `target_vm_id_by_source` :
    {source_vm_id: target_vm_id}. L'échec d'une VM est journalisé mais
    n'interrompt pas le démarrage des suivantes — en bascule réelle, une VM
    en échec ne doit pas empêcher les autres de démarrer."""
    for source_vm_id in vm_ids_ordered:
        target_vm_id = target_vm_id_by_source.get(source_vm_id)
        if not target_vm_id:
            log_step(job_id, f"VM {source_vm_id} : pas de VM cible connue, ignorée.", level="error")
            continue
        log_step(job_id, f"Démarrage de la VM cible {target_vm_id} (source {source_vm_id})...")
        try:
            octl.start_vm(target_ak, target_sk, target_region, target_vm_id)
            restore._wait_vm_state(target_ak, target_sk, target_region, target_vm_id, "running")
            log_step(job_id, f"VM cible {target_vm_id} démarrée.")
        except (octl.OctlError, restore.RestoreError) as exc:
            log_step(job_id, f"Échec du démarrage de {target_vm_id} : {exc}", level="error")


def assign_eips(
    target_ak: str, target_sk: str, target_region: str,
    source_ak: str, source_sk: str, source_region: str,
    source_vm_ids: list[str], target_vm_id_by_source: dict, mode: str, job_id: int,
) -> dict:
    """Attribue une EIP à chaque VM cible dont la VM source en a une.
    mode="activation" : réutilise l'EIP source (LinkPublicIp avec
    AllowRelink, pas besoin de la détacher explicitement de la VM source au
    préalable). mode="test" : alloue une EIP neuve, ne touche jamais à la
    prod. Retourne {source_vm_id: {public_ip, public_ip_id, link_id,
    reused}}."""
    vm_eips = {}
    source_vms_by_id = {vm.get("VmId"): vm for vm in octl.list_vms(source_ak, source_sk, source_region)}

    source_ips_by_ip = {}
    if mode == "activation":
        for p in octl.list_public_ips(source_ak, source_sk, source_region, source_vm_ids):
            if p.get("PublicIp"):
                source_ips_by_ip[p["PublicIp"]] = p.get("PublicIpId")

    for source_vm_id in source_vm_ids:
        source_vm = source_vms_by_id.get(source_vm_id)
        source_public_ip = source_vm.get("PublicIp") if source_vm else None
        target_vm_id = target_vm_id_by_source.get(source_vm_id)
        if not source_public_ip or not target_vm_id:
            continue

        try:
            if mode == "activation":
                public_ip_id = source_ips_by_ip.get(source_public_ip)
                if not public_ip_id:
                    log_step(job_id, f"EIP {source_public_ip} (VM {source_vm_id}) : PublicIpId introuvable, ignorée.", level="error")
                    continue
                log_step(job_id, f"Rattachement de l'EIP de production {source_public_ip} à la VM cible {target_vm_id}...")
                link = octl.link_public_ip(target_ak, target_sk, target_region, public_ip_id, target_vm_id, allow_relink=True)
                vm_eips[source_vm_id] = {
                    "public_ip": source_public_ip, "public_ip_id": public_ip_id,
                    "link_id": link.get("LinkPublicIpId"), "reused": True,
                }
                log_step(job_id, f"EIP {source_public_ip} rattachée à {target_vm_id}.")
            else:
                log_step(job_id, f"Allocation d'une EIP de test pour la VM cible {target_vm_id}...")
                new_ip = octl.create_public_ip(target_ak, target_sk, target_region)
                link = octl.link_public_ip(target_ak, target_sk, target_region, new_ip.get("PublicIpId"), target_vm_id, allow_relink=False)
                vm_eips[source_vm_id] = {
                    "public_ip": new_ip.get("PublicIp"), "public_ip_id": new_ip.get("PublicIpId"),
                    "link_id": link.get("LinkPublicIpId"), "reused": False,
                }
                log_step(job_id, f"EIP de test {new_ip.get('PublicIp')} rattachée à {target_vm_id}.")

            # Sans ce tag, Outscale détache l'EIP à chaque arrêt/démarrage
            # de la VM (utile pour un sandbox : "éteindre" ne doit pas faire
            # perdre son EIP, voir scripts/run_sandbox.py action stop/start).
            octl.create_tags(target_ak, target_sk, target_region, target_vm_id, [{"Key": "osc.fcu.eip.auto-attach", "Value": "true"}])
        except octl.OctlError as exc:
            log_step(job_id, f"Échec de l'attribution d'EIP pour la VM cible {target_vm_id} : {exc}", level="error")

    return vm_eips


def assign_nat(
    source_ak: str, source_sk: str, source_region: str, source_vpc_id: str,
    target_ak: str, target_sk: str, target_region: str, target_vpc_id: str,
    mode: str, job_id: int,
) -> dict:
    """Reconstruit la NAT Gateway côté cible à partir de celle du VPC
    source (si elle existe). mode="activation" : tente de réutiliser l'EIP
    de la NAT source — il faut d'abord la supprimer côté source, l'API
    n'autorisant pas de changer l'EIP d'une NAT Gateway existante ; si la
    création échoue derrière, alloue une EIP neuve à la place (`reused`
    passe à False, à afficher bien en évidence). mode="test" : alloue
    toujours une EIP neuve, ne touche jamais à la NAT source. Recrée aussi
    la route vers cette NAT dans la table de routage cible correspondante
    (ignorée par target.py::_sync_route_tables au resync normal). Ne lève
    pas d'exception : une NAT en échec ne doit pas bloquer le reste de la
    bascule, l'erreur est journalisée et renvoyée dans le résultat."""
    source_nats = [
        n for n in octl.list_nat_services(source_ak, source_sk, source_region, net_id=source_vpc_id)
        if n.get("State") in ("pending", "available")
    ]
    if not source_nats:
        log_step(job_id, "Aucune NAT Gateway source active — rien à reconstruire côté cible.")
        return {}
    source_nat = source_nats[0]
    source_nat_id = source_nat.get("NatServiceId")
    source_public_ips = source_nat.get("PublicIps") or []
    source_public_ip_id = source_public_ips[0].get("PublicIpId") if source_public_ips else None
    source_public_ip = source_public_ips[0].get("PublicIp") if source_public_ips else None

    source_subnets = {s.get("SubnetId"): s for s in octl.list_subnets(source_ak, source_sk, source_region)}
    source_nat_subnet = source_subnets.get(source_nat.get("SubnetId"))
    subnet_name = target.tag_name(source_nat_subnet, source_nat.get("SubnetId")) if source_nat_subnet else None
    target_subnet = next(
        (
            s for s in octl.list_subnets(target_ak, target_sk, target_region)
            if s.get("NetId") == target_vpc_id and target.tag_name(s, s.get("SubnetId")) == subnet_name
        ),
        None,
    ) if subnet_name else None
    if target_subnet is None:
        log_step(job_id, f"Subnet cible correspondant à la NAT source ({subnet_name}) introuvable — NAT non recréée.", level="error")
        return {"error": "Subnet cible introuvable pour la NAT Gateway."}
    target_subnet_id = target_subnet["SubnetId"]

    reused = False
    if mode == "activation" and source_public_ip_id:
        try:
            log_step(job_id, f"Suppression de la NAT Gateway source {source_nat_id} pour libérer son EIP {source_public_ip}...")
            octl.delete_nat_service(source_ak, source_sk, source_region, source_nat_id)
            log_step(job_id, f"Recréation de la NAT Gateway côté cible avec l'EIP {source_public_ip}...")
            new_nat = octl.create_nat_service(target_ak, target_sk, target_region, target_subnet_id, source_public_ip_id)
            new_public_ip_id, new_public_ip, reused = source_public_ip_id, source_public_ip, True
        except octl.OctlError as exc:
            log_step(
                job_id,
                f"Échec de la reconstruction de la NAT Gateway avec l'ancienne EIP ({exc}) — allocation d'une nouvelle EIP.",
                level="error",
            )
            new_ip = octl.create_public_ip(target_ak, target_sk, target_region)
            new_public_ip_id, new_public_ip = new_ip.get("PublicIpId"), new_ip.get("PublicIp")
            new_nat = octl.create_nat_service(target_ak, target_sk, target_region, target_subnet_id, new_public_ip_id)
    else:
        log_step(job_id, "Allocation d'une EIP de test pour la NAT Gateway cible...")
        new_ip = octl.create_public_ip(target_ak, target_sk, target_region)
        new_public_ip_id, new_public_ip = new_ip.get("PublicIpId"), new_ip.get("PublicIp")
        new_nat = octl.create_nat_service(target_ak, target_sk, target_region, target_subnet_id, new_public_ip_id)

    new_nat_id = new_nat.get("NatServiceId")
    log_step(job_id, f"NAT Gateway cible {new_nat_id} créée avec l'EIP {new_public_ip}{' (identique à la prod)' if reused else ' (nouvelle)'}.")

    _recreate_nat_route(source_ak, source_sk, source_region, source_vpc_id, target_ak, target_sk, target_region, target_vpc_id, new_nat_id, job_id)

    return {"nat_service_id": new_nat_id, "public_ip": new_public_ip, "public_ip_id": new_public_ip_id, "reused": reused}


def _recreate_nat_route(
    source_ak, source_sk, source_region, source_vpc_id,
    target_ak, target_sk, target_region, target_vpc_id, nat_service_id, job_id,
) -> None:
    """Retrouve, par nom (même appariement que target.py::_sync_route_tables),
    la table de routage cible correspondant à celle qui pointait vers la NAT
    source, et y crée la route manquante vers la nouvelle NAT Gateway."""
    source_rts = [rt for rt in octl.list_route_tables(source_ak, source_sk, source_region) if rt.get("NetId") == source_vpc_id]
    target_rts = {
        target.tag_name(rt, ""): rt
        for rt in octl.list_route_tables(target_ak, target_sk, target_region) if rt.get("NetId") == target_vpc_id
    }

    for rt in source_rts:
        nat_routes = [r for r in rt.get("Routes", []) if r.get("NatServiceId")]
        if not nat_routes:
            continue
        name = target.tag_name(rt, "")
        target_rt = target_rts.get(name)
        if target_rt is None:
            log_step(job_id, f"Table de routage cible « {name} » introuvable — route NAT non recréée.", level="error")
            continue
        for route in nat_routes:
            destination = route.get("DestinationIpRange")
            existing = {r.get("DestinationIpRange") for r in target_rt.get("Routes", [])}
            if destination in existing:
                continue
            octl.create_route(
                target_ak, target_sk, target_region, target_rt.get("RouteTableId"), destination,
                nat_service_id=nat_service_id,
            )
            log_step(job_id, f"Route {destination} vers la NAT Gateway {nat_service_id} créée dans la table « {name} ».")


def _target_vm_ids_by_source(plan_id: int) -> dict:
    conn = get_connection()
    rows = conn.execute("SELECT source_vm_id, target_vm_id FROM vm_targets WHERE plan_id = ?", (plan_id,)).fetchall()
    conn.close()
    return {row["source_vm_id"]: row["target_vm_id"] for row in rows}


def end_test(plan, target_ak: str, target_sk: str, target_region: str, job_id: int) -> None:
    """Démonte les EIP et la/les NAT Gateway de test, stoppe les VM cible —
    utilisé par scripts/end_test.py (bouton « Terminer le test »). Ne
    s'applique jamais à une Activation réelle (EIP/NAT de prod jamais
    démontées automatiquement). Un plan à plusieurs VPC (voir
    app/plan_vpcs.py) a une NAT Gateway par VPC : `state["nats"]` est une
    liste (une entrée par VPC créée par scripts/run_bascule.py) ; l'ancien
    format `state["nat"]` (objet unique, plans en `test_en_cours` au moment
    d'une mise à jour vers la version multi-VPC) est aussi lu pour ne pas
    laisser un test déjà en cours bloqué."""
    state = json.loads(plan["failover_state"] or "{}")

    for source_vm_id, target_vm_id in _target_vm_ids_by_source(plan["id"]).items():
        log_step(job_id, f"Arrêt de la VM cible {target_vm_id}...")
        try:
            octl.stop_vm(target_ak, target_sk, target_region, target_vm_id)
        except octl.OctlError as exc:
            log_step(job_id, f"Échec de l'arrêt de {target_vm_id} : {exc}", level="error")

    for source_vm_id, eip in state.get("vm_eips", {}).items():
        if eip.get("link_id"):
            try:
                octl.unlink_public_ip(target_ak, target_sk, target_region, eip["link_id"])
            except octl.OctlError as exc:
                log_step(job_id, f"Échec du détachement de l'EIP {eip.get('public_ip')} : {exc}", level="error")
        if eip.get("public_ip_id") and not eip.get("reused"):
            try:
                octl.delete_public_ip(target_ak, target_sk, target_region, eip["public_ip_id"])
            except octl.OctlError as exc:
                log_step(job_id, f"Échec de la suppression de l'EIP {eip.get('public_ip')} : {exc}", level="error")

    nats = state.get("nats") or ([state["nat"]] if state.get("nat") else [])
    for nat in nats:
        if nat.get("nat_service_id"):
            try:
                octl.delete_nat_service(target_ak, target_sk, target_region, nat["nat_service_id"])
            except octl.OctlError as exc:
                log_step(job_id, f"Échec de la suppression de la NAT Gateway de test : {exc}", level="error")
            if nat.get("public_ip_id") and not nat.get("reused"):
                try:
                    octl.delete_public_ip(target_ak, target_sk, target_region, nat["public_ip_id"])
                except octl.OctlError as exc:
                    log_step(job_id, f"Échec de la suppression de l'EIP NAT de test : {exc}", level="error")

    log_step(job_id, "Test terminé, VPC de PRA remis en réplique froide.")
