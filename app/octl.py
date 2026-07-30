"""Intégration avec octl (https://github.com/outscale/octl), le CLI Outscale.

Les credentials sont passés par variables d'environnement (OSC_ACCESS_KEY,
OSC_SECRET_KEY, OSC_REGION) au sous-processus, sans jamais toucher le disque.

Les appels en lecture (Read*) et les créations de VPC/subnet/tags ont été
vérifiés contre un octl réel et un compte Outscale. Les appels de création/
modification de VM, de volume et de ressources réseau (CreateVms,
CreateVolume, LinkVolume, UnlinkVolume, DeleteVolume, StopVms, StartVms,
CreateSecurityGroupRule, CreateRouteTable, LinkRouteTable, CreateRoute,
CreateInternetService, LinkInternetService) n'ont été vérifiés qu'avec
`octl --dry-run` (syntaxe des paramètres uniquement, sans appel réel à
l'API) — à surveiller au premier resync/cycle de restauration réel.

Les fonctions EIP (CreatePublicIp, LinkPublicIp, UnlinkPublicIp,
DeletePublicIp) et NAT Gateway (CreateNatService, DeleteNatService),
utilisées uniquement par la bascule PRA (voir app/failover.py), sont
encore plus récentes : leur syntaxe vient de `octl iaas api <Action>
--help`, mais aucune n'a jamais été exécutée contre un compte réel par ce
code — à valider impérativement via un Test de PRA avant toute Activation.
"""
import json
import os
import shutil
import subprocess

OCTL_BIN = os.environ.get("OCTL_BIN", "octl")
TIMEOUT_SECONDS = 30


class OctlError(Exception):
    pass


def is_available() -> bool:
    return shutil.which(OCTL_BIN) is not None


def _run(action: str, ak: str, sk: str, region: str, *extra_args: str):
    env = os.environ.copy()
    env["OSC_ACCESS_KEY"] = ak
    env["OSC_SECRET_KEY"] = sk
    env["OSC_REGION"] = region

    cmd = [OCTL_BIN, "iaas", "api", action, "--output", "json", *extra_args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=TIMEOUT_SECONDS
        )
    except FileNotFoundError:
        raise OctlError("octl est introuvable sur cette machine (binaire non installé ou hors du PATH).")
    except subprocess.TimeoutExpired:
        raise OctlError(f"octl n'a pas répondu dans le délai imparti ({TIMEOUT_SECONDS}s).")

    if result.returncode != 0:
        raise OctlError(result.stderr.strip() or f"octl a échoué (code {result.returncode}).")

    try:
        return json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise OctlError(f"Réponse octl illisible : {exc}")


def check_access_key(ak: str, sk: str, region: str) -> dict:
    """Teste la validité d'un AK/SK via ReadAccessKeys.

    Retourne {"valid": bool, "expiration_date": str | None, "error": str | None}.
    """
    try:
        keys = _run("ReadAccessKeys", ak, sk, region)
    except OctlError as exc:
        return {"valid": False, "expiration_date": None, "error": str(exc)}

    for key in keys:
        if key.get("AccessKeyId") == ak:
            return {
                "valid": key.get("State") == "ACTIVE",
                "expiration_date": key.get("ExpirationDate"),
                "error": None,
            }
    return {"valid": False, "expiration_date": None, "error": "Clé non retrouvée dans la réponse octl."}


def list_vms(ak: str, sk: str, region: str) -> list:
    return _run("ReadVms", ak, sk, region)


def list_subnets(ak: str, sk: str, region: str) -> list:
    return _run("ReadSubnets", ak, sk, region)


def list_vpcs(ak: str, sk: str, region: str) -> list:
    return _run("ReadNets", ak, sk, region)


def list_images(ak: str, sk: str, region: str) -> list:
    return _run("ReadImages", ak, sk, region)


def delete_net(ak: str, sk: str, region: str, net_id: str) -> None:
    _run("DeleteNet", ak, sk, region, "--NetId", net_id)


def create_tags(ak: str, sk: str, region: str, resource_id: str, tags: list[dict] | None) -> None:
    """Applique une liste de tags (format API OSC : [{"Key": .., "Value": ..}])
    à une ressource cible déjà créée — utilisé pour répliquer tous les tags
    d'une ressource source (pas seulement Name) sur son équivalent cible."""
    if not tags:
        return
    payload = json.dumps({"ResourceIds": [resource_id], "Tags": tags})
    _run("CreateTags", ak, sk, region, "--payload", payload)


def _merge_name_tag(name: str, tags: list[dict] | None) -> list[dict]:
    """Combine un nom déjà résolu (tag Name attendu côté cible, potentiellement
    différent du tag Name source — cas du VPC, suffixé, ou du fallback sur
    l'ID en l'absence de tag Name source) avec le reste des tags source,
    Name exclu pour ne pas le dupliquer/contredire."""
    merged = [{"Key": "Name", "Value": name}] if name else []
    merged += [t for t in (tags or []) if t.get("Key") != "Name"]
    return merged


def create_vpc(ak: str, sk: str, region: str, ip_range: str, name: str, tags: list[dict] | None = None) -> dict:
    """Recrée un VPC (Net) à l'identique du point de vue réseau (même IpRange),
    avec un tag Name pour le retrouver côté cible et les autres tags du VPC
    source (`tags`, Name exclu — le nom cible est volontairement différent du
    nom source, voir app.routers.admin.plan_create_target_vpc)."""
    net = _run("CreateNet", ak, sk, region, "--IpRange", ip_range)
    net_id = net.get("NetId") if isinstance(net, dict) else None
    if net_id:
        create_tags(ak, sk, region, net_id, _merge_name_tag(name, tags))
    return net


def create_subnet(
    ak: str, sk: str, region: str, net_id: str, ip_range: str, subregion: str, name: str,
    tags: list[dict] | None = None,
) -> dict:
    """Recrée un subnet à l'identique (même IpRange, même sous-région) dans
    le VPC cible, avec un tag Name pour le retrouver côté cible et les autres
    tags du subnet source (`tags`, Name exclu)."""
    subnet = _run(
        "CreateSubnet", ak, sk, region,
        "--NetId", net_id, "--IpRange", ip_range, "--SubregionName", subregion,
    )
    subnet_id = subnet.get("SubnetId") if isinstance(subnet, dict) else None
    if subnet_id:
        create_tags(ak, sk, region, subnet_id, _merge_name_tag(name, tags))
    return subnet


def delete_subnet(ak: str, sk: str, region: str, subnet_id: str) -> None:
    _run("DeleteSubnet", ak, sk, region, "--SubnetId", subnet_id)


def list_security_groups(ak: str, sk: str, region: str) -> list:
    return _run("ReadSecurityGroups", ak, sk, region)


def create_security_group(
    ak: str, sk: str, region: str, net_id: str, name: str, description: str,
    tags: list[dict] | None = None,
) -> dict:
    """Recrée un security group à l'identique (nom + description) dans le VPC
    cible, avec les tags du security group source. Les règles sont répliquées
    séparément par create_security_group_rule (voir app/target.py)."""
    sg = _run(
        "CreateSecurityGroup", ak, sk, region,
        "--NetId", net_id, "--SecurityGroupName", name, "--Description", description or name,
    )
    sg_id = sg.get("SecurityGroupId") if isinstance(sg, dict) else None
    if sg_id:
        create_tags(ak, sk, region, sg_id, tags)
    return sg


def delete_security_group(ak: str, sk: str, region: str, security_group_id: str) -> None:
    _run("DeleteSecurityGroup", ak, sk, region, "--SecurityGroupId", security_group_id)


def create_security_group_rule(
    ak: str, sk: str, region: str, security_group_id: str, flow: str,
    ip_protocol: str, from_port: int | None, to_port: int | None,
    ip_ranges: list[str] | None = None, member_security_group_names: list[str] | None = None,
) -> None:
    """Ajoute une règle à un security group cible. `flow` vaut 'Inbound' ou
    'Outbound'. La règle peut cibler des plages IP (`ip_ranges`) et/ou
    d'autres security groups du même compte cible par leur nom
    (`member_security_group_names`).

    Utilise la forme "plate" des paramètres (--IpRange, un par appel) et non
    la forme tableau (--Rules.0.IpRanges) : cette dernière *omet du payload*
    les entiers valant 0 (bug/quirk octl constaté en pratique — une règle
    tcp avec FromPortRange=0 se voit alors attribuer -1 par défaut côté API,
    invalide pour tcp/udp). Une règle avec plusieurs IP/SG déclenche donc
    plusieurs appels API, un par IP/SG référencé, plutôt qu'un seul appel
    batché.

    Pour tcp/udp, FromPortRange/ToPortRange sont toujours envoyés
    explicitement (défaut 0/65535 si absents) : ReadSecurityGroups omet
    parfois FromPortRange quand il vaut 0 (vu en pratique sur des règles
    réelles), et -1 (défaut API si le flag est omis) est invalide pour
    tcp/udp — l'API exige une plage de ports explicite pour ces protocoles,
    -1 n'étant valide que pour icmp ou le protocole 'all'/-1."""
    if ip_protocol in ("tcp", "udp"):
        if from_port is None:
            from_port = 0
        if to_port is None:
            to_port = 65535

    base_args = [
        "--SecurityGroupId", security_group_id,
        "--Flow", flow,
        "--IpProtocol", ip_protocol,
    ]
    if from_port is not None:
        base_args += ["--FromPortRange", str(from_port)]
    if to_port is not None:
        base_args += ["--ToPortRange", str(to_port)]

    targets = [("--IpRange", ip) for ip in (ip_ranges or [])]
    targets += [("--SecurityGroupNameToLink", name) for name in (member_security_group_names or [])]
    for flag, value in targets:
        _run("CreateSecurityGroupRule", ak, sk, region, *base_args, flag, value)


def list_route_tables(ak: str, sk: str, region: str) -> list:
    return _run("ReadRouteTables", ak, sk, region)


def create_route_table(
    ak: str, sk: str, region: str, net_id: str, name: str, tags: list[dict] | None = None,
) -> dict:
    """Crée une route table et la tague (Name + reste des tags source) pour
    pouvoir la retrouver côté cible lors d'un prochain resync — CreateRouteTable
    ne prend pas de nom en paramètre, seul le tag permet de l'identifier
    (contrairement aux security groups, identifiés par leur SecurityGroupName)."""
    result = _run("CreateRouteTable", ak, sk, region, "--NetId", net_id)
    if isinstance(result, dict):
        route_table = result
    elif isinstance(result, list) and result:
        route_table = result[0]
    else:
        raise OctlError("CreateRouteTable n'a renvoyé aucune route table.")

    route_table_id = route_table.get("RouteTableId")
    if route_table_id:
        create_tags(ak, sk, region, route_table_id, _merge_name_tag(name, tags))
    return route_table


def link_route_table(ak: str, sk: str, region: str, route_table_id: str, subnet_id: str) -> None:
    _run("LinkRouteTable", ak, sk, region, "--RouteTableId", route_table_id, "--SubnetId", subnet_id)


def unlink_route_table(ak: str, sk: str, region: str, link_route_table_id: str) -> None:
    _run("UnlinkRouteTable", ak, sk, region, "--LinkRouteTableId", link_route_table_id)


def delete_route_table(ak: str, sk: str, region: str, route_table_id: str) -> None:
    _run("DeleteRouteTable", ak, sk, region, "--RouteTableId", route_table_id)


def create_route(
    ak: str, sk: str, region: str, route_table_id: str, destination_ip_range: str,
    gateway_id: str | None = None, nat_service_id: str | None = None,
) -> None:
    """`gateway_id` (internet service, resync réseau normal) et
    `nat_service_id` (NAT Gateway, bascule PRA uniquement — voir
    app/failover.py) sont mutuellement exclusifs côté API."""
    target_flag, target_value = ("--NatServiceId", nat_service_id) if nat_service_id else ("--GatewayId", gateway_id)
    _run(
        "CreateRoute", ak, sk, region,
        "--RouteTableId", route_table_id, "--DestinationIpRange", destination_ip_range, target_flag, target_value,
    )


def list_internet_services(ak: str, sk: str, region: str) -> list:
    return _run("ReadInternetServices", ak, sk, region)


def create_internet_service(ak: str, sk: str, region: str) -> dict:
    result = _run("CreateInternetService", ak, sk, region)
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and result:
        return result[0]
    raise OctlError("CreateInternetService n'a renvoyé aucun internet service.")


def link_internet_service(ak: str, sk: str, region: str, internet_service_id: str, net_id: str) -> None:
    _run("LinkInternetService", ak, sk, region, "--InternetServiceId", internet_service_id, "--NetId", net_id)


def unlink_internet_service(ak: str, sk: str, region: str, internet_service_id: str, net_id: str) -> None:
    _run("UnlinkInternetService", ak, sk, region, "--InternetServiceId", internet_service_id, "--NetId", net_id)


def delete_internet_service(ak: str, sk: str, region: str, internet_service_id: str) -> None:
    _run("DeleteInternetService", ak, sk, region, "--InternetServiceId", internet_service_id)


def create_public_ip(ak: str, sk: str, region: str) -> dict:
    """Alloue une nouvelle EIP pour le compte (aucun paramètre requis)."""
    result = _run("CreatePublicIp", ak, sk, region)
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and result:
        return result[0]
    raise OctlError("CreatePublicIp n'a renvoyé aucune adresse IP.")


def link_public_ip(ak: str, sk: str, region: str, public_ip_id: str, vm_id: str, allow_relink: bool = True) -> dict:
    """Rattache une EIP à une VM. `allow_relink=True` (défaut) permet de
    rattacher directement une EIP déjà liée à une autre VM sans avoir à
    l'en détacher explicitement d'abord — utilisé en Activation PRA pour
    reprendre l'EIP de la VM source sur la VM cible."""
    args = ["--PublicIpId", public_ip_id, "--VmId", vm_id]
    if allow_relink:
        args += ["--AllowRelink", "true"]
    result = _run("LinkPublicIp", ak, sk, region, *args)
    return result if isinstance(result, dict) else (result[0] if isinstance(result, list) and result else {})


def unlink_public_ip(ak: str, sk: str, region: str, link_public_ip_id: str) -> None:
    _run("UnlinkPublicIp", ak, sk, region, "--LinkPublicIpId", link_public_ip_id)


def delete_public_ip(ak: str, sk: str, region: str, public_ip_id: str) -> None:
    _run("DeletePublicIp", ak, sk, region, "--PublicIpId", public_ip_id)


def list_public_ips(ak: str, sk: str, region: str, vm_ids: list[str] | None = None) -> list:
    args = ["--Filters.VmIds", ",".join(vm_ids)] if vm_ids else []
    result = _run("ReadPublicIps", ak, sk, region, *args)
    return result if isinstance(result, list) else result.get("PublicIps", [])


def create_nat_service(ak: str, sk: str, region: str, subnet_id: str, public_ip_id: str, tags: list[dict] | None = None) -> dict:
    """Crée une NAT Gateway sur `subnet_id` avec l'EIP `public_ip_id`. L'EIP
    d'une NAT Gateway ne peut être définie qu'à la création (l'API ne permet
    pas de la changer ensuite) — voir app/failover.py pour la bascule."""
    result = _run("CreateNatService", ak, sk, region, "--SubnetId", subnet_id, "--PublicIpId", public_ip_id)
    if isinstance(result, dict):
        nat_service = result
    elif isinstance(result, list) and result:
        nat_service = result[0]
    else:
        raise OctlError("CreateNatService n'a renvoyé aucune NAT Gateway.")

    nat_service_id = nat_service.get("NatServiceId")
    if nat_service_id:
        create_tags(ak, sk, region, nat_service_id, tags)
    return nat_service


def list_nat_services(ak: str, sk: str, region: str, net_id: str | None = None) -> list:
    args = ["--Filters.NetIds", net_id] if net_id else []
    result = _run("ReadNatServices", ak, sk, region, *args)
    return result if isinstance(result, list) else result.get("NatServices", [])


def delete_nat_service(ak: str, sk: str, region: str, nat_service_id: str) -> None:
    """Détache l'EIP de la NAT Gateway (ne la libère pas) mais ne supprime
    pas les routes qui pointent vers elle dans les tables de routage — à
    recréer/patcher séparément (voir app/failover.py::assign_nat)."""
    _run("DeleteNatService", ak, sk, region, "--NatServiceId", nat_service_id)


def create_snapshot(ak: str, sk: str, region: str, volume_id: str, description: str) -> dict:
    return _run("CreateSnapshot", ak, sk, region, "--VolumeId", volume_id, "--Description", description)


def list_snapshots(ak: str, sk: str, region: str, volume_id: str) -> list:
    return _run("ReadSnapshots", ak, sk, region, "--VolumeId", volume_id)


def delete_snapshot(ak: str, sk: str, region: str, snapshot_id: str) -> None:
    _run("DeleteSnapshot", ak, sk, region, "--SnapshotId", snapshot_id)


def get_vm(ak: str, sk: str, region: str, vm_id: str) -> dict | None:
    result = _run("ReadVms", ak, sk, region, "--Filters.VmIds", vm_id)
    items = result if isinstance(result, list) else result.get("Vms", [])
    return items[0] if items else None


def stop_vm(ak: str, sk: str, region: str, vm_id: str) -> None:
    _run("StopVms", ak, sk, region, "--VmIds", vm_id)


def start_vm(ak: str, sk: str, region: str, vm_id: str) -> None:
    _run("StartVms", ak, sk, region, "--VmIds", vm_id)


def update_vm_type(ak: str, sk: str, region: str, vm_id: str, vm_type: str) -> None:
    """Change le type (CPU/RAM) d'une VM — nécessite que la VM soit à
    l'arrêt (contrainte de l'API, voir `octl iaas api UpdateVm --help`)."""
    _run("UpdateVm", ak, sk, region, "--VmId", vm_id, "--VmType", vm_type)


def delete_vm(ak: str, sk: str, region: str, vm_id: str) -> None:
    _run("DeleteVms", ak, sk, region, "--VmIds", vm_id)


def create_vm(
    ak: str, sk: str, region: str, image_id: str, vm_type: str, subnet_id: str,
    security_group_ids: list[str], subregion: str, tags: list[dict] | None = None,
) -> dict:
    """Crée la VM cible avec un boot normal depuis l'image (volume racine
    par défaut, démarrage automatique) : son BSU racine est ensuite
    remplacé par le volume restauré depuis le snapshot une fois la VM à
    l'arrêt (voir restore.py pour l'orchestration complète).

    --Placement.Tenancy est toujours envoyé explicitement aux côtés de
    --Placement.SubregionName : dès qu'un sous-champ de --Placement.* est
    posé, octl sérialise tout l'objet Placement, et Tenancy vaut alors ""
    par défaut plutôt que d'être omis — "" n'est pas une valeur Tenancy
    valide (default | dedicated | id de groupe dédié) et fait échouer
    CreateVms avec InvalidParameterValue (constaté en pratique, code retour
    4045 sans détail)."""
    args = [
        "--ImageId", image_id,
        "--VmType", vm_type,
        "--SubnetId", subnet_id,
        "--Placement.SubregionName", subregion,
        "--Placement.Tenancy", "default",
        "--MinVmsCount", "1",
        "--MaxVmsCount", "1",
    ]
    if security_group_ids:
        args += ["--SecurityGroupIds", ",".join(security_group_ids)]

    result = _run("CreateVms", ak, sk, region, *args)
    vms = result if isinstance(result, list) else result.get("Vms", [result] if result else [])
    if not vms:
        raise OctlError("CreateVms n'a renvoyé aucune VM.")
    vm = vms[0]
    vm_id = vm.get("VmId")
    if vm_id:
        create_tags(ak, sk, region, vm_id, tags)
    return vm


def list_volumes(ak: str, sk: str, region: str, volume_ids: list[str] | None = None) -> list:
    args = ["--Filters.VolumeIds", ",".join(volume_ids)] if volume_ids else []
    result = _run("ReadVolumes", ak, sk, region, *args)
    return result if isinstance(result, list) else result.get("Volumes", [])


def create_volume(
    ak: str, sk: str, region: str, snapshot_id: str, subregion: str,
    volume_type: str | None = None, iops: int | None = None, tags: list[dict] | None = None,
) -> dict:
    """`iops` n'est envoyé que pour un volume io1 : ReadVolumes renvoie un
    Iops non nul même pour un volume gp2/standard (valeur dérivée, non
    configurable pour ces types) — le passer tel quel fait échouer
    CreateVolume avec InvalidParameterValue (constaté en pratique, code
    retour 4045 sans détail, sur un volume source gp2)."""
    args = ["--SnapshotId", snapshot_id, "--SubregionName", subregion]
    if volume_type:
        args += ["--VolumeType", volume_type]
    if iops and volume_type == "io1":
        args += ["--Iops", str(iops)]
    result = _run("CreateVolume", ak, sk, region, *args)
    if isinstance(result, dict):
        volume = result
    elif isinstance(result, list) and result:
        volume = result[0]
    else:
        raise OctlError("CreateVolume n'a renvoyé aucun volume.")

    volume_id = volume.get("VolumeId")
    if volume_id:
        create_tags(ak, sk, region, volume_id, tags)
    return volume


def attach_volume(ak: str, sk: str, region: str, volume_id: str, vm_id: str, device_name: str) -> None:
    _run("LinkVolume", ak, sk, region, "--VolumeId", volume_id, "--VmId", vm_id, "--DeviceName", device_name)


def detach_volume(ak: str, sk: str, region: str, volume_id: str) -> None:
    _run("UnlinkVolume", ak, sk, region, "--VolumeId", volume_id)


def delete_volume(ak: str, sk: str, region: str, volume_id: str) -> None:
    _run("DeleteVolume", ak, sk, region, "--VolumeId", volume_id)
