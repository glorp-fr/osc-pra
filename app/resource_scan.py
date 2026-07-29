"""Cache du décompte des ressources réseau du VPC source de chaque plan
(subnets, security groups, route tables, internet services), rafraîchi
automatiquement toutes les heures (voir scripts/scan_resources.py et
app/cron.py) et affiché sur la page Visualiser du plan à côté du décompte
du VPC cible (calculé en direct, pas mis en cache), pour repérer un écart
de configuration entre les deux VPC."""
from datetime import datetime, timezone

from app import octl
from app.crypto import decrypt
from app.db import get_connection
from app.target import count_vpc_resources


def scan_and_cache_source(plan) -> None:
    """Scanne le VPC source du plan et met à jour le cache. N'échoue pas
    bruyamment : une erreur (octl indisponible, AK/SK invalide...) est
    stockée dans le cache pour être affichée, plutôt que de faire remonter
    une exception jusqu'à l'appelant (le job planifié traite tous les plans
    à la suite, un plan en échec ne doit pas bloquer les suivants)."""
    if not plan["source_vpc_id"]:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    counts = None
    error = None

    if not octl.is_available():
        error = "octl n'est pas installé sur ce serveur."
    elif not (plan["source_ak"] and plan["source_sk_encrypted"] and plan["source_region"]):
        error = "AK/SK/région source non configurés pour ce plan."
    else:
        try:
            sk = decrypt(plan["source_sk_encrypted"])
            counts = count_vpc_resources(plan["source_ak"], sk, plan["source_region"], plan["source_vpc_id"])
        except octl.OctlError as exc:
            error = str(exc)

    conn = get_connection()
    conn.execute(
        """
        INSERT INTO resource_scan_cache
            (plan_id, subnets_count, security_groups_count, route_tables_count, internet_services_count, scanned_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_id) DO UPDATE SET
            subnets_count = excluded.subnets_count,
            security_groups_count = excluded.security_groups_count,
            route_tables_count = excluded.route_tables_count,
            internet_services_count = excluded.internet_services_count,
            scanned_at = excluded.scanned_at,
            error = excluded.error
        """,
        (
            plan["id"],
            counts["subnets"] if counts else None,
            counts["security_groups"] if counts else None,
            counts["route_tables"] if counts else None,
            counts["internet_services"] if counts else None,
            now,
            error,
        ),
    )
    conn.commit()
    conn.close()


def get_cached_source_counts(plan_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM resource_scan_cache WHERE plan_id = ?", (plan_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def scan_all_plans() -> None:
    """Appelé une fois par heure par cron (voir app/cron.py) : rescanne le
    VPC source de tous les plans qui en ont un configuré."""
    conn = get_connection()
    plans = conn.execute("SELECT * FROM plans WHERE source_vpc_id IS NOT NULL AND source_vpc_id != ''").fetchall()
    conn.close()

    for plan in plans:
        scan_and_cache_source(plan)
