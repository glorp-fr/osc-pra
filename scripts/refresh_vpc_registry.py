#!/usr/bin/env python3
"""Rafraîchissement horaire de la présence réelle des VPC cible du registre
(app/vpc_registry.py, page Ressources VPC de l'administration).

Appelé par cron toutes les heures (voir app/cron.py). Peut aussi être
lancé manuellement : `venv/bin/python3 scripts/refresh_vpc_registry.py`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.vpc_registry import refresh_all  # noqa: E402

if __name__ == "__main__":
    refresh_all()
