#!/usr/bin/env bash
# Instala la app en una VM Ubuntu de Oracle Cloud (Always Free, región Frankfurt).
# Uso, dentro de la VM:   curl -fsSL https://raw.githubusercontent.com/JLPROYECTOS/trading/main/deploy/oracle-setup.sh | sudo bash
# Después: sudo nano /etc/trading.env  (pega tus claves, PIN y contraseña)  y  sudo systemctl restart trading
set -euo pipefail

REPO="https://github.com/JLPROYECTOS/trading.git"
DIR=/opt/trading

apt-get update -y
apt-get install -y python3 python3-venv git debian-keyring debian-archive-keyring apt-transport-https curl

# Código y dependencias
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull; else git clone "$REPO" "$DIR"; fi
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
mkdir -p /var/lib/trading

# Configuración (solo la crea si no existe; NUNCA la sube a GitHub)
if [ ! -f /etc/trading.env ]; then
  cat > /etc/trading.env <<'EOF'
BYBIT_ENV=eu
BYBIT_API_KEY=
BYBIT_API_SECRET=
APP_PIN=
APP_PASSWORD=
HOST=127.0.0.1
APP_PORT=8765
TRADING_DATA_DIR=/var/lib/trading
MAX_LEVERAGE=4
EOF
  chmod 600 /etc/trading.env
fi

# Servicio que arranca solo y se reinicia si falla
cat > /etc/systemd/system/trading.service <<EOF
[Unit]
Description=Senales de trading
After=network-online.target

[Service]
EnvironmentFile=/etc/trading.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/.venv/bin/python $DIR/app/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# HTTPS automático con Caddy y un dominio gratuito <ip>.sslip.io
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y && apt-get install -y caddy
IP=$(curl -fsS https://api.ipify.org)
DOMAIN="$(echo "$IP" | tr . -).sslip.io"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:8765
}
EOF

# Las imágenes Ubuntu de Oracle bloquean 80/443 con iptables: abrirlos
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT || true
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
command -v netfilter-persistent >/dev/null && netfilter-persistent save || true

systemctl daemon-reload
systemctl enable --now trading
systemctl restart caddy

echo
echo "Listo. Tu app: https://$DOMAIN"
echo "1) sudo nano /etc/trading.env   -> pega BYBIT_API_KEY, BYBIT_API_SECRET, APP_PIN y APP_PASSWORD"
echo "2) sudo systemctl restart trading"
echo "3) En la consola de Oracle abre los puertos 80 y 443 (Security List de la VCN) si no lo has hecho."
