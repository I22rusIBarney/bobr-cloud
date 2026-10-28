#!/usr/bin/env bash
set -Eeuo pipefail

C_GREEN='\033[0;32m'; C_CYAN='\033[0;36m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_RESET='\033[0m'
info(){ printf "${C_CYAN}➜${C_RESET} %s\n" "$*"; }
ok(){ printf "${C_GREEN}✔${C_RESET} %s\n" "$*"; }
warn(){ printf "${C_YELLOW}! ${C_RESET}%s\n" "$*"; }
die(){ printf "${C_RED}✖ %s${C_RESET}\n" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo ./monitoring/bobr-checker/install.sh"
[[ -r /etc/os-release ]] || die "Не удалось определить ОС."
. /etc/os-release
case "${ID:-}" in ubuntu|debian) ;; *) die "Поддерживаются Ubuntu и Debian. Обнаружено: ${PRETTY_NAME:-unknown}";; esac

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/bobr-checker"
command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y curl; }

if ! command -v docker >/dev/null 2>&1; then
  info "Устанавливаю Docker Engine..."
  curl -fsSL https://get.docker.com | sh
fi
command -v docker >/dev/null 2>&1 || die "Docker не установлен."
if ! docker compose version >/dev/null 2>&1; then
  info "Устанавливаю Docker Compose plugin..."
  apt-get update -qq
  if ! apt-get install -y docker-compose-plugin; then
    ARCH="$(uname -m)"
    case "$ARCH" in x86_64) COMPOSE_ARCH=x86_64 ;; aarch64|arm64) COMPOSE_ARCH=aarch64 ;; *) die "Неизвестная архитектура для Docker Compose: $ARCH" ;; esac
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -fL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$COMPOSE_ARCH" -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod 755 /usr/local/lib/docker/cli-plugins/docker-compose
  fi
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose plugin не найден."
systemctl enable --now docker >/dev/null 2>&1 || true

if [[ -e "$INSTALL_DIR/.env" ]]; then
  warn "Найдена существующая установка. Настройки будут сохранены."
  read -r -p "Обновить файлы и пересоздать контейнер? [y/N]: " CONFIRM
  [[ "$CONFIRM" =~ ^[YyДд]$ ]] || exit 0
  cp -a "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.backup.$(date +%Y%m%d%H%M%S)"
else
  mkdir -p "$INSTALL_DIR"
fi

mkdir -p "$INSTALL_DIR/geo" "$INSTALL_DIR/custom"
cp "$SRC_DIR/docker-compose.yml" "$INSTALL_DIR/docker-compose.yml"
cp "$SRC_DIR/README.md" "$INSTALL_DIR/README.md"
cp "$SRC_DIR/custom/"* "$INSTALL_DIR/custom/"
chown -R 1000:1000 "$INSTALL_DIR/geo" "$INSTALL_DIR/custom"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  printf '\nBOBR CLOUD CHECKER — первоначальная настройка\n\n'
  while :; do
    read -r -p "URL подписки: " SUB_URL
    [[ "$SUB_URL" =~ ^https?:// ]] && break
    warn "Нужна ссылка, начинающаяся с http:// или https://."
  done
  read -r -p "Порт веб-панели [2112]: " HOST_PORT
  HOST_PORT="${HOST_PORT:-2112}"
  [[ "$HOST_PORT" =~ ^[0-9]+$ ]] && (( HOST_PORT >= 1 && HOST_PORT <= 65535 )) || die "Некорректный порт."
  read -r -p "Логин администратора [admin]: " AUTH_USER
  AUTH_USER="${AUTH_USER:-admin}"
  [[ "$AUTH_USER" =~ ^[a-zA-Z0-9_.-]+$ ]] || die "Некорректный логин."
  command -v openssl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y openssl; }
  AUTH_PASS="$(openssl rand -hex 18)"
  cat > "$INSTALL_DIR/.env" <<EOF
SUBSCRIPTION_URL='$SUB_URL'
SUBSCRIPTION_UPDATE=true
SUBSCRIPTION_UPDATE_INTERVAL=300
SUBSCRIPTION_JSON_FORMAT=false
PROXY_CHECK_INTERVAL=60
PROXY_CHECK_CONCURRENCY=10
PROXY_CHECK_METHOD=ip
PROXY_TIMEOUT=20
PROXY_IP_CHECK_URL=https://api.ipify.org?format=text
METRICS_HOST=0.0.0.0
METRICS_PORT=2112
METRICS_PROTECTED=true
METRICS_USERNAME=$AUTH_USER
METRICS_PASSWORD=$AUTH_PASS
WEB_PUBLIC=false
WEB_SHOW_DETAILS=true
WEB_CUSTOM_ASSETS_PATH=/app/custom
LOG_LEVEL=info
HOST_PORT=$HOST_PORT
BIND_ADDRESS=127.0.0.1
EOF
  chmod 600 "$INSTALL_DIR/.env"
  AUTH_CREATED=1
else
  AUTH_USER="$(sed -n 's/^METRICS_USERNAME=//p' "$INSTALL_DIR/.env" | tail -n1)"
  AUTH_PASS="$(sed -n 's/^METRICS_PASSWORD=//p' "$INSTALL_DIR/.env" | tail -n1)"
  HOST_PORT="$(sed -n 's/^HOST_PORT=//p' "$INSTALL_DIR/.env" | tail -n1)"
  AUTH_CREATED=0
fi

install -m 755 "$SRC_DIR/cli.sh" /usr/local/bin/bobr-checker
info "Загружаю образ и запускаю мониторинг..."
cd "$INSTALL_DIR"
docker compose pull checker
docker compose up -d --remove-orphans
ok "Контейнер запущен."

HOST_PORT="${HOST_PORT:-2112}"
PUBLIC_IP="$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
[[ -n "$PUBLIC_IP" ]] || PUBLIC_IP="IP-АДРЕС-СЕРВЕРА"
for i in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$HOST_PORT/health" >/dev/null 2>&1 && break
  sleep 2
done
if curl -fsS "http://127.0.0.1:$HOST_PORT/health" >/dev/null 2>&1; then ok "Проверка /health пройдена."; else warn "Панель пока не отвечает. Проверьте: bobr-checker logs"; fi

printf '\nBOBR CLOUD CHECKER УСТАНОВЛЕН\n'
printf 'Панель: http://127.0.0.1:%s (доступ только локально)\nЛогин:  %s\n' "$HOST_PORT" "$AUTH_USER"
printf 'SSH-туннель: ssh -L %s:127.0.0.1:%s root@%s\n' "$HOST_PORT" "$HOST_PORT" "$PUBLIC_IP"
if [[ "${AUTH_CREATED:-0}" == 1 ]]; then
  printf 'Пароль: %s\nСохраните его сейчас — повторно он не выводится.\n' "$AUTH_PASS"
else
  printf 'Пароль сохранён в %s/.env\n' "$INSTALL_DIR"
fi
printf '\nУправление: bobr-checker help\nНастройки: %s/.env\n' "$INSTALL_DIR"
warn "Панель привязана к localhost. Для постоянного доступа настройте HTTPS reverse proxy (например, Caddy)."
