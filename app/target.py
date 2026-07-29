"""Résolution des identifiants du compte cible et utilitaires partagés entre
la page Visualiser (resynchronisation des ressources réseau) et la
restauration des BSU sur la VM cible (voir restore.py)."""
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
