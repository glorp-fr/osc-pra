"""Résolution des identifiants du compte cible et utilitaires partagés entre
la création du VPC cible, la page Visualiser (resynchronisation des
ressources réseau) et la restauration des BSU sur la VM cible (voir
restore.py). Un plan peut porter plusieurs VPC (voir app/plan_vpcs.py) :
les fonctions ci-dessous opèrent sur UN VPC (source_vpc_id/target_vpc_id
explicites), appelées une fois par VPC du plan par l'appelant
(scripts/run_plan.py, scripts/run_sandbox.py, app/routers/admin.py) —
seule sync_net_peerings est plan-level (elle a besoin de tous les VPC
cible déjà créés pour apparier les paires)."""
from app import octl, vpc_registry
from app.crypto import decrypt
from app.db import get_connection

# Ressources volontairement exclues du resync : reprises seulement à la
# bascule vers le PRA (voir restore.py), pas à chaque cycle de
# resynchronisation réseau.
#   - EIP (PublicIp) : une VM avec IP publique récupère la sienne (même
#     région) ou une nouvelle (autre région) au moment de la bascule.
#   - NAT Gateway (NatService) : dépend d'une EIP, créée au même moment.
# En conséquence, les routes qui pointent vers une NAT Gateway ou une
# instance NAT (VmId/NicId) ne sont pas non plus répliquées ici (ressources
# elles-mêmes hors scope). Les Net Peering, eux, SONT répliqués (voir
# sync_net_peerings et le paramètre peering_target_id_by_source_id de
# _sync_route_tables) — un plan à plusieurs VPC peut donc reprendre le
# peering existant entre eux côté source.


def resolve_target_credentials(plan) -> tuple[str, str, str, str | None]:
    """En 'même région', le compte cible est le compte source ; en 'autre
    région', ce sont les AK/SK dédiés du plan (même logique que la création
    du VPC cible)."""
    if plan["target_type"] == "autre_region":
        target_ak = plan["target_ak"]
        target_sk = decrypt(plan["target_sk_encrypted"]) if plan["target_sk_encrypted"] else ""
        target_region = plan["target_region"]
        if not (target_ak and target_sk and target_region):
            return "", "", "", "AK/SK et région du compte cible requis pour ce plan."
        return target_ak, target_sk, target_region, None

    target_ak = plan["source_ak"]
    target_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""
    target_region = plan["source_region"]
    if not (target_ak and target_sk and target_region):
        return "", "", "", "AK/SK et région source requis pour ce plan."
    return target_ak, target_sk, target_region, None


def tag_name(resource: dict, fallback: str) -> str:
    return next((t["Value"] for t in resource.get("Tags", []) if t.get("Key") == "Name"), fallback)


RESOURCE_COUNT_TYPES = ("subnets", "security_groups", "route_tables", "internet_services")


def count_vpc_resources(ak: str, sk: str, region: str, vpc_id: str) -> dict:
    """Dénombre, par type, les ressources réseau d'un VPC — utilisé pour
    comparer le VPC source et le VPC cible d'un plan (voir app/resource_scan.py
    et la page Visualiser). Lève octl.OctlError en cas d'échec d'un appel."""
    return {
        "subnets": len([s for s in octl.list_subnets(ak, sk, region) if s.get("NetId") == vpc_id]),
        "security_groups": len([g for g in octl.list_security_groups(ak, sk, region) if g.get("NetId") == vpc_id]),
        "route_tables": len([rt for rt in octl.list_route_tables(ak, sk, region) if rt.get("NetId") == vpc_id]),
        "internet_services": len([i for i in octl.list_internet_services(ak, sk, region) if i.get("NetId") == vpc_id]),
    }


def _is_main_route_table(route_table: dict) -> bool:
    return any(link.get("Main") for link in route_table.get("LinkRouteTables", []))


def _route_table_subnet_id(route_table: dict) -> str | None:
    for link in route_table.get("LinkRouteTables", []):
        if link.get("SubnetId"):
            return link["SubnetId"]
    return None


def _route_table_key(route_table: dict, subnet_names_by_id: dict) -> str | None:
    """Clé stable pour apparier une route table secondaire d'un cycle de
    resync à l'autre. Le tag Name est prioritaire ; à défaut, le nom du
    subnet auquel elle est rattachée (stable, contrairement à l'ID de la
    route table elle-même, différent côté cible) ; à défaut, si elle porte
    au moins une route significative (au-delà de la route locale posée
    automatiquement à la création), une clé dérivée de ces routes — ex. la
    route table privée d'une NAT Gateway, jamais nommée en pratique mais
    dont la route NAT (ignorée ici, reprise seulement à la bascule) doit
    pouvoir être rattachée à une route table cible existante plus tard.
    Renvoie None si rien de tout ça n'est disponible : la table est alors
    ignorée plutôt que recréée en double à chaque cycle."""
    name = tag_name(route_table, "")
    if name:
        return name

    subnet_id = _route_table_subnet_id(route_table)
    subnet_name = subnet_names_by_id.get(subnet_id) if subnet_id else None
    if subnet_name:
        return f"__subnet__:{subnet_name}"

    significant_routes = [r for r in route_table.get("Routes", []) if r.get("GatewayId") != "local"]
    if significant_routes:
        return "__routes__:" + "|".join(sorted(
            f"{r.get('DestinationIpRange')}>"
            f"{r.get('GatewayId') or r.get('NatServiceId') or r.get('NetPeeringId') or r.get('VmId') or r.get('NicId') or ''}"
            for r in significant_routes
        ))

    return None


def _sync_subnets(source_region, source_vpc_id, target_subregion, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id):
    source_subnets = [
        s for s in octl.list_subnets(source_ak, source_sk, source_region)
        if s.get("NetId") == source_vpc_id
    ]
    target_subnets = [
        s for s in octl.list_subnets(target_ak, target_sk, target_region)
        if s.get("NetId") == target_vpc_id
    ]
    existing_by_name = {tag_name(s, s.get("SubnetId")): s for s in target_subnets}

    created = 0
    for subnet in source_subnets:
        name = tag_name(subnet, subnet.get("SubnetId"))
        if name in existing_by_name:
            continue
        new_subnet = octl.create_subnet(
            target_ak, target_sk, target_region, target_vpc_id,
            subnet["IpRange"], target_subregion, name,
            tags=subnet.get("Tags", []),
        )
        existing_by_name[name] = {**new_subnet, "Tags": [{"Key": "Name", "Value": name}]}
        created += 1

    return source_subnets, existing_by_name, created


def _sync_security_groups(source_region, source_vpc_id, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id):
    source_sgs = [
        g for g in octl.list_security_groups(source_ak, source_sk, source_region)
        if g.get("NetId") == source_vpc_id and g.get("SecurityGroupName") != "default"
    ]
    target_sgs = [
        g for g in octl.list_security_groups(target_ak, target_sk, target_region)
        if g.get("NetId") == target_vpc_id
    ]
    existing_by_name = {g.get("SecurityGroupName"): g for g in target_sgs}

    created = 0
    for sg in source_sgs:
        name = sg.get("SecurityGroupName")
        if not name or name in existing_by_name:
            continue
        new_sg = octl.create_security_group(
            target_ak, target_sk, target_region, target_vpc_id, name, sg.get("Description", ""),
            tags=sg.get("Tags", []),
        )
        existing_by_name[name] = new_sg
        created += 1

    return source_sgs, existing_by_name, created


def _normalize_ports(ip_protocol: str, from_port, to_port) -> tuple:
    """tcp/udp exigent une plage de ports explicite côté API ; ReadSecurityGroups
    omet parfois FromPortRange quand il vaut 0. Même défaut que
    octl.create_security_group_rule, appliqué ici aussi pour que la
    signature d'une règle source (FromPortRange absent) corresponde à celle
    de la même règle une fois créée côté cible (FromPortRange=0 explicite),
    et rester idempotent d'un resync à l'autre."""
    if ip_protocol in ("tcp", "udp"):
        from_port = 0 if from_port is None else from_port
        to_port = 65535 if to_port is None else to_port
    return from_port, to_port


def _rule_signature(rule: dict, member_names: tuple) -> tuple:
    ip_protocol = rule.get("IpProtocol")
    from_port, to_port = _normalize_ports(ip_protocol, rule.get("FromPortRange"), rule.get("ToPortRange"))
    return (
        ip_protocol,
        from_port,
        to_port,
        tuple(sorted(rule.get("IpRanges", []) or [])),
        member_names,
    )


def _sync_security_group_rules(target_ak, target_sk, target_region, source_sgs, target_sgs_by_name):
    """Réplique les règles inbound/outbound des security groups déjà créés
    côté cible. Une règle qui référence un autre security group comme
    source/destination est remappée par nom ; si ce security group n'existe
    pas encore côté cible, la règle est ignorée (comptée à part)."""
    source_sg_id_to_name = {}
    for sgs in (source_sgs,):
        for sg in sgs:
            source_sg_id_to_name[sg.get("SecurityGroupId")] = sg.get("SecurityGroupName")

    created = 0
    total = 0
    skipped = 0

    for sg in source_sgs:
        name = sg.get("SecurityGroupName")
        target_sg = target_sgs_by_name.get(name)
        if target_sg is None:
            continue
        target_sg_id = target_sg.get("SecurityGroupId")

        # Règles déjà présentes côté cible (signature par noms, pas par ID
        # — les IDs de security group diffèrent entre source et cible).
        target_sg_id_to_name = {g.get("SecurityGroupId"): n for n, g in target_sgs_by_name.items()}
        existing_signatures = {"Inbound": set(), "Outbound": set()}
        for flow, rules in (("Inbound", target_sg.get("InboundRules", [])), ("Outbound", target_sg.get("OutboundRules", []))):
            for rule in rules:
                member_names = tuple(sorted(
                    target_sg_id_to_name.get(m.get("SecurityGroupId"), "") for m in rule.get("SecurityGroupsMembers", []) or []
                ))
                existing_signatures[flow].add(_rule_signature(rule, member_names))

        for flow, rules in (("Inbound", sg.get("InboundRules", [])), ("Outbound", sg.get("OutboundRules", []))):
            for rule in rules:
                total += 1
                member_source_ids = [m.get("SecurityGroupId") for m in rule.get("SecurityGroupsMembers", []) or []]
                member_names = tuple(sorted(source_sg_id_to_name.get(mid, "") for mid in member_source_ids))
                ip_ranges = rule.get("IpRanges") or []

                if not ip_ranges and not member_names:
                    skipped += 1
                    continue

                if _rule_signature(rule, member_names) in existing_signatures[flow]:
                    continue

                if any(target_sgs_by_name.get(member_name) is None for member_name in member_names):
                    skipped += 1
                    continue

                from_port, to_port = _normalize_ports(rule.get("IpProtocol"), rule.get("FromPortRange"), rule.get("ToPortRange"))
                octl.create_security_group_rule(
                    target_ak, target_sk, target_region, target_sg_id, flow,
                    rule.get("IpProtocol", "-1"), from_port, to_port,
                    ip_ranges=ip_ranges or None, member_security_group_names=list(member_names) or None,
                )
                created += 1

    return created, total, skipped


def _sync_internet_service(source_region, source_vpc_id, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id):
    source_has_is = any(
        i.get("NetId") == source_vpc_id for i in octl.list_internet_services(source_ak, source_sk, source_region)
    )
    if not source_has_is:
        return None, False

    target_is = next(
        (i for i in octl.list_internet_services(target_ak, target_sk, target_region) if i.get("NetId") == target_vpc_id),
        None,
    )
    if target_is is not None:
        return target_is.get("InternetServiceId"), False

    new_is = octl.create_internet_service(target_ak, target_sk, target_region)
    is_id = new_is.get("InternetServiceId")
    octl.link_internet_service(target_ak, target_sk, target_region, is_id, target_vpc_id)
    return is_id, True


def _remap_gateway_id(gateway_id: str | None, target_internet_service_id: str | None) -> tuple[str | None, str | None]:
    """Retourne (gateway_id_cible, raison_skip). raison_skip est None si la
    route doit être créée."""
    if gateway_id is None:
        return None, "sans passerelle"
    if gateway_id == "local":
        return None, "route locale (créée automatiquement)"
    if gateway_id.startswith("igw-"):
        if target_internet_service_id is None:
            return None, "internet service non disponible côté cible"
        return target_internet_service_id, None
    if gateway_id.startswith("vgw-"):
        return None, "passerelle VPN non répliquée"
    return None, "type de passerelle non géré"


def _sync_route_tables(
    source_region, source_vpc_id, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id,
    source_subnets, target_subnets_by_name, target_internet_service_id,
    peering_target_id_by_source_id=None,
):
    source_subnet_names_by_id = {s.get("SubnetId"): tag_name(s, s.get("SubnetId")) for s in source_subnets}
    target_subnet_names_by_id = {s.get("SubnetId"): name for name, s in target_subnets_by_name.items()}

    source_rts = [
        rt for rt in octl.list_route_tables(source_ak, source_sk, source_region)
        if rt.get("NetId") == source_vpc_id
    ]
    target_rts = [
        rt for rt in octl.list_route_tables(target_ak, target_sk, target_region)
        if rt.get("NetId") == target_vpc_id
    ]
    target_main = next((rt for rt in target_rts if _is_main_route_table(rt)), None)
    target_by_key = {}
    for rt in target_rts:
        if _is_main_route_table(rt):
            continue
        key = _route_table_key(rt, target_subnet_names_by_id)
        if key:
            target_by_key[key] = rt

    route_tables_created = 0
    routes_created = 0
    routes_skipped = 0

    for rt in source_rts:
        is_main = _is_main_route_table(rt)
        if is_main:
            target_rt = target_main
        else:
            key = _route_table_key(rt, source_subnet_names_by_id)
            target_rt = target_by_key.get(key) if key else None
            if target_rt is None:
                if key is None:
                    # Ni tag Name, ni subnet rattaché, ni route significative :
                    # impossible d'identifier cette route table de façon stable
                    # d'un cycle à l'autre, on l'ignore plutôt que de risquer
                    # d'en recréer une à chaque resync.
                    routes_skipped += len([r for r in rt.get("Routes", []) if r.get("GatewayId") != "local"])
                    continue

                name = tag_name(rt, "")

                # Le subnet cible auquel rattacher la nouvelle route table
                # est retrouvé via le nom du subnet SOURCE associé (pas le
                # nom de la route table elle-même, différent dans la
                # pratique — ex. route table "rte-vms01" associée au
                # subnet "sn-vms01-1c").
                source_subnet_id = _route_table_subnet_id(rt)
                source_subnet_name = source_subnet_names_by_id.get(source_subnet_id)
                target_subnet = target_subnets_by_name.get(source_subnet_name) if source_subnet_name else None
                new_rt = octl.create_route_table(
                    target_ak, target_sk, target_region, target_vpc_id, name, tags=rt.get("Tags", []),
                )
                target_rt_id = new_rt.get("RouteTableId")
                if target_subnet is not None:
                    octl.link_route_table(target_ak, target_sk, target_region, target_rt_id, target_subnet.get("SubnetId"))
                route_tables_created += 1
                target_rt = {**new_rt, "RouteTableId": target_rt_id, "Routes": [], "Tags": rt.get("Tags", [])}
                target_by_key[key] = target_rt

        if target_rt is None:
            routes_skipped += len([r for r in rt.get("Routes", []) if r.get("GatewayId") != "local"])
            continue

        target_rt_id = target_rt.get("RouteTableId")
        existing_destinations = {r.get("DestinationIpRange") for r in target_rt.get("Routes", [])}

        for route in rt.get("Routes", []):
            destination = route.get("DestinationIpRange")

            net_peering_id = route.get("NetPeeringId")
            if net_peering_id:
                target_peering_id = (peering_target_id_by_source_id or {}).get(net_peering_id)
                if target_peering_id is None:
                    routes_skipped += 1
                    continue
                if destination in existing_destinations:
                    continue
                octl.create_route(
                    target_ak, target_sk, target_region, target_rt_id, destination, net_peering_id=target_peering_id,
                )
                existing_destinations.add(destination)
                routes_created += 1
                continue

            if route.get("NatServiceId") or route.get("VmId") or route.get("NicId"):
                if route.get("GatewayId") != "local":
                    routes_skipped += 1
                continue

            target_gateway_id, skip_reason = _remap_gateway_id(route.get("GatewayId"), target_internet_service_id)
            if skip_reason is not None:
                if skip_reason != "route locale (créée automatiquement)":
                    routes_skipped += 1
                continue

            if destination in existing_destinations:
                continue

            octl.create_route(target_ak, target_sk, target_region, target_rt_id, destination, target_gateway_id)
            existing_destinations.add(destination)
            routes_created += 1

    route_tables_total = len([
        rt for rt in source_rts
        if not _is_main_route_table(rt) and _route_table_key(rt, source_subnet_names_by_id)
    ])
    return route_tables_created, route_tables_total, routes_created, routes_skipped


def sync_target_network(
    plan, target_ak: str, target_sk: str, target_region: str,
    source_vpc_id: str, target_vpc_id: str, target_subregion: str,
    peering_target_id_by_source_id: dict | None = None,
) -> dict:
    """Recrée côté compte cible les subnets, security groups (avec leurs
    règles), l'internet service et les tables de routage du VPC source qui
    n'existent pas encore côté VPC cible (identifiés par leur tag/nom).
    EIP et NAT Gateway sont volontairement exclus (repris à la bascule, pas
    au resync — voir restore.py). Les routes vers un Net peering sont
    recréées si `peering_target_id_by_source_id` (retourné par
    sync_net_peerings, à appeler avant pour un plan à plusieurs VPC) fournit
    la correspondance ; sinon (plan à un seul VPC, ou peering pas encore
    créé côté cible) elles sont ignorées, comme les routes vers une
    passerelle VPN (jamais répliquées, hors scope). `source_vpc_id`/
    `target_vpc_id`/`target_subregion` sont ceux d'UN VPC du plan (voir
    app/plan_vpcs.py) — appelée une fois par VPC par l'appelant. Utilisé à
    la fois juste après la création du VPC cible et depuis le bouton de
    resynchronisation de la page Visualiser. Lève octl.OctlError en cas
    d'échec d'un appel."""
    source_ak = plan["source_ak"]
    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""
    source_region = plan["source_region"]

    _source_subnets, target_subnets_by_name, subnets_created = _sync_subnets(
        source_region, source_vpc_id, target_subregion, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id
    )
    source_sgs, target_sgs_by_name, sgs_created = _sync_security_groups(
        source_region, source_vpc_id, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id
    )
    sg_rules_created, sg_rules_total, sg_rules_skipped = _sync_security_group_rules(
        target_ak, target_sk, target_region, source_sgs, target_sgs_by_name
    )
    target_internet_service_id, internet_service_created = _sync_internet_service(
        source_region, source_vpc_id, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id
    )
    route_tables_created, route_tables_total, routes_created, routes_skipped = _sync_route_tables(
        source_region, source_vpc_id, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id,
        _source_subnets, target_subnets_by_name, target_internet_service_id,
        peering_target_id_by_source_id,
    )

    return {
        "subnets_created": subnets_created,
        "subnets_total": len(_source_subnets),
        "sgs_created": sgs_created,
        "sgs_total": len(source_sgs),
        "sg_rules_created": sg_rules_created,
        "sg_rules_total": sg_rules_total,
        "sg_rules_skipped": sg_rules_skipped,
        "internet_service_created": internet_service_created,
        "route_tables_created": route_tables_created,
        "route_tables_total": route_tables_total,
        "routes_created": routes_created,
        "routes_skipped": routes_skipped,
    }


def sync_net_peerings(plan, plan_vpcs: list, target_ak: str, target_sk: str, target_region: str) -> dict:
    """Découvre les Net Peering actifs entre les VPC source du plan (compte
    source, jamais modifié) et recrée l'équivalent entre les VPC cible
    correspondants (détection automatique, pas de sélection manuelle — voir
    CLAUDE.MD, section « Plans multi-VPC »). Idempotent : une paire de VPC
    cible déjà peerée n'est pas recréée. À appeler avant sync_target_network
    (une fois par plan, pas par VPC) pour que les routes vers ces peerings
    soient recréées dans la même passe de resynchronisation — voir le
    paramètre peering_target_id_by_source_id de sync_target_network. Lève
    octl.OctlError en cas d'échec d'un appel. Retourne {NetPeeringId source:
    NetPeeringId cible}."""
    vpcs_with_target = [pv for pv in plan_vpcs if pv["source_vpc_id"] and pv["target_vpc_id"]]
    if len(vpcs_with_target) < 2:
        return {}

    source_ak = plan["source_ak"]
    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""
    source_to_target = {pv["source_vpc_id"]: pv["target_vpc_id"] for pv in vpcs_with_target}

    source_peerings = octl.list_net_peerings(
        source_ak, source_sk, plan["source_region"],
        net_ids=list(source_to_target), state_names=["active", "pending-acceptance"],
    )
    relevant = []
    for peering in source_peerings:
        vpc_a = (peering.get("SourceNet") or {}).get("NetId")
        vpc_b = (peering.get("AccepterNet") or {}).get("NetId")
        if vpc_a in source_to_target and vpc_b in source_to_target and vpc_a != vpc_b:
            relevant.append((peering.get("NetPeeringId"), vpc_a, vpc_b))
    if not relevant:
        return {}

    target_peerings = octl.list_net_peerings(
        target_ak, target_sk, target_region,
        net_ids=list(source_to_target.values()), state_names=["active", "pending-acceptance"],
    )
    target_id_by_pair = {}
    for peering in target_peerings:
        vpc_a = (peering.get("SourceNet") or {}).get("NetId")
        vpc_b = (peering.get("AccepterNet") or {}).get("NetId")
        if vpc_a and vpc_b:
            target_id_by_pair[frozenset({vpc_a, vpc_b})] = peering.get("NetPeeringId")

    source_peering_id_to_target_id = {}
    for source_peering_id, vpc_a, vpc_b in relevant:
        target_pair = frozenset({source_to_target[vpc_a], source_to_target[vpc_b]})
        target_peering_id = target_id_by_pair.get(target_pair)
        if target_peering_id is None:
            target_a, target_b = tuple(target_pair)
            new_peering = octl.create_net_peering(target_ak, target_sk, target_region, target_a, target_b)
            target_peering_id = new_peering.get("NetPeeringId")
            octl.accept_net_peering(target_ak, target_sk, target_region, target_peering_id)
            target_id_by_pair[target_pair] = target_peering_id
        source_peering_id_to_target_id[source_peering_id] = target_peering_id

    return source_peering_id_to_target_id


def delete_target_vpc(
    plan, target_ak: str, target_sk: str, target_region: str, target_vpc_id: str,
    sandbox_id: int | None = None, vm_ids: list[str] | None = None,
) -> dict:
    """Suppression best-effort d'un VPC cible et de tout ce qu'il contient.
    Utilisée quand on recrée un VPC cible pour un plan qui en avait déjà un
    (voir admin.plan_create_target_vpc), et par scripts/run_sandbox.py
    (action `delete`, avec `sandbox_id` fourni — lit alors le mapping VM
    depuis `sandbox_vm_targets` plutôt que `vm_targets`). `vm_ids`, s'il est
    fourni (source_vm_id), restreint le mapping VM à ces VM — nécessaire
    pour un plan à plusieurs VPC : `vm_targets` est partagé par tout le plan
    (pas par VPC), donc sans ce filtre on essaierait de supprimer les VM des
    AUTRES VPC du même plan. Les autres catégories de ressources
    (subnets/SG/route tables/internet service/VPC) sont déjà scopées par
    `target_vpc_id`, pas besoin de filtre pour elles. Chaque catégorie de
    ressource est traitée indépendamment : une erreur sur l'une d'elles
    n'empêche pas d'essayer les suivantes, tout est remonté dans `errors`
    plutôt que de lever OctlError, pour laisser l'appelant poursuivre même
    en cas de nettoyage partiel. La suppression des VM est asynchrone côté
    API : on force leur arrêt (hard stop, plus rapide et plus fiable qu'un
    arrêt propre côté OS) puis on attend leur disparition effective avant
    de toucher au reste — security groups, subnets et VPC ne peuvent pas
    être supprimés tant qu'une VM y est encore attachée, même si l'appel
    DeleteVms a déjà été envoyé. Si une VM n'a pas disparu dans le délai
    imparti, le reste du nettoyage est reporté (pas tenté) pour ce VPC —
    un nouvel appel (ex. re-cliquer sur Supprimer) reprendra là où il
    s'est arrêté."""
    errors: list[str] = []
    vpc_registry.record_delete_attempt(target_vpc_id)

    conn = get_connection()
    if sandbox_id is not None:
        query = "SELECT source_vm_id, target_vm_id FROM sandbox_vm_targets WHERE sandbox_id = ?"
        params = [sandbox_id]
    else:
        query = "SELECT source_vm_id, target_vm_id FROM vm_targets WHERE plan_id = ?"
        params = [plan["id"]]
    if vm_ids is not None and not vm_ids:
        targets = []
    else:
        if vm_ids is not None:
            query += f" AND source_vm_id IN ({','.join('?' * len(vm_ids))})"
            params.extend(vm_ids)
        targets = conn.execute(query, params).fetchall()
    conn.close()

    vms_deleted = 0
    volume_ids_by_vm: dict[str, list[str]] = {}
    for row in targets:
        target_vm_id = row["target_vm_id"]
        try:
            vm = octl.get_vm(target_ak, target_sk, target_region, target_vm_id)
            if vm is None:
                vms_deleted += 1
                continue
            volume_ids_by_vm[target_vm_id] = [
                bdm["Bsu"]["VolumeId"] for bdm in vm.get("BlockDeviceMappings", [])
                if bdm.get("Bsu", {}).get("VolumeId")
            ]
            if vm.get("State") not in ("terminated", "shutting-down"):
                try:
                    octl.stop_vm(target_ak, target_sk, target_region, target_vm_id, force=True)
                except octl.OctlError:
                    pass  # pas bloquant : DeleteVms termine aussi une VM encore en cours d'arrêt
            octl.delete_vm(target_ak, target_sk, target_region, target_vm_id)
        except octl.OctlError as exc:
            errors.append(f"VM cible {target_vm_id} : {exc}")

    still_present = octl.wait_vms_terminated(target_ak, target_sk, target_region, list(volume_ids_by_vm))
    for target_vm_id, volume_ids in volume_ids_by_vm.items():
        if target_vm_id in still_present:
            errors.append(f"VM cible {target_vm_id} : toujours présente après le délai d'attente — nouvel essai nécessaire.")
            continue
        for volume_id in volume_ids:
            try:
                octl.delete_volume(target_ak, target_sk, target_region, volume_id)
            except octl.OctlError:
                pass  # volume racine probablement déjà supprimé (DeleteOnVmDeletion)
        vms_deleted += 1

    if still_present:
        # Des VM sont encore en train de disparaître : le reste (security
        # groups, subnets, VPC) ne peut pas être supprimé tant qu'elles y
        # sont attachées — inutile d'essayer maintenant, ça échouerait et
        # laisserait le VPC à moitié démonté. Un nouvel appel (ex.
        # re-cliquer sur Supprimer) reprendra une fois les VM parties.
        result = {
            "vms_deleted": vms_deleted,
            "vms_total": len(targets),
            "sgs_deleted": 0,
            "route_tables_deleted": 0,
            "internet_service_deleted": False,
            "subnets_deleted": 0,
            "vpc_deleted": False,
            "errors": errors,
        }
        vpc_registry.record_delete_result(
            target_vpc_id, f"Incomplet : {len(still_present)} VM encore présente(s), reste du nettoyage reporté."
        )
        return result

    sgs_deleted = 0
    try:
        sgs = [
            g for g in octl.list_security_groups(target_ak, target_sk, target_region)
            if g.get("NetId") == target_vpc_id and g.get("SecurityGroupName") != "default"
        ]
        for sg in sgs:
            try:
                octl.delete_security_group(target_ak, target_sk, target_region, sg.get("SecurityGroupId"))
                sgs_deleted += 1
            except octl.OctlError as exc:
                errors.append(f"Security group {sg.get('SecurityGroupName')} : {exc}")
    except octl.OctlError as exc:
        errors.append(f"Liste des security groups cible : {exc}")

    route_tables_deleted = 0
    try:
        rts = [
            rt for rt in octl.list_route_tables(target_ak, target_sk, target_region)
            if rt.get("NetId") == target_vpc_id
        ]
        for rt in rts:
            if _is_main_route_table(rt):
                continue
            rt_id = rt.get("RouteTableId")
            for link in rt.get("LinkRouteTables", []):
                link_id = link.get("LinkRouteTableId")
                if not link_id:
                    continue
                try:
                    octl.unlink_route_table(target_ak, target_sk, target_region, link_id)
                except octl.OctlError as exc:
                    errors.append(f"Dissociation route table {rt_id} : {exc}")
            try:
                octl.delete_route_table(target_ak, target_sk, target_region, rt_id)
                route_tables_deleted += 1
            except octl.OctlError as exc:
                errors.append(f"Route table {rt_id} : {exc}")
    except octl.OctlError as exc:
        errors.append(f"Liste des route tables cible : {exc}")

    internet_service_deleted = False
    try:
        services = [
            i for i in octl.list_internet_services(target_ak, target_sk, target_region)
            if i.get("NetId") == target_vpc_id
        ]
        for service in services:
            is_id = service.get("InternetServiceId")
            try:
                octl.unlink_internet_service(target_ak, target_sk, target_region, is_id, target_vpc_id)
                octl.delete_internet_service(target_ak, target_sk, target_region, is_id)
                internet_service_deleted = True
            except octl.OctlError as exc:
                errors.append(f"Internet service {is_id} : {exc}")
    except octl.OctlError as exc:
        errors.append(f"Liste des internet services cible : {exc}")

    subnets_deleted = 0
    try:
        subnets = [
            s for s in octl.list_subnets(target_ak, target_sk, target_region)
            if s.get("NetId") == target_vpc_id
        ]
        for subnet in subnets:
            try:
                octl.delete_subnet(target_ak, target_sk, target_region, subnet.get("SubnetId"))
                subnets_deleted += 1
            except octl.OctlError as exc:
                errors.append(f"Subnet {subnet.get('SubnetId')} : {exc}")
    except octl.OctlError as exc:
        errors.append(f"Liste des subnets cible : {exc}")

    vpc_deleted = False
    try:
        octl.delete_net(target_ak, target_sk, target_region, target_vpc_id)
        vpc_deleted = True
    except octl.OctlError as exc:
        errors.append(f"VPC {target_vpc_id} : {exc}")

    result = {
        "vms_deleted": vms_deleted,
        "vms_total": len(targets),
        "sgs_deleted": sgs_deleted,
        "route_tables_deleted": route_tables_deleted,
        "internet_service_deleted": internet_service_deleted,
        "subnets_deleted": subnets_deleted,
        "vpc_deleted": vpc_deleted,
        "errors": errors,
    }
    vpc_registry.record_delete_result(
        target_vpc_id, "VPC supprimé." if vpc_deleted else f"Échec : {'; '.join(errors) or 'raison inconnue'}"
    )
    return result
