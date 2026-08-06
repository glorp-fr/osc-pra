"""Synchronise le crontab de l'utilisateur système avec les fréquences
configurées dans l'application (sauvegarde de la configuration, snapshots
des plans de reprise actifs).

Le crontab est régénéré à chaque modification pertinente (plan créé/modifié/
supprimé/activé/désactivé, paramètres enregistrés) via un bloc balisé
(MARK_BEGIN/MARK_END) : tout ce qui est en dehors de ce bloc dans le crontab
existant de l'utilisateur est préservé.
"""
import subprocess
from pathlib import Path

from app.db import get_connection

APP_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = APP_DIR / "venv" / "bin" / "python3"
LOG_FILE = APP_DIR / "data" / "cron.log"

MARK_BEGIN = "# BEGIN OSC-PRA (généré automatiquement — ne pas éditer à la main)"
MARK_END = "# END OSC-PRA"

# cron (contrairement au service systemd) n'hérite d'aucun PATH utilisable :
# sans cette ligne, le PATH par défaut du démon cron (/usr/bin:/bin) ne
# contient pas /usr/local/bin, où octl est installé, et les jobs échouent
# systématiquement avec "octl n'est pas installé sur ce serveur".
CRON_PATH = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class CronError(Exception):
    pass


def python_bin() -> str:
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else "python3"


def build_cron_lines() -> list[str]:
    conn = get_connection()
    settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    active_plans = conn.execute("SELECT * FROM plans WHERE active = 1 ORDER BY name").fetchall()
    has_source_vpc = conn.execute(
        "SELECT 1 FROM plans WHERE source_vpc_id IS NOT NULL AND source_vpc_id != '' LIMIT 1"
    ).fetchone() is not None
    has_target_vpc_registry = conn.execute("SELECT 1 FROM target_vpc_registry LIMIT 1").fetchone() is not None
    conn.close()

    python = python_bin()
    lines = []

    if settings and settings["backup_frequency"]:
        lines.append(
            f"{settings['backup_frequency']} {python} {APP_DIR}/scripts/run_backup.py >> {LOG_FILE} 2>&1"
        )

    for plan in active_plans:
        if plan["snapshot_frequency"]:
            lines.append(
                f"{plan['snapshot_frequency']} {python} {APP_DIR}/scripts/run_plan.py {plan['id']} "
                f">> {LOG_FILE} 2>&1"
            )

    # Rescan horaire des ressources du VPC source (tous les plans, actifs ou
    # non, dès qu'un VPC source est configuré) — voir app/resource_scan.py.
    if has_source_vpc:
        lines.append(f"0 * * * * {python} {APP_DIR}/scripts/scan_resources.py >> {LOG_FILE} 2>&1")

    # Rescan horaire de la présence réelle des VPC cible du registre (voir
    # app/vpc_registry.py) — décalé de 5 min pour ne pas concurrencer le
    # rescan des VPC source ci-dessus.
    if has_target_vpc_registry:
        lines.append(f"5 * * * * {python} {APP_DIR}/scripts/refresh_vpc_registry.py >> {LOG_FILE} 2>&1")

    return [CRON_PATH, *lines] if lines else []


def sync_crontab() -> None:
    """Régénère le bloc OSC-PRA du crontab de l'utilisateur courant."""
    lines = build_cron_lines()

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CronError("La commande crontab n'est pas disponible sur ce serveur.") from exc

    # Code de retour non nul si l'utilisateur n'a pas encore de crontab : on repart d'un fichier vide.
    current = existing.stdout if existing.returncode == 0 else ""

    kept = []
    skipping = False
    for line in current.splitlines():
        if line.strip() == MARK_BEGIN:
            skipping = True
            continue
        if line.strip() == MARK_END:
            skipping = False
            continue
        if not skipping:
            kept.append(line)

    block = [MARK_BEGIN, *lines, MARK_END] if lines else []
    new_crontab = "\n".join([*kept, *block])
    if new_crontab:
        new_crontab += "\n"

    result = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
    if result.returncode != 0:
        raise CronError(result.stderr.strip() or "Échec de la mise à jour du crontab.")
