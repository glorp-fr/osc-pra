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
    _migrate_schema(conn)
    conn.commit()
    conn.close()
