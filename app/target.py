"""Résolution des identifiants du compte cible et utilitaires partagés entre
la création du VPC cible, la page Visualiser (resynchronisation des
ressources réseau) et la restauration des BSU sur la VM cible (voir
restore.py)."""
from app import octl
from app.crypto import decrypt


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


def sync_target_network(plan, target_ak: str, target_sk: str, target_region: str, target_vpc_id: str) -> dict:
    """Recrée côté compte cible les subnets et security groups du VPC
    source qui n'existent pas encore côté VPC cible (identifiés par leur
    tag/nom). Les règles de security group et les tables de routage ne
    sont pas répliquées. Utilisé à la fois juste après la création du VPC
    cible et depuis le bouton de resynchronisation de la page Visualiser.
    Lève octl.OctlError en cas d'échec d'un appel."""
    source_sk = decrypt(plan["source_sk_encrypted"]) if plan["source_sk_encrypted"] else ""

    source_subnets = [
        s for s in octl.list_subnets(plan["source_ak"], source_sk, plan["source_region"])
        if s.get("NetId") == plan["source_vpc_id"]
    ]
    target_subnets = [
        s for s in octl.list_subnets(target_ak, target_sk, target_region)
        if s.get("NetId") == target_vpc_id
    ]
    existing_subnet_names = {tag_name(s, s.get("SubnetId")) for s in target_subnets}

    subnets_created = 0
    for subnet in source_subnets:
        name = tag_name(subnet, subnet.get("SubnetId"))
        if name in existing_subnet_names:
            continue
        octl.create_subnet(
            target_ak, target_sk, target_region, target_vpc_id,
            subnet["IpRange"], subnet["SubregionName"], name,
        )
        subnets_created += 1

    source_sgs = [
        g for g in octl.list_security_groups(plan["source_ak"], source_sk, plan["source_region"])
        if g.get("NetId") == plan["source_vpc_id"] and g.get("SecurityGroupName") != "default"
    ]
    target_sgs = [
        g for g in octl.list_security_groups(target_ak, target_sk, target_region)
        if g.get("NetId") == target_vpc_id
    ]
    existing_sg_names = {g.get("SecurityGroupName") for g in target_sgs}

    sgs_created = 0
    for sg in source_sgs:
        name = sg.get("SecurityGroupName")
        if not name or name in existing_sg_names:
            continue
        octl.create_security_group(target_ak, target_sk, target_region, target_vpc_id, name, sg.get("Description", ""))
        sgs_created += 1

    return {
        "subnets_created": subnets_created,
        "subnets_total": len(source_subnets),
        "sgs_created": sgs_created,
        "sgs_total": len(source_sgs),
    }
