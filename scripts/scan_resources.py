#!/usr/bin/env python3
"""Rescan horaire du VPC source de chaque plan (subnets, security groups,
route tables, internet services), pour détecter un écart avec le VPC
cible sur la page Visualiser d'un plan.

Appelé par cron toutes les heures (voir app/cron.py). Peut aussi être
lancé manuellement : `venv/bin/python3 scripts/scan_resources.py`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.resource_scan import scan_all_plans  # noqa: E402

if __name__ == "__main__":
    scan_all_plans()
