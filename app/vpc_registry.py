"""Registre des VPC cible créés par l'outil — les deux seuls endroits qui
appellent octl.create_vpc pour un usage cible (VPC cible de plan et VPC de
sandbox, voir app/routers/admin.py::plan_vpc_create_target et
scripts/run_sandbox.py::create) enregistrent ici une ligne. Sert de trace
persistante : plan_vpcs.target_vpc_id et sandbox_vpcs.vpc_id n'exposent que
le VPC ACTUEL d'un bloc, pas l'historique — un VPC recréé (ou dont la
suppression a échoué en partie) devient orphelin dans la configuration
courante sans que rien ne le signale ailleurs. Affiché sur la page
d'administration Ressources VPC (app/routers/admin.py::vpc_registry_view) ;
scripts/refresh_vpc_registry.py rafraîchit toutes les heures (voir
app/cron.py) la présence réelle de chaque VPC sur son compte/région cible."""
from datetime import datetime, timezone

from app.db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def record_created(
    vpc_id: str, origin: str, plan_id: int, plan_name: str,
    account_ak: str, region: str, created_by: str | None,
    plan_vpc_id: int | None = None, sandbox_id: int | None = None,
) -> None:
    """`origin` : 'plan_target' ou 'sandbox'. Idempotent (ON CONFLICT DO
    NOTHING sur vpc_id) : un ID de VPC n'est jamais réutilisé par l'API,
    donc un conflit ne peut venir que d'un double appel accidentel."""
    if not vpc_id:
        return
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO target_vpc_registry
            (vpc_id, origin, plan_id, plan_name, plan_vpc_id, sandbox_id, account_ak, region, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(vpc_id) DO NOTHING
        """,
        (vpc_id, origin, plan_id, plan_name, plan_vpc_id, sandbox_id, account_ak, region, created_by, _now()),
    )
    conn.commit()
    conn.close()


def record_delete_attempt(vpc_id: str) -> None:
    if not vpc_id:
        return
    conn = get_connection()
    conn.execute("UPDATE target_vpc_registry SET delete_attempted_at = ? WHERE vpc_id = ?", (_now(), vpc_id))
    conn.commit()
    conn.close()


def record_delete_result(vpc_id: str, result: str) -> None:
    if not vpc_id:
        return
    conn = get_connection()
    conn.execute("UPDATE target_vpc_registry SET delete_last_result = ? WHERE vpc_id = ?", (result, vpc_id))
    conn.commit()
    conn.close()


def _mark_checked(vpc_ids: list[str], checked_at: str, present: bool | None, error: str | None) -> None:
    if not vpc_ids:
        return
    conn = get_connection()
    conn.executemany(
        "UPDATE target_vpc_registry SET present = ?, checked_at = ?, check_error = ? WHERE vpc_id = ?",
        [(None if present is None else int(present), checked_at, error, vpc_id) for vpc_id in vpc_ids],
    )
    conn.commit()
    conn.close()


def refresh_all() -> None:
    """Appelé une fois par heure par cron (voir app/cron.py, script
    scripts/refresh_vpc_registry.py) : vérifie, pour chaque VPC du
    registre, s'il existe toujours réellement sur son compte/région
    cible — un VPC supprimé manuellement dans la console, ou dont la
    suppression a échoué en partie, doit apparaître comme tel plutôt que
    de rester silencieusement dans l'inconnu. Un seul appel ReadVpcs par
    plan (pas par VPC) : les VPC du registre sont groupés par plan_id
    avant vérification."""
    from app import octl  # import différé : octl ne dépend pas de nous, mais garde le style local à la fonction
    from app.target import resolve_target_credentials  # idem, évite un cycle avec target.py au chargement du module

    conn = get_connection()
    rows = [dict(row) for row in conn.execute("SELECT vpc_id, plan_id FROM target_vpc_registry").fetchall()]
    plans_by_id = {row["id"]: row for row in conn.execute("SELECT * FROM plans").fetchall()}
    conn.close()

    now = _now()
    vpc_ids_by_plan: dict[int, list[str]] = {}
    for row in rows:
        vpc_ids_by_plan.setdefault(row["plan_id"], []).append(row["vpc_id"])

    for plan_id, vpc_ids in vpc_ids_by_plan.items():
        plan = plans_by_id.get(plan_id)
        if plan is None:
            _mark_checked(vpc_ids, now, present=None, error="Plan introuvable (supprimé depuis).")
            continue
        if not octl.is_available():
            _mark_checked(vpc_ids, now, present=None, error="octl n'est pas installé sur ce serveur.")
            continue
        target_ak, target_sk, target_region, cred_error = resolve_target_credentials(plan)
        if cred_error:
            _mark_checked(vpc_ids, now, present=None, error=cred_error)
            continue
        try:
            existing_ids = {v.get("NetId") for v in octl.list_vpcs(target_ak, target_sk, target_region)}
        except octl.OctlError as exc:
            _mark_checked(vpc_ids, now, present=None, error=str(exc))
            continue

        present_ids = [vpc_id for vpc_id in vpc_ids if vpc_id in existing_ids]
        absent_ids = [vpc_id for vpc_id in vpc_ids if vpc_id not in existing_ids]
        _mark_checked(present_ids, now, present=True, error=None)
        _mark_checked(absent_ids, now, present=False, error=None)
