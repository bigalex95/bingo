#!/usr/bin/env bash
# Первичная настройка чистого Ubuntu 24.04 (Oracle Cloud, AWS или любой VPS) под Бинго. Запускается один раз:
#   sudo bash setup-server.sh "<публичный SSH-ключ для деплоя>" [домен]
# Без домена используется <ip-через-дефисы>.sslip.io — HTTPS-сертификат Caddy получит сам.
set -euo pipefail

DEPLOY_PUBKEY="${1:?Укажите публичный SSH-ключ для деплоя}"
DOMAIN="${2:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$DOMAIN" ]; then
  IP=$(curl -4 -sf https://checkip.amazonaws.com | tr -d '[:space:]')
  DOMAIN="${IP//./-}.sslip.io"
fi

export DEBIAN_FRONTEND=noninteractive
# на свежем сервере cloud-init и автообновления ещё держат блокировку apt
command -v cloud-init >/dev/null && cloud-init status --wait >/dev/null || true
apt() { apt-get -o DPkg::Lock::Timeout=600 -y "$@"; }
apt update
apt install python3 rsync curl gnupg debian-keyring debian-archive-keyring apt-transport-https

# В образах Ubuntu от Oracle Cloud iptables по умолчанию пропускает только SSH — открываем 80 и 443
if iptables -S INPUT 2>/dev/null | grep -q REJECT; then
  for port in 80 443; do
    iptables -C INPUT -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 1 -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT
  done
  command -v netfilter-persistent >/dev/null && netfilter-persistent save
fi

# Caddy — reverse proxy с автоматическим HTTPS (официальный репозиторий)
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt update
  apt install caddy
fi

# Пользователь bingo: владеет файлами игры, под ним заходит CI
id bingo >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/bingo --shell /bin/bash bingo
install -d -o bingo -g bingo -m 755 /opt/bingo
install -d -o bingo -g bingo -m 700 /home/bingo/.ssh
printf '%s\n' "$DEPLOY_PUBKEY" > /home/bingo/.ssh/authorized_keys
chown bingo:bingo /home/bingo/.ssh/authorized_keys
chmod 600 /home/bingo/.ssh/authorized_keys

# CI может только перезапускать сервис, больше никаких прав root
cat > /etc/sudoers.d/bingo <<'EOF'
bingo ALL=(root) NOPASSWD: /usr/bin/systemctl restart bingo, /usr/bin/systemctl status bingo
EOF
chmod 440 /etc/sudoers.d/bingo
visudo -cf /etc/sudoers.d/bingo

install -m 644 "$SCRIPT_DIR/bingo.service" /etc/systemd/system/bingo.service
systemctl daemon-reload
systemctl enable bingo  # запустится при первом деплое, когда появится server.py

cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	reverse_proxy 127.0.0.1:8000
}
EOF
systemctl enable caddy
systemctl restart caddy

echo
echo "Готово. Адрес игры: https://$DOMAIN"
