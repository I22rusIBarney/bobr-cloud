#!/usr/bin/env bash
# Установщик VPS-бота. Запуск на чистом VPS (Ubuntu/Debian) от root:
#   sudo ./install.sh            — установка / повторная настройка
#   sudo ./install.sh update     — подтянуть обновления (git pull), пересобрать и перезапустить
#   sudo ./install.sh logs       — смотреть логи
#   sudo ./install.sh status     — состояние контейнеров
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

C_B=$'\e[1m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_0=$'\e[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '%s✖ %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo ./install.sh"

compose() { docker compose "$@"; }

case "${1:-install}" in
  update)
    if [ -d .git ] && command -v git >/dev/null; then
      # не зависим от настроек отслеживания ветки: явно берём origin/main
      git fetch origin main && git merge --ff-only origin/main \
        || die "Не удалось обновить из git — проверьте локальные изменения (git status)"
    fi
    compose up -d --build; ok "Обновлено"; exit 0 ;;
  logs)   compose logs -f --tail=100 bot; exit 0 ;;
  status) compose ps; exit 0 ;;
  install) ;;
  *) die "Неизвестная команда: $1" ;;
esac

# ───────── значения по умолчанию из прошлой установки ─────────
declare -A V
if [[ -f .env ]]; then
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" == \#* ]] && continue
    v="${v#\'}"; v="${v%\'}"; V["$k"]="$v"
  done < .env
  warn "Найден .env — Enter оставит текущее значение."
fi

# ввод: ask ИМЯ "Вопрос" [secret]
ask() {
  local name="$1" q="$2" secret="${3:-}" cur="${V[$1]:-}" shown="" ans=""
  if [[ -n "$cur" ]]; then
    if [[ -n "$secret" ]]; then shown=" [оставить текущее]"; else shown=" [$cur]"; fi
  fi
  while true; do
    if [[ -n "$secret" ]]; then
      read -rs -p "$q$shown: " ans </dev/tty || true; echo
    else
      read -r -p "$q$shown: " ans </dev/tty || true
    fi
    ans="${ans:-$cur}"
    [[ "$ans" == *"'"* ]] && { warn "Символ ' не поддерживается, повторите."; continue; }
    V["$name"]="$ans"; return
  done
}
yesno() { local a; read -r -p "$1 [y/N]: " a </dev/tty || true; [[ "$a" =~ ^[YyДд] ]]; }

say "${C_B}══ Установка VPS-бота ══${C_0}"

# ───────── Docker ─────────
if ! command -v docker >/dev/null 2>&1; then
  say "Устанавливаю Docker…"
  command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker compose version >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq docker-compose-plugin; }
systemctl enable --now docker >/dev/null 2>&1 || true
ok "Docker готов"

command -v curl >/dev/null 2>&1 || apt-get install -y -qq curl >/dev/null
command -v openssl >/dev/null 2>&1 || apt-get install -y -qq openssl >/dev/null

# ───────── Telegram ─────────
say; say "${C_B}1/3 Telegram${C_0}  (токен бота — у @BotFather, ваш ID — у @userinfobot)"
while true; do
  ask BOT_TOKEN "Токен бота" secret
  r=$(curl -fsS "https://api.telegram.org/bot${V[BOT_TOKEN]}/getMe" 2>/dev/null || true)
  if [[ "$r" == *'"ok":true'* ]]; then ok "Бот: @$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$r")"; break; fi
  warn "Токен не подошёл, попробуйте ещё раз."; V[BOT_TOKEN]=""
done
while true; do
  ask ADMIN_IDS "Telegram ID администратора (несколько — через запятую)"
  [[ "${V[ADMIN_IDS]}" =~ ^[0-9]+(,[0-9]+)*$ ]] && break
  warn "Нужны только цифры, через запятую."; V[ADMIN_IDS]=""
done
ask SUPPORT_USERNAME "Username поддержки без @ (можно пропустить)"

# ───────── Домен ─────────
say; say "${C_B}2/3 Домен${C_0}  (A-запись домена должна указывать на IP этого сервера; нужен для HTTPS)"
SERVER_IP=$(curl -fsS -4 https://api.ipify.org 2>/dev/null || curl -fsS -4 https://ifconfig.me 2>/dev/null || echo "")
[[ -n "$SERVER_IP" ]] && say "IP этого сервера: $SERVER_IP"
while true; do
  ask DOMAIN "Домен (например bot.example.com)"
  d="${V[DOMAIN]#https://}"; d="${d%%/*}"; V[DOMAIN]="$d"
  [[ "$d" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || { warn "Похоже на неверный домен."; V[DOMAIN]=""; continue; }
  RES=$(getent ahostsv4 "$d" 2>/dev/null | awk '{print $1; exit}' || true)
  if [[ -n "$SERVER_IP" && "$RES" == "$SERVER_IP" ]]; then ok "DNS указывает на этот сервер"; break; fi
  warn "Домен сейчас резолвится в «${RES:-ничего}», а не в $SERVER_IP. Без этого HTTPS не получится."
  yesno "Продолжить всё равно?" && break
  V[DOMAIN]=""
done

# ───────── Astra ─────────
say; say "${C_B}3/3 Astra VPS${C_0}  (API-ключ: t.me/astravpsbot?start=reseller)"
while true; do
  ask ASTRA_API_KEY "API-ключ реселлера" secret
  r=$(curl -fsS -H "Authorization: Bearer ${V[ASTRA_API_KEY]}" "https://astravps.co/api/reseller/v1/balance" 2>/dev/null || true)
  if [[ "$r" == *balance_rub* ]]; then
    ok "Ключ верный. Баланс Astra: $(sed -n 's/.*"balance_rub": *\([0-9.]*\).*/\1/p' <<<"$r") ₽, скидка: $(sed -n 's/.*"discount_pct": *\([0-9.]*\).*/\1/p' <<<"$r")%"; break
  fi
  warn "Ключ не подошёл."; V[ASTRA_API_KEY]=""
done

# ───────── Файлы ─────────
[[ -n "${V[WEBHOOK_SECRET]:-}" ]] || V[WEBHOOK_SECRET]=$(openssl rand -hex 16)
umask 077
cat > .env <<EOF
BOT_TOKEN='${V[BOT_TOKEN]}'
ADMIN_IDS='${V[ADMIN_IDS]}'
SUPPORT_USERNAME='${V[SUPPORT_USERNAME]:-}'
PUBLIC_URL='https://${V[DOMAIN]}'
WEBHOOK_SECRET='${V[WEBHOOK_SECRET]}'
ASTRA_API_KEY='${V[ASTRA_API_KEY]}'
ASTRA_BASE_URL='https://astravps.co/api/reseller/v1'
YOOKASSA_SHOP_ID='${V[YOOKASSA_SHOP_ID]:-}'
YOOKASSA_SECRET_KEY='${V[YOOKASSA_SECRET_KEY]:-}'
NALOG_INN='${V[NALOG_INN]:-}'
NALOG_PASSWORD='${V[NALOG_PASSWORD]:-}'
EOF
chmod 600 .env
printf '%s {\n\treverse_proxy bot:8080\n}\n' "${V[DOMAIN]}" > Caddyfile
mkdir -p data
ok "Конфигурация записана (.env, Caddyfile)"

# фаервол
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ok "ufw: открыты 80 и 443"
fi
if ss -ltn 2>/dev/null | grep -E ':(80|443)\s' | grep -qv docker; then
  if ! docker ps --format '{{.Ports}}' | grep -qE ':(80|443)->'; then
    warn "Порты 80/443 заняты другим сервисом (nginx/apache?). Освободите их, иначе HTTPS не поднимется."
  fi
fi

# ───────── Запуск ─────────
say; say "Собираю и запускаю (первый раз 1–3 минуты)…"
compose up -d --build

say "Жду, пока сервис ответит по HTTPS…"
up=""
for _ in $(seq 1 45); do
  if curl -fsS -m 5 "https://${V[DOMAIN]}/paid" >/dev/null 2>&1; then up=1; break; fi
  sleep 4
done

say
if [[ -n "$up" ]]; then ok "Сервис работает: https://${V[DOMAIN]}"; else
  warn "HTTPS ещё не отвечает. Частые причины: DNS не обновился, закрыты порты 80/443."
  warn "Логи: sudo ./install.sh logs"
fi

cat <<EOF

${C_B}Готово. Дальше всё настраивается в Telegram:${C_0}
 1. Откройте бота, отправьте /start и нажмите «⚙️ Админка».
 2. Раздел «Интеграции»:
      • ЮKassa: shopId и секретный ключ, «Сохранить и проверить».
        Там же показан URL уведомлений для кабинета ЮKassa (события payment.succeeded и payment.canceled).
      • «Мой налог»: ИНН и пароль (по желанию).
 3. Там же наценка и остальные параметры.
Клиентский кабинет — кнопка «Кабинет» в боте.

Команды: ./install.sh logs | status | update
EOF
