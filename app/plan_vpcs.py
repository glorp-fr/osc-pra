"""CRUD des VPC d'un plan de reprise (voir CLAUDE.MD, section « Plans
multi-VPC ») : un plan peut désormais répliquer plusieurs VPC source, chacun
avec son propre VPC cible, son AZ cible et sa sélection de VMs — portés par
la table `plan_vpcs` plutôt que par les colonnes `plans.source_vpc_id` /
`target_vpc_id` / `target_subregion` / `selected_vms` / `vm_image_overrides`
(conservées pour compatibilité mais plus lues, voir app/db.py::_migrate_plan_vpcs).
"""
import json

from app.db import get_connection


def list_plan_vpcs(plan_id: int) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM plan_vpcs WHERE plan_id = ? ORDER BY position, id", (plan_id,)
    ).fetchall()
    conn.close()
    return rows


def get_plan_vpc(plan_vpc_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM plan_vpcs WHERE id = ?", (plan_vpc_id,)).fetchone()
    conn.close()
    return row


def create_plan_vpc(plan_id: int) -> int:
    conn = get_connection()
    position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM plan_vpcs WHERE plan_id = ?", (plan_id,)
    ).fetchone()["next"]
    cur = conn.execute(
        "INSERT INTO plan_vpcs (plan_id, position) VALUES (?, ?)", (plan_id, position)
    )
    plan_vpc_id = cur.lastrowid
    conn.commit()
    conn.close()
    return plan_vpc_id


def update_plan_vpc(plan_vpc_id: int, **fields) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    conn = get_connection()
    conn.execute(f"UPDATE plan_vpcs SET {columns} WHERE id = ?", (*fields.values(), plan_vpc_id))
    conn.commit()
    conn.close()


def delete_plan_vpc(plan_vpc_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM plan_vpcs WHERE id = ?", (plan_vpc_id,))
    conn.execute("DELETE FROM plan_vpc_resource_scan_cache WHERE plan_vpc_id = ?", (plan_vpc_id,))
    conn.commit()
    conn.close()


def selected_vms_of(plan_vpc) -> list:
    return json.loads(plan_vpc["selected_vms"] or "[]")


def vm_image_overrides_of(plan_vpc) -> dict:
    return json.loads(plan_vpc["vm_image_overrides"] or "{}")
