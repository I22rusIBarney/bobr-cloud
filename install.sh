#!/usr/bin/env bash
# Установщик Bobr Cloud. Запуск на чистом VPS (Ubuntu/Debian) от root: sudo ./install.sh
# После установки доступна короткая команда bcloud (bcloud help — список команд).
set -euo pipefail
CALLER_PWD="${BCLOUD_CWD:-$PWD}"  # откуда запустили — для относительных путей (bcloud restore файл)
cd "$(dirname "$(readlink -f "$0")")"

C_B=$'\e[1m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_0=$'\e[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '%s✖ %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root: sudo ./install.sh"

compose() { docker compose "$@"; }

# Команда bcloud в /usr/local/bin: работает из любой папки и сама поднимает права через sudo
install_cli() {
  local dir; dir="$(pwd)"
  cat > /usr/local/bin/bcloud <<EOF
#!/usr/bin/env bash
# Bobr Cloud — управление ботом. Создано установщиком из ${dir}
if [[ \$EUID -ne 0 ]]; then exec sudo "\$0" "\$@"; fi
export BCLOUD_CWD="\$PWD"
cd "${dir}" || { echo "Папка бота ${dir} не найдена — запустите install.sh из новой папки"; exit 1; }
exec ./install.sh "\${@:-help}"
EOF
  chmod 755 /usr/local/bin/bcloud
}

# ───────── Caddy: основной домен и (по желанию) свой домен консоли VNC ─────────
env_get() { [[ -f .env ]] && sed -n "s/^$1='\{0,1\}\([^']*\)'\{0,1\}$/\1/p" .env | tail -1; }
env_set() {
  if grep -q "^$1=" .env 2>/dev/null; then sed -i "s|^$1=.*|$1='$2'|" .env; else printf "%s='%s'\n" "$1" "$2" >> .env; fi
}
write_caddyfile() {  # $1 — домен бота, $2 — домен консоли (может быть пустым)
  printf '%s {\n\treverse_proxy bot:8080\n}\n' "$1" > Caddyfile
  if [[ -n "${2:-}" ]]; then
    cat >> Caddyfile <<CADDY

# консоль VNC под своим доменом: прозрачный прокси на Astra (страница и WebSocket)
$2 {
	reverse_proxy https://vnc.astravps.co {
		header_up Host vnc.astravps.co
		header_up Origin https://vnc.astravps.co
		header_up Referer https://vnc.astravps.co/
		transport http {
			tls_server_name vnc.astravps.co
		}
	}
}
CADDY
  fi
}

vnc_domain_cmd() {
  [[ -f .env ]] || die "Сначала установите бота: sudo ./install.sh"
  local d="${1:-}" main cur ip res a
  main="$(env_get PUBLIC_URL)"; main="${main#https://}"; main="${main%%/*}"
  cur="$(env_get VNC_DOMAIN)"
  if [[ -z "$d" ]]; then
    say "Консоль VNC сейчас: ${cur:-адрес Astra (свой домен не настроен)}"
    say "Включить:  bcloud vnc vnc.${main#*.}   (A-запись поддомена — на IP этого сервера)"
    say "Выключить: bcloud vnc off"
    return 0
  fi
  if [[ "$d" == off ]]; then
    d=""
  else
    d="${d#https://}"; d="${d%%/*}"; d="${d,,}"
    [[ "$d" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "Неверный домен: $d"
    [[ "$d" != "$main" ]] || die "Нужен отдельный поддомен, не $main — например vnc.${main#*.}"
    ip=$(curl -fsS -4 -m 5 https://api.ipify.org 2>/dev/null || true)
    res=$(getent ahostsv4 "$d" 2>/dev/null | awk '{print $1; exit}' || true)
    if [[ -n "$ip" && "$res" != "$ip" ]]; then
      warn "$d сейчас указывает на «${res:-ничего}», а не на этот сервер ($ip)."
      warn "Добавьте A-запись $d → $ip, иначе сертификат не выпустится."
      read -r -p "Продолжить всё равно? [y/N] " a; [[ "$a" =~ ^[yYдД] ]] || { say "Отменено"; return 0; }
    fi
  fi
  env_set VNC_DOMAIN "$d"
  write_caddyfile "$main" "$d"
  compose up -d --force-recreate caddy >/dev/null 2>&1 || compose restart caddy
  compose up -d --force-recreate bot >/dev/null 2>&1 || compose restart bot
  if [[ -n "$d" ]]; then
    ok "Консоль VNC теперь открывается по адресу https://$d/…"
    say "Сертификат выпустится за минуту. Проверьте: откройте консоль в кабинете."
  else
    ok "Свой домен консоли выключен — ссылки снова ведут на Astra"
  fi
}

# ───────── защита сервера ─────────
ssh_port() { sshd -T 2>/dev/null | awk '/^port /{print $2; exit}'; }

harden() {
  command -v apt-get >/dev/null || die "Поддерживаются Ubuntu и Debian"
  local port dir keys pw_off="no"
  port="$(ssh_port)"; port="${port:-22}"
  dir="$(pwd)"
  keys=0; [[ -s /root/.ssh/authorized_keys ]] && keys=$(grep -cE '^(ssh-|ecdsa-|sk-)' /root/.ssh/authorized_keys || true)
  cat <<EOF
${C_B}Защита сервера${C_0}
  • Файрвол UFW: входящие закрыты, кроме SSH (порт ${port}), 80 и 443
  • fail2ban: блокировка IP после 5 неудачных входов по SSH (на час, повторно — дольше)
  • SSH: не больше 3 попыток за подключение, без пустых паролей и X11
  • Автоматические обновления безопасности Ubuntu
  • Защита сетевого стека (sysctl) и уведомления о входах по SSH в бота
Текущую SSH-сессию не закрывайте, пока не проверите вход в новом окне.
EOF
  read -r -p "Продолжить? [y/N] " a; [[ "$a" =~ ^[yYдД] ]] || { say "Отменено"; return; }
  if (( keys > 0 )); then
    say; say "Найдено SSH-ключей у root: ${keys}. Можно запретить вход по паролю — это главная защита от взлома."
    say "Перед этим убедитесь, что входите именно по ключу, иначе потеряете доступ."
    read -r -p "Запретить вход по паролю? [y/N] " a; [[ "$a" =~ ^[yYдД] ]] && pw_off="yes"
  else
    warn "SSH-ключей у root нет — вход по паролю оставляю. Настройте ключ и запустите bcloud harden ещё раз."
  fi

  say "Устанавливаю пакеты…"
  apt-get update -qq >/dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw fail2ban unattended-upgrades >/dev/null || die "Не удалось установить пакеты"

  # 1. файрвол: существующие правила не сбрасываем, только добавляем нужные
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow "${port}/tcp" comment 'SSH' >/dev/null
  # страховка: открываем все порты, на которых реально слушает sshd, чтобы не потерять доступ
  local ports="$port"
  for p in $(ss -tlnpH 2>/dev/null | awk '/sshd/{n=split($4,a,":"); print a[n]}' | sort -u); do
    if [[ "$p" =~ ^[0-9]+$ && "$p" != "$port" ]]; then ufw allow "${p}/tcp" comment 'SSH' >/dev/null; ports="${ports},${p}"; fi
  done
  ufw allow 80/tcp comment 'HTTP (Caddy)' >/dev/null
  ufw allow 443/tcp comment 'HTTPS (Caddy)' >/dev/null
  ufw --force enable >/dev/null
  ok "Файрвол включён: открыты ${ports} (SSH), 80, 443"

  # 2. хук событий: входы по SSH и блокировки → data/host-events.jsonl → бот
  cat > /usr/local/sbin/bcloud-event <<EOF
#!/bin/sh
# Bobr Cloud: события сервера для бота (создано bcloud harden)
F="${dir}/data/host-events.jsonl"
clean() { printf '%s' "\$1" | tr -cd 'A-Za-z0-9._:@-' | cut -c1-64; }
case "\$1" in
  login) [ "\$PAM_TYPE" = "open_session" ] || exit 0
         printf '{"type":"login","user":"%s","ip":"%s","ts":%s}\n' "\$(clean "\$PAM_USER")" "\$(clean "\$PAM_RHOST")" "\$(date +%s)" >> "\$F" ;;
  ban)   printf '{"type":"ban","ip":"%s","jail":"%s","ts":%s}\n' "\$(clean "\$2")" "\$(clean "\$3")" "\$(date +%s)" >> "\$F" ;;
esac
exit 0
EOF
  chmod 755 /usr/local/sbin/bcloud-event
  grep -qs bcloud-event /etc/pam.d/sshd || echo "session optional pam_exec.so quiet /usr/local/sbin/bcloud-event login" >> /etc/pam.d/sshd

  # 3. fail2ban
  mkdir -p /etc/fail2ban/action.d /etc/fail2ban/jail.d
  cat > /etc/fail2ban/action.d/bcloud-notify.conf <<'EOF'
[Definition]
actionban = /usr/local/sbin/bcloud-event ban <ip> <name>
actionunban =
EOF
  cat > /etc/fail2ban/jail.d/bcloud.conf <<EOF
# Bobr Cloud (bcloud harden)
[DEFAULT]
bantime = 1h
bantime.increment = true
bantime.maxtime = 1w
findtime = 10m
maxretry = 5
ignoreip = 127.0.0.1/8 ::1

[sshd]
enabled = true
port = ${ports}
backend = systemd
action = %(action_)s
         bcloud-notify
EOF
  systemctl enable fail2ban >/dev/null 2>&1 || true
  systemctl restart fail2ban || die "fail2ban не запустился: journalctl -u fail2ban"
  ok "fail2ban следит за SSH"

  # 4. SSH: отдельный файл, первые значения в sshd_config.d имеют приоритет
  mkdir -p /etc/ssh/sshd_config.d
  {
    echo "# Bobr Cloud (bcloud harden). Удалите файл, чтобы вернуть прежние настройки."
    echo "MaxAuthTries 3"
    echo "LoginGraceTime 30"
    echo "PermitEmptyPasswords no"
    echo "X11Forwarding no"
    echo "ClientAliveInterval 300"
    echo "ClientAliveCountMax 2"
    if [[ "$pw_off" == "yes" ]]; then
      echo "PasswordAuthentication no"
      echo "KbdInteractiveAuthentication no"
      echo "PermitRootLogin prohibit-password"
    fi
  } > /etc/ssh/sshd_config.d/00-bcloud.conf
  if sshd -t 2>/dev/null; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    ok "SSH усилен" ; [[ "$pw_off" == "yes" ]] && ok "Вход по паролю отключён — только по ключу"
  else
    rm -f /etc/ssh/sshd_config.d/00-bcloud.conf
    warn "Проверка конфигурации SSH не прошла — SSH оставлен как был"
  fi

  # 5. автообновления безопасности и сетевой стек
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
  cat > /etc/sysctl.d/90-bcloud.conf <<'EOF'
# Bobr Cloud (bcloud harden)
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.icmp_echo_ignore_broadcasts = 1
EOF
  sysctl --system >/dev/null 2>&1 || true
  ok "Автообновления безопасности включены"

  mkdir -p data
  printf '{"ts":%s,"ssh_port":%s,"password_login":%s}\n' "$(date +%s)" "$port" "$([[ "$pw_off" == yes ]] && echo false || echo true)" > data/.hardened
  say
  ok "Готово. О входах по SSH и атаках бот сообщит в событие «Безопасность»."
  warn "Не закрывайте эту сессию: откройте новое окно и проверьте, что входите на сервер."
}

security_status() {
  say "${C_B}Файрвол${C_0}"; ufw status 2>/dev/null | sed -n '1,12p' || warn "UFW не установлен — bcloud harden"
  say; say "${C_B}fail2ban (SSH)${C_0}"
  if command -v fail2ban-client >/dev/null; then fail2ban-client status sshd 2>/dev/null | grep -E 'Currently|Total' || warn "fail2ban не запущен"; else warn "fail2ban не установлен — bcloud harden"; fi
  say; say "${C_B}SSH${C_0}"
  sshd -T 2>/dev/null | grep -E '^(port|passwordauthentication|permitrootlogin|maxauthtries) ' || true
  [[ "$(sshd -T 2>/dev/null | awk '/^passwordauthentication /{print $2}')" == "yes" ]] && warn "Вход по паролю разрешён: настройте SSH-ключ и запустите bcloud harden"
  say; say "${C_B}Обновления безопасности${C_0}"
  grep -q 'Unattended-Upgrade "1"' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null && ok "Автообновления включены" || warn "Автообновления выключены"
}

usage() {
  cat <<EOF
${C_B}bcloud${C_0} — управление Bobr Cloud

  bcloud update    обновить из GitHub, пересобрать и перезапустить
  bcloud restart   перезапустить бота
  bcloud logs      логи бота (выход — Ctrl+C)
  bcloud status    состояние сервисов и версия
  bcloud backup    резервная копия базы и настроек в /root/bcloud-backups
  bcloud restore Ф восстановить базу из копии (файл из Telegram *.db.gz или bot.db)
  bcloud harden    защитить сервер: файрвол, fail2ban, SSH, автообновления
  bcloud security  состояние защиты: файрвол, блокировки, вход по паролю
  bcloud vnc ДОМЕН консоль VNC под своим поддоменом (bcloud vnc off — выключить)
  bcloud setup     повторная настройка (токен, домен, ключи)
  bcloud help      эта справка
EOF
}

case "${1:-install}" in
  update)
    if [ -d .git ] && command -v git >/dev/null; then
      before="$(git rev-parse --short HEAD 2>/dev/null || true)"
      # не зависим от настроек отслеживания ветки: явно берём origin/main
      git fetch -q origin main && git merge -q --ff-only origin/main \
        || die "Не удалось обновить из git — проверьте локальные изменения (git status)"
      after="$(git rev-parse --short HEAD)"
      if [[ "$before" == "$after" ]]; then ok "Уже последняя версия ($after)"; else ok "Код обновлён: $before → $after"; fi
      # дальше — уже свежей версией скрипта
      exec ./install.sh _rebuild
    fi
    compose up -d --build; install_cli; ok "Обновлено"; exit 0 ;;
  _rebuild) compose up -d --build; install_cli; ok "Обновлено и перезапущено"; exit 0 ;;
  restart) compose restart bot; ok "Бот перезапущен"; exit 0 ;;
  logs)    compose logs -f --tail=100 bot; exit 0 ;;
  status)
    compose ps
    [ -d .git ] && say "Версия: $(git log -1 --format='%h от %cd' --date=format:'%d.%m.%Y %H:%M' 2>/dev/null)"
    exit 0 ;;
  backup)
    mkdir -p /root/bcloud-backups
    f="/root/bcloud-backups/bcloud-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar -czf "$f" data .env $( [ -f Caddyfile ] && echo Caddyfile ) 2>/dev/null || die "Не удалось создать копию"
    chmod 600 "$f"
    ls -1t /root/bcloud-backups/bcloud-*.tar.gz | tail -n +11 | xargs -r rm -f  # храним 10 последних
    ok "Копия: $f"; exit 0 ;;
  restore)
    f="${2:-}"
    [[ -n "$f" ]] || die "Укажите файл: bcloud restore /root/bcloud-20261026-1200.db.gz"
    [[ "$f" = /* ]] || f="$CALLER_PWD/$f"
    [[ -f "$f" ]] || die "Файл не найден: $f"
    tmp="$(mktemp)"
    if [[ "$f" == *.gz ]]; then gzip -dc "$f" > "$tmp" || die "Файл повреждён"; else cp "$f" "$tmp"; fi
    head -c 15 "$tmp" | grep -q "SQLite format 3" || { rm -f "$tmp"; die "Это не файл базы SQLite"; }
    read -r -p "Текущая база будет заменена (её копия сохранится). Продолжить? [y/N] " a
    [[ "$a" =~ ^[yYдД] ]] || { rm -f "$tmp"; say "Отменено"; exit 0; }
    compose stop bot
    mkdir -p data
    [[ -f data/bot.db ]] && cp data/bot.db "data/bot.db.before-restore-$(date +%Y%m%d-%H%M%S)"
    rm -f data/bot.db-wal data/bot.db-shm
    mv "$tmp" data/bot.db
    compose start bot
    ok "База восстановлена. Если копия из Telegram — введите заново ключи ЮKassa и «Мой налог» в админке."
    exit 0 ;;
  harden)   harden; exit 0 ;;
  security) security_status; exit 0 ;;
  vnc)      vnc_domain_cmd "${2:-}"; exit 0 ;;
  help|-h|--help) usage; exit 0 ;;
  setup|install) ;;
  *) usage; die "Неизвестная команда: $1" ;;
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
VNC_DOMAIN='${V[VNC_DOMAIN]:-}'
EOF
chmod 600 .env
write_caddyfile "${V[DOMAIN]}" "${V[VNC_DOMAIN]:-}"
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
install_cli

say "Жду, пока сервис ответит по HTTPS…"
up=""
for _ in $(seq 1 45); do
  if curl -fsS -m 5 "https://${V[DOMAIN]}/paid" >/dev/null 2>&1; then up=1; break; fi
  sleep 4
done

say
if [[ -n "$up" ]]; then ok "Сервис работает: https://${V[DOMAIN]}"; else
  warn "HTTPS ещё не отвечает. Частые причины: DNS не обновился, закрыты порты 80/443."
  warn "Логи: bcloud logs"
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

Управление: bcloud update · restart · logs · status · backup · setup (bcloud help — подробнее)
EOF
