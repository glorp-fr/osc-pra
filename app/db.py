import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "osc-pra.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
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
            target_type TEXT NOT NULL DEFAULT 'meme_region',
            target_region TEXT,
            sync_endpoint TEXT,
            sync_bucket TEXT,
            sync_ak TEXT,
            sync_sk_encrypted TEXT,
            target_retain_count INTEGER,
            snapshot_frequency TEXT,
            source_retain_count INTEGER,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
