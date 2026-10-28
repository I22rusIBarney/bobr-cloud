#!/usr/bin/env bash
set -Eeuo pipefail

C_GREEN='\033[0;32m'; C_CYAN='\033[0;36m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_RESET='\033[0m'
info(){ printf "${C_CYAN}➜${C_RESET} %s\n" "$*"; }
ok(){ printf "${C_GREEN}✔${C_RESET} %s\n" "$*"; }
warn(){ printf "${C_YELLOW}! ${C_RESET}%s\n" "$*"; }
die(){ printf "${C_RED}✖ %s${C_RESET}\n" "$*" >&2; exit 1; }
set_env() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  [[ ! -f "$INSTALL_DIR/.env" ]] || grep -v "^$key=" "$INSTALL_DIR/.env" > "$tmp" || true
  printf "%s='%s'\n" "$key" "$value" >> "$tmp"
  install -m 600 "$tmp" "$INSTALL_DIR/.env"
  rm -f "$tmp"
}


env_value() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); value=$0 } END { print value }' "$INSTALL_DIR/.env" | tr -d "'\"")"
  printf '%s' "$value"
}
print_credentials() {
  printf '\n========== BOBR CLOUD CHECKER ==========\n'
  printf 'Панель: https://%s\n' "$DOMAIN"
  printf 'Логин:  %s\n' "$AUTH_USER"
  printf 'Пароль: %s\n' "$AUTH_PASS"
  printf '\nКоманды: bobr-checker status | logs | restart | update\n'
  printf 'Логи Caddy: docker logs -f bobr-cloud-caddy\n'
  printf 'Настройки: %s/.env\n' "$INSTALL_DIR"
  printf '========================================\n'
}
check_local_https() {
  local code
  code="$(curl -fsS -o /dev/null -w '%{http_code}' \
    --connect-timeout 3 --max-time 6 \
    --resolve "$DOMAIN:443:127.0.0.1" \
    "https://$DOMAIN/" 2>/dev/null || true)"
  [[ "$code" =~ ^[23][0-9][0-9]$ || "$code" == "401" ]]
}
wait_for_checker() {
  local i
  for i in $(seq 1 45); do
    if docker compose exec -T checker curl -fsS http://127.0.0.1:2112/health >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}
wait_for_https() {
  local i
  for i in $(seq 1 60); do
    if check_local_https; then return 0; fi
    if (( i % 4 == 0 )); then printf '.'; fi
    sleep 5
  done
  printf '\n'
  return 1
}

[[ $EUID -eq 0 ]] || die "Запустите: sudo bash monitoring/bobr-checker/install.sh"
[[ -r /etc/os-release ]] || die "Не удалось определить ОС."
. /etc/os-release
case "${ID:-}" in ubuntu|debian) ;; *) die "Поддерживаются Ubuntu и Debian. Обнаружено: ${PRETTY_NAME:-unknown}";; esac

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/bobr-checker"
command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y curl; }
command -v openssl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y openssl; }

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
    case "$ARCH" in x86_64) COMPOSE_ARCH=x86_64 ;; aarch64|arm64) COMPOSE_ARCH=aarch64 ;; *) die "Неизвестная архитектура Docker Compose: $ARCH" ;; esac
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -fL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$COMPOSE_ARCH" -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod 755 /usr/local/lib/docker/cli-plugins/docker-compose
  fi
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose plugin не найден."
systemctl enable --now docker >/dev/null 2>&1 || true

if [[ -f "$INSTALL_DIR/.env" && -f "$INSTALL_DIR/docker-compose.yml" ]]; then
  DOMAIN="$(env_value DOMAIN)"
  AUTH_USER="$(env_value METRICS_USERNAME)"
  AUTH_PASS="$(env_value METRICS_PASSWORD)"
  if [[ -n "$DOMAIN" && -n "$AUTH_USER" && -n "$AUTH_PASS" ]]; then
    info "Найдена предыдущая установка. Проверяю конфигурацию и контейнеры..."
    cd "$INSTALL_DIR"
    # Remnawave must be requested in app-like JSON mode so balancer members
    # are returned as individual outbounds instead of a collapsed share link.
    cp -a "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.backup.$(date +%Y%m%d%H%M%S)"
    set_env "SUBSCRIPTION_JSON_FORMAT" "true"
    docker compose config -q || die "Конфигурация предыдущей установки некорректна. Проверьте $INSTALL_DIR/.env."
    docker compose pull
    docker compose up -d --remove-orphans
    info "Проверяю Checker..."
    if ! wait_for_checker; then
      docker compose logs --tail=80 checker || true
      die "Checker не прошёл healthcheck. Реквизиты не выведены, поскольку установка не прошла проверку."
    fi
    ok "Checker работает."
    info "Проверяю HTTPS и доверие сертификату для $DOMAIN..."
    if wait_for_https; then
      ok "HTTPS работает, сертификат доверенный."
      print_credentials
      exit 0
    fi
    warn "HTTPS не подтвердился. Последние логи Caddy:"
    docker compose logs --tail=120 caddy || true
    die "Предыдущая установка найдена, но HTTPS не прошёл проверку."
  fi
fi

mkdir -p "$INSTALL_DIR"
if [[ -f "$INSTALL_DIR/.env" ]]; then
  cp -a "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.backup.$(date +%Y%m%d%H%M%S)"
  AUTH_USER="$(sed -n "s/^METRICS_USERNAME=//p" "$INSTALL_DIR/.env" | tail -n1)"
  AUTH_PASS="$(sed -n "s/^METRICS_PASSWORD=//p" "$INSTALL_DIR/.env" | tail -n1)"
  OLD_DOMAIN="$(sed -n "s/^DOMAIN=//p" "$INSTALL_DIR/.env" | tail -n1 | tr -d "'")"
  OLD_SUB="$(sed -n "s/^SUBSCRIPTION_URL=//p" "$INSTALL_DIR/.env" | tail -n1 | sed "s/^'//;s/'$//")"
  AUTH_CREATED=0
else
  AUTH_USER="admin"
  AUTH_PASS="$(openssl rand -hex 18)"
  OLD_DOMAIN=""
  OLD_SUB=""
  AUTH_CREATED=1
fi

printf '\n'
printf '  ____        _             ____ _oud\n'
printf ' | __ )  ___ | |__  _ __   / ___| | ___  _   _  __| |\n'
printf ' |  _ \\ / _ \\|  _ \\|  _ \\ | |   | |/ _ \\| | | |/ _` |\n'
printf ' | |_) | (_) | |_) | |_) || |___| | (_) | |_| | (_| |\n'
printf ' |____/ \\___/|_.__/| .__/  \\____|_|\\___/ \\__,_|\\__,_|\n'
printf '                    |_|       ONE-CLICK INSTALLER\n\n'

read -r -p "Домен панели ${OLD_DOMAIN:+[$OLD_DOMAIN]}: " DOMAIN
DOMAIN="${DOMAIN:-$OLD_DOMAIN}"
[[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$DOMAIN" == *.* ]] || die "Укажите корректный домен, например monitor.example.com."

read -r -p "Ссылка подписки ${OLD_SUB:+[оставить текущую]}: " SUB_URL
SUB_URL="${SUB_URL:-$OLD_SUB}"
[[ "$SUB_URL" =~ ^https?://[^[:space:]]+$ ]] || die "Нужна полная ссылка подписки, начинающаяся с http:// или https://."
[[ "$SUB_URL" != *"'"* ]] || die "Ссылка содержит недопустимый символ одинарной кавычки."

if [[ -f "$INSTALL_DIR/.env" ]]; then
  read -r -p "Обновить/перезапустить существующую установку? [Y/n]: " CONFIRM
  [[ "$CONFIRM" =~ ^[NnНн]$ ]] && exit 0
else
  read -r -p "Продолжить установку? [Y/n]: " CONFIRM
  [[ "$CONFIRM" =~ ^[NnНн]$ ]] && exit 0
fi

# Determine the public IPv4 and verify DNS before starting Caddy/Let's Encrypt.
PUBLIC_IP="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
[[ -n "$PUBLIC_IP" ]] || die "Не удалось определить публичный IPv4 сервера. Проверьте исходящий доступ."
info "Проверяю DNS для $DOMAIN..."
DNS_IP=""
for attempt in $(seq 1 6); do
  DNS_IP="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
  [[ -n "$DNS_IP" ]] && break
  sleep 2
done
[[ -n "$DNS_IP" ]] || die "Домен $DOMAIN пока не разрешается в IPv4. Создайте DNS A-запись на $PUBLIC_IP и повторите запуск."
[[ "$DNS_IP" == "$PUBLIC_IP" ]] || die "Домен $DOMAIN указывает на $DNS_IP, а IP этого сервера — $PUBLIC_IP. Исправьте DNS A-запись и повторите."

# Detect a conflicting web server before publishing the reverse proxy.
if command -v ss >/dev/null 2>&1; then
  BUSY_PORTS="$(ss -H -ltn '( sport = :80 or sport = :443 )' 2>/dev/null || true)"
  [[ -z "$BUSY_PORTS" ]] || die "Порты 80/443 уже заняты. Остановите другой веб-сервер/reverse proxy и повторите.\n$BUSY_PORTS"
fi

info "Проверяю доступность ссылки подписки..."
SUB_HTTP="$(curl -L -sS -o /dev/null -w '%{http_code}' --connect-timeout 8 --max-time 20 "$SUB_URL" 2>/dev/null || true)"
[[ -n "$SUB_HTTP" && "$SUB_HTTP" != "000" ]] || die "Сервер не смог подключиться к URL подписки. Проверьте ссылку и доступность источника."
if [[ "$SUB_HTTP" =~ ^[23] ]]; then
  ok "Ссылка подписки отвечает (HTTP $SUB_HTTP)."
else
  warn "Источник подписки вернул HTTP $SUB_HTTP. Установка продолжится, но проверьте ссылку/доступ."
fi

# Open host firewall ports only when UFW is already enabled; never enable it blindly.
if command -v ufw >/dev/null 2>&1; then
  UFW_STATUS="$(ufw status 2>/dev/null | head -n1 || true)"
  if [[ "$UFW_STATUS" == "Status: active" ]]; then
    info "Открываю порты в UFW..."
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw allow 443/udp
    ok "Правила UFW добавлены: TCP 80/443 и UDP 443."
  else
    warn "UFW не активен — ничего не меняю в firewall, чтобы не потерять SSH."
  fi
else
  warn "UFW не установлен. Локальный firewall не настраивался."
fi

mkdir -p "$INSTALL_DIR/geo" "$INSTALL_DIR/custom" "$INSTALL_DIR/caddy_data" "$INSTALL_DIR/caddy_config"
cp "$SRC_DIR/docker-compose.yml" "$INSTALL_DIR/docker-compose.yml"
cp "$SRC_DIR/README.md" "$INSTALL_DIR/README.md"
cp "$SRC_DIR/custom/"* "$INSTALL_DIR/custom/"
chown -R 1000:1000 "$INSTALL_DIR/geo" "$INSTALL_DIR/custom"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  cat > "$INSTALL_DIR/.env" <<EOF
SUBSCRIPTION_UPDATE=true
SUBSCRIPTION_UPDATE_INTERVAL=300
SUBSCRIPTION_JSON_FORMAT=true
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
EOF
fi
set_env "SUBSCRIPTION_URL" "$SUB_URL"
set_env "DOMAIN" "$DOMAIN"
chmod 600 "$INSTALL_DIR/.env"

cat > "$INSTALL_DIR/Caddyfile" <<'EOF'
{$DOMAIN} {
    encode zstd gzip
    reverse_proxy checker:2112
}
EOF

install -m 755 "$SRC_DIR/cli.sh" /usr/local/bin/bobr-checker

info "Загружаю образы и запускаю Bobr Cloud Checker..."
cd "$INSTALL_DIR"
docker compose pull
docker compose up -d --remove-orphans
ok "Контейнеры запущены."

info "Жду готовности Checker..."
if wait_for_checker; then
  ok "Checker отвечает."
else
  docker compose logs --tail=80 checker || true
  die "Checker не прошёл healthcheck. Проверьте логи выше."
fi

info "Жду выпуска и проверки HTTPS-сертификата для $DOMAIN (до 5 минут)..."
HTTPS_READY=0
if wait_for_https; then HTTPS_READY=1; fi
printf '\n'
if [[ "$HTTPS_READY" == 1 ]]; then
  ok "HTTPS работает, сертификат проходит проверку доверия."
else
  warn "HTTPS не подтвердился. Последние логи Caddy:"
  docker compose logs --tail=120 caddy || true
fi
print_credentials
if [[ "$HTTPS_READY" != 1 ]]; then
  printf '\nПроверьте внешнюю доступность TCP 80 и TCP 443, DNS A/AAAA и логи Caddy.\n'
  exit 1
fi
