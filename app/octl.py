"""Intégration avec octl (https://github.com/outscale/octl), le CLI Outscale.

Les credentials sont passés par variables d'environnement (OSC_ACCESS_KEY,
OSC_SECRET_KEY, OSC_REGION) au sous-processus, sans jamais toucher le disque.
Ce comportement et la syntaxe des commandes (`octl iaas api <Action>
--output json`) sont documentés dans le dépôt officiel ; à revalider avec un
octl réel et un compte Outscale, faute d'environnement de test disponible ici.
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


def list_security_groups(ak: str, sk: str, region: str) -> list:
    return _run("ReadSecurityGroups", ak, sk, region)


def list_route_tables(ak: str, sk: str, region: str) -> list:
    return _run("ReadRouteTables", ak, sk, region)


def create_snapshot(ak: str, sk: str, region: str, volume_id: str, description: str) -> dict:
    return _run("CreateSnapshot", ak, sk, region, "--VolumeId", volume_id, "--Description", description)


def list_snapshots(ak: str, sk: str, region: str, volume_id: str) -> list:
    return _run("ReadSnapshots", ak, sk, region, "--VolumeId", volume_id)


def delete_snapshot(ak: str, sk: str, region: str, snapshot_id: str) -> None:
    _run("DeleteSnapshot", ak, sk, region, "--SnapshotId", snapshot_id)
