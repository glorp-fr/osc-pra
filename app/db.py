import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "osc-pra.db"

# Colonnes attendues par table : permet d'ajouter une colonne à une base
# existante (créée par une version antérieure) via ALTER TABLE, puisque
# CREATE TABLE IF NOT EXISTS ne touche pas aux tables déjà présentes.
EXPECTED_COLUMNS = {
    "plans": {
        "source_region": "TEXT",
        "selected_vms": "TEXT",
        "source_vpc_id": "TEXT",
        "target_ak": "TEXT",
        "target_sk_encrypted": "TEXT",
        "target_vpc_id": "TEXT",
        "target_subregion": "TEXT",
        "vm_image_overrides": "TEXT",
        "auto_sync_target": "INTEGER NOT NULL DEFAULT 1",
        "vm_restart_order": "TEXT",
        "failover_status": "TEXT NOT NULL DEFAULT 'normal'",
        "failover_state": "TEXT",
    },
    "users": {
        "role": "TEXT NOT NULL DEFAULT 'operateur'",
    },
    "vm_targets": {
        "restored_snapshot_id": "TEXT",
        "restored_at": "TEXT",
    },
}


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    for table, columns in EXPECTED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, col_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                if table == "users" and column == "role":
                    conn.execute("UPDATE users SET role = 'admin' WHERE is_admin = 1")


def init_db() -> None:
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL DEFAULT 'operateur',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mail_from TEXT,
            mail_to TEXT,
            smtp_server TEXT,
            storage_mode TEXT NOT NULL DEFAULT 'local',
            s3_endpoint TEXT,
            s3_bucket TEXT,
            s3_ak TEXT,
            s3_sk_encrypted TEXT,
            backup_bucket_mode TEXT NOT NULL DEFAULT 'reuse',
            backup_endpoint TEXT,
            backup_bucket TEXT,
            backup_ak TEXT,
            backup_sk_encrypted TEXT,
            backup_frequency TEXT,
            backup_retain_count INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            source_ak TEXT,
            source_sk_encrypted TEXT,
            source_region TEXT,
            selected_vms TEXT,
            source_vpc_id TEXT,
            target_type TEXT NOT NULL DEFAULT 'meme_region',
            target_region TEXT,
            target_ak TEXT,
            target_sk_encrypted TEXT,
            target_vpc_id TEXT,
            target_subregion TEXT,
            vm_image_overrides TEXT,
            sync_endpoint TEXT,
            sync_bucket TEXT,
            sync_ak TEXT,
            sync_sk_encrypted TEXT,
            target_retain_count INTEGER,
            snapshot_frequency TEXT,
            source_retain_count INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            auto_sync_target INTEGER NOT NULL DEFAULT 1,
            vm_restart_order TEXT,
            failover_status TEXT NOT NULL DEFAULT 'normal',
            failover_state TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vm_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            source_vm_id TEXT NOT NULL,
            target_vm_id TEXT NOT NULL,
            restored_snapshot_id TEXT,
            restored_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(plan_id, source_vm_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS resource_scan_cache (
            plan_id INTEGER PRIMARY KEY,
            subnets_count INTEGER,
            security_groups_count INTEGER,
            route_tables_count INTEGER,
            internet_services_count INTEGER,
            scanned_at TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL,
            plan_id INTEGER,
            plan_name TEXT,
            vm_id TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            message TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            logged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            level TEXT NOT NULL DEFAULT 'info',
            message TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            ip_address TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sandboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            vpc_id TEXT,
            status TEXT NOT NULL DEFAULT 'creating',
            state TEXT,
            error TEXT,
            job_id INTEGER,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_vm_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sandbox_id INTEGER NOT NULL,
            source_vm_id TEXT NOT NULL,
            target_vm_id TEXT NOT NULL,
            restored_snapshot_id TEXT,
            restored_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sandbox_id, source_vm_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_vpcs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            source_vpc_id TEXT,
            target_vpc_id TEXT,
            target_subregion TEXT,
            selected_vms TEXT NOT NULL DEFAULT '[]',
            vm_image_overrides TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plan_vpcs_plan_id ON plan_vpcs(plan_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_vpc_resource_scan_cache (
            plan_vpc_id INTEGER PRIMARY KEY,
            subnets_count INTEGER,
            security_groups_count INTEGER,
            route_tables_count INTEGER,
            internet_services_count INTEGER,
            scanned_at TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_vpcs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sandbox_id INTEGER NOT NULL,
            plan_vpc_id INTEGER NOT NULL,
            vpc_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sandbox_vpcs_sandbox_id ON sandbox_vpcs(sandbox_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS target_vpc_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vpc_id TEXT NOT NULL UNIQUE,
            origin TEXT NOT NULL,
            plan_id INTEGER,
            plan_name TEXT,
            plan_vpc_id INTEGER,
            sandbox_id INTEGER,
            account_ak TEXT,
            region TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            delete_attempted_at TEXT,
            delete_last_result TEXT,
            present INTEGER,
            checked_at TEXT,
            check_error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_target_vpc_registry_plan_id ON target_vpc_registry(plan_id)")
    _migrate_schema(conn)
    _migrate_plan_vpcs(conn)
    _migrate_target_vpc_registry(conn)
    conn.commit()
    conn.close()


def _migrate_plan_vpcs(conn: sqlite3.Connection) -> None:
    """Bascule additive vers le modèle multi-VPC (voir CLAUDE.MD, section
    « Plans multi-VPC ») : un plan créé par une version antérieure n'a qu'un
    seul VPC, porté par les colonnes historiques `plans.source_vpc_id` /
    `target_vpc_id` / `target_subregion` / `selected_vms` /
    `vm_image_overrides`. On les recopie ici dans une ligne `plan_vpcs` pour
    que le plan continue à fonctionner sans intervention manuelle après mise
    à jour — idempotent (`WHERE NOT EXISTS`), donc sans effet une fois fait.
    Les colonnes historiques ne sont jamais supprimées (politique de
    compatibilité) : elles restent inertes une fois la ligne `plan_vpcs`
    créée, plus rien ne les lit."""
    conn.execute(
        """
        INSERT INTO plan_vpcs (plan_id, source_vpc_id, target_vpc_id, target_subregion, selected_vms, vm_image_overrides)
        SELECT id, source_vpc_id, target_vpc_id, target_subregion,
               COALESCE(NULLIF(selected_vms, ''), '[]'), COALESCE(NULLIF(vm_image_overrides, ''), '{}')
        FROM plans
        WHERE source_vpc_id IS NOT NULL AND source_vpc_id != ''
          AND NOT EXISTS (SELECT 1 FROM plan_vpcs WHERE plan_vpcs.plan_id = plans.id)
        """
    )


def _migrate_target_vpc_registry(conn: sqlite3.Connection) -> None:
    """Rétroactif : les VPC cible créés avant l'introduction du registre
    (voir app/vpc_registry.py) n'y ont pas de ligne — cette migration les y
    ajoute une fois à partir de ce qui est encore référencé aujourd'hui
    (idempotent, `vpc_id` est UNIQUE sur la table). `created_by` reste NULL
    pour les VPC cible de plan (pas d'historique de qui a cliqué avant ce
    registre) — connu en revanche pour les sandbox (sandboxes.created_by),
    y compris celles créées avant la prise en charge du multi-VPC (pas de
    ligne `sandbox_vpcs`, VPC porté par l'ancienne colonne
    `sandboxes.vpc_id` — voir scripts/run_sandbox.py)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO target_vpc_registry (vpc_id, origin, plan_id, plan_name, plan_vpc_id, created_at)
        SELECT plan_vpcs.target_vpc_id, 'plan_target', plans.id, plans.name, plan_vpcs.id, plan_vpcs.created_at
        FROM plan_vpcs JOIN plans ON plans.id = plan_vpcs.plan_id
        WHERE plan_vpcs.target_vpc_id IS NOT NULL AND plan_vpcs.target_vpc_id != ''
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO target_vpc_registry
            (vpc_id, origin, plan_id, plan_name, plan_vpc_id, sandbox_id, created_by, created_at)
        SELECT sandbox_vpcs.vpc_id, 'sandbox', plans.id, plans.name, sandbox_vpcs.plan_vpc_id,
               sandbox_vpcs.sandbox_id, sandboxes.created_by, sandbox_vpcs.created_at
        FROM sandbox_vpcs
        JOIN sandboxes ON sandboxes.id = sandbox_vpcs.sandbox_id
        JOIN plans ON plans.id = sandboxes.plan_id
        WHERE sandbox_vpcs.vpc_id IS NOT NULL AND sandbox_vpcs.vpc_id != ''
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO target_vpc_registry (vpc_id, origin, plan_id, plan_name, sandbox_id, created_by, created_at)
        SELECT sandboxes.vpc_id, 'sandbox', plans.id, plans.name, sandboxes.id, sandboxes.created_by, sandboxes.created_at
        FROM sandboxes JOIN plans ON plans.id = sandboxes.plan_id
        WHERE sandboxes.vpc_id IS NOT NULL AND sandboxes.vpc_id != ''
          AND NOT EXISTS (SELECT 1 FROM sandbox_vpcs WHERE sandbox_vpcs.sandbox_id = sandboxes.id)
        """
    )
