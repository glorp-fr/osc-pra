"""Intégration avec octl (https://github.com/outscale/octl), le CLI Outscale.

Les credentials sont passés par variables d'environnement (OSC_ACCESS_KEY,
OSC_SECRET_KEY, OSC_REGION) au sous-processus, sans jamais toucher le disque.

Les appels en lecture (Read*) et les créations de VPC/subnet/tags ont été
vérifiés contre un octl réel et un compte Outscale. Les appels de création/
modification de VM et de volume (CreateVms, CreateVolume, LinkVolume,
UnlinkVolume, DeleteVolume, StopVms, StartVms) n'ont été vérifiés qu'avec
`octl --dry-run` (syntaxe des paramètres uniquement, sans appel réel à
l'API) — à surveiller au premier cycle de restauration réel.
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
    cible. Les règles ne sont pas répliquées (à implémenter)."""
    return _run(
        "CreateSecurityGroup", ak, sk, region,
        "--NetId", net_id, "--SecurityGroupName", name, "--Description", description or name,
    )


def list_route_tables(ak: str, sk: str, region: str) -> list:
    return _run("ReadRouteTables", ak, sk, region)


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
