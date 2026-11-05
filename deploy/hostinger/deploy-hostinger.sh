#!/usr/bin/env bash
# VerifyHub — Hostinger VPS setup script (Ubuntu 22.04 / 24.04).
#
# Run this ON the VPS (SSH in, or use Hostinger's browser terminal).
# Before running:
#   1. Point your domain's A record at this VPS IP (Hostinger hPanel → DNS).
#   2. Upload this whole project zip to the VPS and unzip it, e.g.:
#        scp VerifyHub-hostinger-render.zip root@YOUR_VPS_IP:/root/
#        ssh root@YOUR_VPS_IP
#        cd /root && unzip -o VerifyHub-hostinger-render.zip
#      (or upload with FileZilla to /root/)
#   3. Edit DOMAIN below to your real domain.
#
# The script installs Python + Nginx + Certbot, creates /opt/verifyhub,
# installs the API dependencies, and installs the systemd + Nginx files.
# It does NOT fill in your .env secrets — you do that by hand after
# (see the checklist it prints at the end).
set -euo pipefail

DOMAIN="YOURDOMAIN"
SRC_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # project root (verification-platform/)
APP_DIR="/opt/verifyhub"

if [ "$DOMAIN" = "YOURDOMAIN" ]; then
  echo "STOP: edit DOMAIN at the top of this script to your real domain first."
  exit 1
fi

echo "== 1. System packages =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx unzip

echo "== 2. App files -> $APP_DIR =="
mkdir -p "$APP_DIR"
# Remove old copies first so re-running the script never nests folders.
rm -rf "$APP_DIR/backend" "$APP_DIR/frontend" "$APP_DIR/deploy"
cp -r "$SRC_DIR/backend" "$SRC_DIR/frontend" "$SRC_DIR/render.yaml" "$SRC_DIR/.python-version" "$APP_DIR/"
cp -r "$SRC_DIR/deploy" "$APP_DIR/deploy"
chown -R www-data:www-data "$APP_DIR"
chmod 750 "$APP_DIR"

echo "== 3. Python venv + API dependencies =="
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements-api.txt"

echo "== 4. .env =="
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/deploy/hostinger/.env.hostinger.example" "$APP_DIR/.env"
  chown www-data:www-data "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "Created $APP_DIR/.env from the example — YOU MUST FILL IT IN."
else
  echo "$APP_DIR/.env already exists — leaving it alone."
fi

echo "== 5. systemd service =="
cp "$APP_DIR/deploy/hostinger/verifyhub-api.service" /etc/systemd/system/verifyhub-api.service
systemctl daemon-reload
systemctl enable verifyhub-api

echo "== 6. Nginx site =="
sed "s/YOURDOMAIN/$DOMAIN/g" "$APP_DIR/deploy/hostinger/nginx-verifyhub.conf" > /etc/nginx/sites-available/verifyhub
ln -sf /etc/nginx/sites-available/verifyhub /etc/nginx/sites-enabled/verifyhub
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo ""
echo "== NEXT STEPS (do these by hand) =="
echo "1. Edit the secrets file (never share it in chat):"
echo "     nano $APP_DIR/.env"
echo "   Fill BOT_TOKEN, REGISTRATION_URL, ADMIN_TELEGRAM_IDS, MINI_APP_URL=https://$DOMAIN/"
echo "   and DATABASE_URL = the EXTERNAL URL from Render PostgreSQL."
echo "2. Start the API:"
echo "     systemctl restart verifyhub-api"
echo "     systemctl status verifyhub-api   (should say active)"
echo "     curl http://127.0.0.1:8000/api/health   (should print {\"ok\":true})"
echo "3. HTTPS (Telegram needs https):"
echo "     certbot --nginx -d $DOMAIN"
echo "4. Test in a browser: https://$DOMAIN/  and  https://$DOMAIN/api/health"
echo "5. Then do the Render bot side + BotFather (see deploy/DEPLOY-GUIDE.md)."
