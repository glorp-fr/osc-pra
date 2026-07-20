#!/usr/bin/env python3
"""Sauvegarde planifiée (ou manuelle) de la base SQLite de configuration.

Appelé par cron (voir app/cron.py) selon la fréquence configurée dans
Paramètres, ou directement depuis l'admin pour une sauvegarde manuelle.
Copie la base dans data/backups/, purge au-delà de backup_retain_count,
puis envoie la copie vers le bucket S3 configuré (dédié, ou réutilisation
du bucket de stockage de la configuration) si renseigné.
"""
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crypto import decrypt  # noqa: E402
from app.db import DB_PATH, get_connection  # noqa: E402
from app.jobs import finish_job, start_job  # noqa: E402

BACKUP_DIR = DB_PATH.parent / "backups"


def main() -> None:
    job_id = start_job("backup")

    if not DB_PATH.exists():
        finish_job(job_id, "error", "Fichier de base introuvable.")
        return

    conn = get_connection()
    settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_name = f"osc-pra-{stamp}.db"
    backup_path = BACKUP_DIR / backup_name
    shutil.copy2(DB_PATH, backup_path)

    retain = (settings["backup_retain_count"] if settings else None) or 7
    _prune_local_backups(retain)

    message = f"Sauvegarde locale : {backup_name}"
    try:
        if _upload_to_s3(settings, backup_path, backup_name):
            message += " (envoyée vers S3)"
    except Exception as exc:
        finish_job(job_id, "error", f"{message} — échec de l'envoi S3 : {exc}")
        return

    finish_job(job_id, "success", message)


def _prune_local_backups(retain: int) -> None:
    backups = sorted(BACKUP_DIR.glob("osc-pra-*.db"), reverse=True)
    for old in backups[retain:]:
        old.unlink(missing_ok=True)


def _upload_to_s3(settings, backup_path: Path, backup_name: str) -> bool:
    if settings is None:
        return False

    if settings["backup_bucket_mode"] == "dedicated":
        endpoint, bucket = settings["backup_endpoint"], settings["backup_bucket"]
        ak, sk_encrypted = settings["backup_ak"], settings["backup_sk_encrypted"]
    else:
        endpoint, bucket = settings["s3_endpoint"], settings["s3_bucket"]
        ak, sk_encrypted = settings["s3_ak"], settings["s3_sk_encrypted"]

    if not (endpoint and bucket and ak and sk_encrypted):
        return False

    import boto3

    client = boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=ak, aws_secret_access_key=decrypt(sk_encrypted),
    )
    client.upload_file(str(backup_path), bucket, backup_name)
    return True


if __name__ == "__main__":
    main()
