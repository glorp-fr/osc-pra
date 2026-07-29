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


def create_vpc(ak: str, sk: str, region: str, ip_range: str, name: str) -> dict:
    """Recrée un VPC (Net) à l'identique du point de vue réseau (même IpRange),
    avec un tag Name pour le retrouver côté cible."""
    net = _run("CreateNet", ak, sk, region, "--IpRange", ip_range)
    net_id = net.get("NetId") if isinstance(net, dict) else None
    if net_id and name:
        payload = json.dumps({"ResourceIds": [net_id], "Tags": [{"Key": "Name", "Value": name}]})
        _run("CreateTags", ak, sk, region, "--payload", payload)
    return net


def create_subnet(ak: str, sk: str, region: str, net_id: str, ip_range: str, subregion: str, name: str) -> dict:
    """Recrée un subnet à l'identique (même IpRange, même sous-région) dans
    le VPC cible, avec un tag Name pour le retrouver côté cible."""
    subnet = _run(
        "CreateSubnet", ak, sk, region,
        "--NetId", net_id, "--IpRange", ip_range, "--SubregionName", subregion,
    )
    subnet_id = subnet.get("SubnetId") if isinstance(subnet, dict) else None
    if subnet_id and name:
        payload = json.dumps({"ResourceIds": [subnet_id], "Tags": [{"Key": "Name", "Value": name}]})
        _run("CreateTags", ak, sk, region, "--payload", payload)
    return subnet


def list_security_groups(ak: str, sk: str, region: str) -> list:
    return _run("ReadSecurityGroups", ak, sk, region)


def create_security_group(ak: str, sk: str, region: str, net_id: str, name: str, description: str) -> dict:
    """Recrée un security group à l'identique (nom + description) dans le VPC
    cible. Les règles sont répliquées séparément par create_security_group_rule
    (voir app/target.py)."""
    return _run(
        "CreateSecurityGroup", ak, sk, region,
        "--NetId", net_id, "--SecurityGroupName", name, "--Description", description or name,
    )


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


def create_route_table(ak: str, sk: str, region: str, net_id: str, name: str) -> dict:
    """Crée une route table et la tague (Name) pour pouvoir la retrouver côté
    cible lors d'un prochain resync — CreateRouteTable ne prend pas de nom en
    paramètre, seul le tag permet de l'identifier (contrairement aux security
    groups, identifiés par leur SecurityGroupName)."""
    result = _run("CreateRouteTable", ak, sk, region, "--NetId", net_id)
    if isinstance(result, dict):
        route_table = result
    elif isinstance(result, list) and result:
        route_table = result[0]
    else:
        raise OctlError("CreateRouteTable n'a renvoyé aucune route table.")

    route_table_id = route_table.get("RouteTableId")
    if route_table_id and name:
        payload = json.dumps({"ResourceIds": [route_table_id], "Tags": [{"Key": "Name", "Value": name}]})
        _run("CreateTags", ak, sk, region, "--payload", payload)
    return route_table


def link_route_table(ak: str, sk: str, region: str, route_table_id: str, subnet_id: str) -> None:
    _run("LinkRouteTable", ak, sk, region, "--RouteTableId", route_table_id, "--SubnetId", subnet_id)


def create_route(ak: str, sk: str, region: str, route_table_id: str, destination_ip_range: str, gateway_id: str) -> None:
    _run(
        "CreateRoute", ak, sk, region,
        "--RouteTableId", route_table_id, "--DestinationIpRange", destination_ip_range, "--GatewayId", gateway_id,
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


def create_vm(
    ak: str, sk: str, region: str, image_id: str, vm_type: str, subnet_id: str,
    security_group_ids: list[str], subregion: str, root_device_name: str, root_snapshot_id: str,
    root_volume_type: str | None = None, root_iops: int | None = None,
) -> dict:
    """Crée la VM cible à l'arrêt (aucun boot automatique), avec son volume
    racine restauré directement depuis le snapshot fourni plutôt que depuis
    l'image (voir restore.py pour l'orchestration complète)."""
    args = [
        "--ImageId", image_id,
        "--VmType", vm_type,
        "--SubnetId", subnet_id,
        "--Placement.SubregionName", subregion,
        "--MinVmsCount", "1",
        "--MaxVmsCount", "1",
        "--BlockDeviceMappings.0.DeviceName", root_device_name,
        "--BlockDeviceMappings.0.Bsu.SnapshotId", root_snapshot_id,
        "--BlockDeviceMappings.0.Bsu.DeleteOnVmDeletion",
    ]
    if root_volume_type:
        args += ["--BlockDeviceMappings.0.Bsu.VolumeType", root_volume_type]
    if root_iops:
        args += ["--BlockDeviceMappings.0.Bsu.Iops", str(root_iops)]
    if security_group_ids:
        args += ["--SecurityGroupIds", ",".join(security_group_ids)]

    result = _run("CreateVms", ak, sk, region, *args)
    vms = result if isinstance(result, list) else result.get("Vms", [result] if result else [])
    if not vms:
        raise OctlError("CreateVms n'a renvoyé aucune VM.")
    return vms[0]


def list_volumes(ak: str, sk: str, region: str, volume_ids: list[str] | None = None) -> list:
    args = ["--Filters.VolumeIds", ",".join(volume_ids)] if volume_ids else []
    result = _run("ReadVolumes", ak, sk, region, *args)
    return result if isinstance(result, list) else result.get("Volumes", [])


def create_volume(
    ak: str, sk: str, region: str, snapshot_id: str, subregion: str,
    volume_type: str | None = None, iops: int | None = None,
) -> dict:
    args = ["--SnapshotId", snapshot_id, "--SubregionName", subregion]
    if volume_type:
        args += ["--VolumeType", volume_type]
    if iops:
        args += ["--Iops", str(iops)]
    result = _run("CreateVolume", ak, sk, region, *args)
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and result:
        return result[0]
    raise OctlError("CreateVolume n'a renvoyé aucun volume.")


def attach_volume(ak: str, sk: str, region: str, volume_id: str, vm_id: str, device_name: str) -> None:
    _run("LinkVolume", ak, sk, region, "--VolumeId", volume_id, "--VmId", vm_id, "--DeviceName", device_name)


def detach_volume(ak: str, sk: str, region: str, volume_id: str) -> None:
    _run("UnlinkVolume", ak, sk, region, "--VolumeId", volume_id)


def delete_volume(ak: str, sk: str, region: str, volume_id: str) -> None:
    _run("DeleteVolume", ak, sk, region, "--VolumeId", volume_id)
