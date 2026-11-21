#!/usr/bin/env bash
# Создаёт в Oracle Cloud сеть и бесплатный (Always Free) сервер для Бинго.
# Сначала войдите:  oci session authenticate --region eu-frankfurt-1 --profile-name bingo
# Запуск:           bash deploy/oci-create-vm.sh [публичный SSH-ключ, по умолчанию ~/.ssh/id_ed25519.pub]
# Повторный запуск безопасен: уже созданное переиспользуется.
set -euo pipefail

PROFILE="${OCI_PROFILE:-bingo}"
SSH_PUBKEY="${1:-$HOME/.ssh/id_ed25519.pub}"
SHAPE="VM.Standard.E2.1.Micro"  # Always Free

oci() { command oci --profile "$PROFILE" --auth security_token "$@"; }

TENANCY=$(awk -v p="[$PROFILE]" '$0==p{f=1;next} /^\[/{f=0} f && /^tenancy/{sub(/^tenancy *= */,""); print}' ~/.oci/config)
[ -n "$TENANCY" ] || { echo "Не найден профиль $PROFILE в ~/.oci/config — выполните oci session authenticate"; exit 1; }

echo "Сеть..."
VCN=$(oci network vcn list --compartment-id "$TENANCY" --display-name bingo-vcn --query 'data[0].id' --raw-output 2>/dev/null || true)
if [ -z "$VCN" ]; then
  VCN=$(oci network vcn create --compartment-id "$TENANCY" --display-name bingo-vcn \
    --cidr-blocks '["10.0.0.0/16"]' --dns-label bingo --wait-for-state AVAILABLE --query data.id --raw-output)
fi
RT=$(oci network vcn get --vcn-id "$VCN" --query 'data."default-route-table-id"' --raw-output)
SL=$(oci network vcn get --vcn-id "$VCN" --query 'data."default-security-list-id"' --raw-output)

IGW=$(oci network internet-gateway list --compartment-id "$TENANCY" --vcn-id "$VCN" --query 'data[0].id' --raw-output 2>/dev/null || true)
if [ -z "$IGW" ]; then
  IGW=$(oci network internet-gateway create --compartment-id "$TENANCY" --vcn-id "$VCN" --is-enabled true \
    --display-name bingo-igw --wait-for-state AVAILABLE --query data.id --raw-output)
fi
oci network route-table update --rt-id "$RT" --force \
  --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW\"}]" >/dev/null

# Входящие: SSH, HTTP, HTTPS + служебный ICMP; исходящие: всё
oci network security-list update --security-list-id "$SL" --force \
  --ingress-security-rules '[
    {"source":"0.0.0.0/0","protocol":"6","tcpOptions":{"destinationPortRange":{"min":22,"max":22}}},
    {"source":"0.0.0.0/0","protocol":"6","tcpOptions":{"destinationPortRange":{"min":80,"max":80}}},
    {"source":"0.0.0.0/0","protocol":"6","tcpOptions":{"destinationPortRange":{"min":443,"max":443}}},
    {"source":"0.0.0.0/0","protocol":"1","icmpOptions":{"type":3,"code":4}},
    {"source":"10.0.0.0/16","protocol":"1","icmpOptions":{"type":3}}
  ]' \
  --egress-security-rules '[{"destination":"0.0.0.0/0","protocol":"all"}]' >/dev/null

SUBNET=$(oci network subnet list --compartment-id "$TENANCY" --vcn-id "$VCN" --display-name bingo-subnet --query 'data[0].id' --raw-output 2>/dev/null || true)
if [ -z "$SUBNET" ]; then
  SUBNET=$(oci network subnet create --compartment-id "$TENANCY" --vcn-id "$VCN" --cidr-block 10.0.0.0/24 \
    --display-name bingo-subnet --dns-label web --wait-for-state AVAILABLE --query data.id --raw-output)
fi

echo "Образ Ubuntu 24.04..."
IMAGE=$(oci compute image list --compartment-id "$TENANCY" --operating-system "Canonical Ubuntu" \
  --operating-system-version "24.04" --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC --limit 1 \
  --query 'data[0].id' --raw-output)

INSTANCE=$(oci compute instance list --compartment-id "$TENANCY" --display-name bingo --lifecycle-state RUNNING \
  --query 'data[0].id' --raw-output 2>/dev/null || true)
if [ -z "$INSTANCE" ]; then
  for AD in $(oci iam availability-domain list --compartment-id "$TENANCY" --query "join(' ', data[].name)" --raw-output); do
    echo "Создаю сервер в $AD..."
    if INSTANCE=$(oci compute instance launch --availability-domain "$AD" --compartment-id "$TENANCY" \
        --shape "$SHAPE" --subnet-id "$SUBNET" --image-id "$IMAGE" --display-name bingo \
        --assign-public-ip true --ssh-authorized-keys-file "$SSH_PUBKEY" \
        --wait-for-state RUNNING --query data.id --raw-output); then
      break
    fi
    INSTANCE=""
  done
fi
[ -n "$INSTANCE" ] || { echo "Не удалось создать сервер (у Oracle нет свободных мощностей) — попробуйте позже"; exit 1; }

IP=$(oci compute instance list-vnics --instance-id "$INSTANCE" --query 'data[0]."public-ip"' --raw-output)
echo "PUBLIC_IP=$IP"
