#!/usr/bin/env bash
# Push vers origin/main en utilisant le token stocké dans .github_token
# (fichier ignoré par git, jamais commité). Usage : ./push.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

TOKEN_FILE=".github_token"
[ -f "$TOKEN_FILE" ] || { echo "Fichier $TOKEN_FILE introuvable." >&2; exit 1; }

TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
git push "https://${TOKEN}@github.com/glorp-fr/osc-pra.git" main
