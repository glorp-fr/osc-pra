#!/usr/bin/env bash
# Script de setup Osc-PRA : dépendances, compte admin, service systemd,
# HTTPS optionnel (Let's Encrypt).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
APP_PORT=8000

log()  { echo -e "\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m!!\033[0m $*"; }
die()  { echo -e "\033[1;31mERREUR:\033[0m $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Ce script doit être exécuté avec sudo/root (apt, systemd, nginx)."
command -v apt-get >/dev/null 2>&1 || die "Ce script suppose une distribution basée sur apt (Debian/Ubuntu)."

log "Installation des dépendances système"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 curl >/dev/null

log "Création de l'environnement virtuel Python"
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER" "$APP_DIR/data" "$APP_DIR/venv"

log "Création du compte admin"
sudo -u "$SERVICE_USER" "$APP_DIR/venv/bin/python" "$APP_DIR/scripts/create_admin.py"

log "Installation du service systemd"
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    "$APP_DIR/deploy/osc-pra.service.template" > /etc/systemd/system/osc-pra.service
systemctl daemon-reload
systemctl enable --quiet osc-pra
systemctl restart osc-pra

log "Installation de nginx (reverse proxy)"
apt-get install -y -qq nginx >/dev/null

ENABLE_HTTPS=""
read -rp "Activer HTTPS (Let's Encrypt) ? [y/N] " ENABLE_HTTPS
if [[ "$ENABLE_HTTPS" =~ ^[Yy]$ ]]; then
    read -rp "Nom de domaine (FQDN) pointant vers cette machine : " FQDN
    [ -n "$FQDN" ] || die "FQDN requis pour activer HTTPS."
    read -rp "Email pour l'enregistrement Let's Encrypt : " LE_EMAIL
    [ -n "$LE_EMAIL" ] || die "Email requis pour Let's Encrypt."
    SERVER_NAME="$FQDN"
else
    SERVER_NAME="_"
fi

cat > /etc/nginx/sites-available/osc-pra <<EOF
server {
    listen 80;
    server_name ${SERVER_NAME};

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/osc-pra /etc/nginx/sites-enabled/osc-pra
nginx -t
systemctl reload nginx

if [[ "$ENABLE_HTTPS" =~ ^[Yy]$ ]]; then
    log "Installation de certbot et obtention du certificat pour ${FQDN}"
    apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    certbot --nginx -d "$FQDN" -m "$LE_EMAIL" --agree-tos --non-interactive --redirect

    warn "Pense à ouvrir les ports 80 (HTTP) et 443 (HTTPS) dans le security group Outscale de cette VM."
    log "Osc-PRA est accessible sur https://${FQDN}"
else
    warn "HTTPS non activé — pense à ouvrir le port 80 (HTTP) dans le security group Outscale de cette VM."
    log "Osc-PRA est accessible en HTTP sur cette VM (port 80)."
fi

log "Setup terminé. Statut du service :"
systemctl status --no-pager osc-pra || true
