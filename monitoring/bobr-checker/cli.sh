#!/usr/bin/env bash
set -Eeuo pipefail
DIR=/opt/bobr-checker
cd "$DIR"
case "${1:-help}" in
  status) docker compose ps ;;
  logs) docker compose logs -f --tail=200 ;;
  restart) docker compose restart ;;
  update)
    [[ -f "$DIR/.env" ]] || { echo "Не найден $DIR/.env"; exit 1; }
    cp -a "$DIR/.env" "$DIR/.env.backup.$(date +%Y%m%d%H%M%S)"
    tmp="$(mktemp)"
    if grep -q '^SUBSCRIPTION_JSON_FORMAT=' "$DIR/.env"; then
      sed 's/^SUBSCRIPTION_JSON_FORMAT=.*/SUBSCRIPTION_JSON_FORMAT=true/' "$DIR/.env" > "$tmp"
    else
      cat "$DIR/.env" > "$tmp"
      printf '\nSUBSCRIPTION_JSON_FORMAT=true\n' >> "$tmp"
    fi
    install -m 600 "$tmp" "$DIR/.env"
    rm -f "$tmp"
    docker compose config -q
    docker compose pull
    docker compose up -d --remove-orphans
    echo "Ожидаю готовности Checker..."
    ready=0
    for _ in $(seq 1 45); do
      if docker compose exec -T checker curl -fsS http://127.0.0.1:2112/health >/dev/null 2>&1; then ready=1; break; fi
      sleep 2
    done
    if [[ "$ready" != 1 ]]; then
      docker compose logs --tail=100 checker
      echo "Checker не прошёл healthcheck." >&2
      exit 1
    fi
    echo "Готово: JSON-режим включён, Checker обновлён и работает."
    echo "Проверяйте логи подписки: bobr-checker logs"
    ;;
  config) "${EDITOR:-nano}" "$DIR/.env" ;;
  uninstall)
    echo "Контейнер и файлы приложения будут удалены."
    read -r -p "Для подтверждения введите DELETE: " answer
    [[ "$answer" == DELETE ]] || exit 0
    docker compose down
    cd /
    rm -rf "$DIR"
    rm -f /usr/local/bin/bobr-checker
    echo "Bobr Cloud Checker удалён."
    ;;
  help|--help|-h)
    echo "Bobr Cloud Checker: status | logs | restart | update | config | uninstall"
    ;;
  *) echo "Неизвестная команда. Используйте: bobr-checker help"; exit 2 ;;
esac
