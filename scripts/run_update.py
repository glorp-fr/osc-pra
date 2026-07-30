#!/usr/bin/env python3
"""Exécute update.sh et journalise sa sortie comme un job (voir app/jobs.py)
— déclenché depuis Paramètres globaux (bouton « Mettre à jour maintenant »
quand une nouvelle version est détectée disponible, voir
app/version.py::available_update).

Doit être lancé dans une unité systemd transitoire indépendante du service
osc-pra (voir app/routers/admin.py::mise_a_jour_lancer, `systemd-run
--collect`) : update.sh redémarre osc-pra.service, et ce service tourne en
KillMode=control-group (par défaut) — lancé comme un sous-processus normal
de l'app, ce script serait tué par ce redémarrage avant d'avoir fini de
journaliser le résultat.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.jobs import finish_job, log_step, start_job  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def main(tag: str = "") -> None:
    job_id = start_job("update")
    cmd = ["bash", str(APP_DIR / "update.sh")] + ([tag] if tag else [])

    try:
        process = subprocess.Popen(
            cmd, cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in process.stdout:
            line = ANSI_ESCAPE.sub("", line.rstrip())
            if line:
                log_step(job_id, line, level="error" if "ERREUR" in line else "info")
        returncode = process.wait(timeout=300)
    except Exception as exc:
        finish_job(job_id, "error", f"Échec du lancement de la mise à jour : {exc}")
        return

    if returncode == 0:
        finish_job(job_id, "success", "Mise à jour terminée.")
    else:
        finish_job(job_id, "error", f"update.sh a échoué (code {returncode}).")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
