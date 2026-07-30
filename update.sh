#!/usr/bin/env bash
# Met à jour Osc-PRA vers une version taguée (voir CLAUDE.MD, section
# « Versioning et mises à jour ») : fetch des tags, checkout, mise à jour des
# dépendances Python, redémarrage du service. Les migrations de schéma
# (ajout de colonnes) sont appliquées automatiquement au démarrage de l'app
# (app/db.py, init_db/EXPECTED_COLUMNS) — rien à faire ici de ce côté.
#
# Usage : ./update.sh [tag]
#   Sans argument : met à jour vers le dernier tag vX.Y.Z disponible sur origin.
#   Avec argument : met à jour vers ce tag précis (rollback possible en
#   pointant vers un tag antérieur au tag actuellement installé).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="osc-pra"
TARGET_REF="${1:-}"

log()  { echo -e "\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m!!\033[0m $*"; }
die()  { echo -e "\033[1;31mERREUR:\033[0m $*" >&2; exit 1; }

cd "$APP_DIR"

[ -d .git ] || die "Ce script doit être exécuté depuis un clone git de osc-pra (répertoire $APP_DIR)."

CURRENT_VERSION="$(cat VERSION 2>/dev/null || echo inconnue)"
log "Version actuellement installée : $CURRENT_VERSION"

if ! git diff --quiet || ! git diff --cached --quiet; then
    die "Modifications locales non commitées détectées (git status) — commit ou stash-les avant de mettre à jour."
fi

log "Récupération des tags depuis origin"
git fetch --quiet --tags origin

if [ -z "$TARGET_REF" ]; then
    TARGET_REF="$(git tag --list 'v*' --sort=-v:refname | head -1)"
    [ -n "$TARGET_REF" ] || die "Aucun tag de version (vX.Y.Z) trouvé sur origin — précise-en un explicitement : ./update.sh <tag>."
fi

git rev-parse --verify --quiet "refs/tags/${TARGET_REF}" >/dev/null \
    || die "Tag '$TARGET_REF' introuvable (git tag --list pour voir les tags connus après fetch)."

log "Mise à jour vers $TARGET_REF"
git checkout --quiet "$TARGET_REF"

NEW_VERSION="$(cat VERSION 2>/dev/null || echo inconnue)"

log "Mise à jour des dépendances Python"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade -r "$APP_DIR/requirements.txt"

log "Redémarrage du service $SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Terminé : $CURRENT_VERSION -> $NEW_VERSION ($TARGET_REF), service actif."
else
    die "Le service $SERVICE_NAME n'est pas actif après redémarrage — vérifie 'systemctl status $SERVICE_NAME' et 'journalctl -u $SERVICE_NAME'."
fi
