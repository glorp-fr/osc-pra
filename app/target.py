"""Résolution des identifiants du compte cible et utilitaires partagés entre
la création du VPC cible, la page Visualiser (resynchronisation des
ressources réseau) et la restauration des BSU sur la VM cible (voir
restore.py)."""
from app import octl
from app.crypto import decrypt

# Ressources volontairement exclues du resync : reprises seulement à la
# bascule vers le PRA (voir restore.py), pas à chaque cycle de
# resynchronisation réseau.
#   - EIP (PublicIp) : une VM avec IP publique récupère la sienne (même
#     région) ou une nouvelle (autre région) au moment de la bascule.
#   - NAT Gateway (NatService) : dépend d'une EIP, créée au même moment.
# En conséquence, les routes qui pointent vers une NAT Gateway, une
# instance NAT (VmId/NicId) ou un Net peering / une passerelle VPN (VGW)
# ne sont pas non plus répliquées ici (ressources elles-mêmes hors scope).


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


def _sync_subnets(plan, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id):
    source_subnets = [
        s for s in octl.list_subnets(source_ak, source_sk, plan["source_region"])
        if s.get("NetId") == plan["source_vpc_id"]
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
            subnet["IpRange"], subnet["SubregionName"], name,
        )
        existing_by_name[name] = {**new_subnet, "Tags": [{"Key": "Name", "Value": name}]}
        created += 1

    return source_subnets, existing_by_name, created


def _sync_security_groups(plan, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id):
    source_sgs = [
        g for g in octl.list_security_groups(source_ak, source_sk, plan["source_region"])
        if g.get("NetId") == plan["source_vpc_id"] and g.get("SecurityGroupName") != "default"
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
        new_sg = octl.create_security_group(target_ak, target_sk, target_region, target_vpc_id, name, sg.get("Description", ""))
        existing_by_name[name] = new_sg
        created += 1

    return source_sgs, existing_by_name, created


def _rule_signature(rule: dict, member_names: tuple) -> tuple:
    return (
        rule.get("IpProtocol"),
        rule.get("FromPortRange"),
        rule.get("ToPortRange"),
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

                member_target_ids = []
                unresolved = False
                for member_name in member_names:
                    target_member = target_sgs_by_name.get(member_name)
                    if target_member is None:
                        unresolved = True
                        break
                    member_target_ids.append(target_member.get("SecurityGroupId"))
                if unresolved:
                    skipped += 1
                    continue

                octl.create_security_group_rule(
                    target_ak, target_sk, target_region, target_sg_id, flow,
                    rule.get("IpProtocol", "-1"), rule.get("FromPortRange"), rule.get("ToPortRange"),
                    ip_ranges=ip_ranges or None, member_security_group_ids=member_target_ids or None,
                )
                created += 1

    return created, total, skipped


def _sync_internet_service(plan, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id):
    source_has_is = any(
        i.get("NetId") == plan["source_vpc_id"] for i in octl.list_internet_services(source_ak, source_sk, plan["source_region"])
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
    plan, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id,
    source_subnets, target_subnets_by_name, target_internet_service_id,
):
    source_subnet_names_by_id = {s.get("SubnetId"): tag_name(s, s.get("SubnetId")) for s in source_subnets}

    source_rts = [
        rt for rt in octl.list_route_tables(source_ak, source_sk, plan["source_region"])
        if rt.get("NetId") == plan["source_vpc_id"]
    ]
    target_rts = [
        rt for rt in octl.list_route_tables(target_ak, target_sk, target_region)
        if rt.get("NetId") == target_vpc_id
    ]
    target_main = next((rt for rt in target_rts if _is_main_route_table(rt)), None)
    target_by_name = {tag_name(rt, ""): rt for rt in target_rts if not _is_main_route_table(rt) and tag_name(rt, "")}

    route_tables_created = 0
    routes_created = 0
    routes_skipped = 0

    for rt in source_rts:
        is_main = _is_main_route_table(rt)
        if is_main:
            target_rt = target_main
        else:
            name = tag_name(rt, "")
            target_rt = target_by_name.get(name)
            if target_rt is None:
                if not name:
                    # Pas de tag Name : impossible d'identifier cette route
                    # table de façon stable d'un cycle à l'autre, on l'ignore
                    # plutôt que de risquer d'en recréer une à chaque resync.
                    routes_skipped += len([r for r in rt.get("Routes", []) if r.get("GatewayId") != "local"])
                    continue

                # Le subnet cible auquel rattacher la nouvelle route table
                # est retrouvé via le nom du subnet SOURCE associé (pas le
                # nom de la route table elle-même, différent dans la
                # pratique — ex. route table "rte-vms01" associée au
                # subnet "sn-vms01-1c").
                source_subnet_id = _route_table_subnet_id(rt)
                source_subnet_name = source_subnet_names_by_id.get(source_subnet_id)
                target_subnet = target_subnets_by_name.get(source_subnet_name) if source_subnet_name else None
                new_rt = octl.create_route_table(target_ak, target_sk, target_region, target_vpc_id, name)
                target_rt_id = new_rt.get("RouteTableId")
                if target_subnet is not None:
                    octl.link_route_table(target_ak, target_sk, target_region, target_rt_id, target_subnet.get("SubnetId"))
                route_tables_created += 1
                target_rt = {**new_rt, "RouteTableId": target_rt_id, "Routes": [], "Tags": [{"Key": "Name", "Value": name}]}
                target_by_name[name] = target_rt

        if target_rt is None:
            routes_skipped += len([r for r in rt.get("Routes", []) if r.get("GatewayId") != "local"])
            continue

        target_rt_id = target_rt.get("RouteTableId")
        existing_destinations = {r.get("DestinationIpRange") for r in target_rt.get("Routes", [])}

        for route in rt.get("Routes", []):
            destination = route.get("DestinationIpRange")
            if route.get("NatServiceId") or route.get("NetPeeringId") or route.get("VmId") or route.get("NicId"):
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

    route_tables_total = len([rt for rt in source_rts if not _is_main_route_table(rt) and tag_name(rt, "")])
    return route_tables_created, route_tables_total, routes_created, routes_skipped


def sync_target_network(plan, target_ak: str, target_sk: str, target_region: str, target_vpc_id: str) -> dict:
    """Recrée côté compte cible les subnets, security groups (avec leurs
    règles), l'internet service et les tables de routage du VPC source qui
    n'existent pas encore côté VPC cible (identifiés par leur tag/nom).
    EIP et NAT Gateway sont volontairement exclus (repris à la bascule, pas
    au resync — voir restore.py). Les routes vers un Net peering ou une
    passerelle VPN sont ignorées (ressources elles-mêmes non répliquées).
    Utilisé à la fois juste après la création du VPC cible et depuis le
    bouton de resynchronisation de la page Visualiser. Lève octl.OctlError
    en cas d'échec d'un appel."""
    source_ak = plan["source_ak"]
    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""

    _source_subnets, target_subnets_by_name, subnets_created = _sync_subnets(
        plan, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id
    )
    source_sgs, target_sgs_by_name, sgs_created = _sync_security_groups(
        plan, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id
    )
    sg_rules_created, sg_rules_total, sg_rules_skipped = _sync_security_group_rules(
        target_ak, target_sk, target_region, source_sgs, target_sgs_by_name
    )
    target_internet_service_id, internet_service_created = _sync_internet_service(
        plan, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id
    )
    route_tables_created, route_tables_total, routes_created, routes_skipped = _sync_route_tables(
        plan, source_ak, source_sk, target_ak, target_sk, target_region, target_vpc_id,
        _source_subnets, target_subnets_by_name, target_internet_service_id,
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
