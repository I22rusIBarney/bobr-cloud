#!/usr/bin/env bash
set -Eeuo pipefail
DIR=/opt/bobr-checker
cd "$DIR"
case "\${1:-help}" in
  status) docker compose ps ;;
  logs) docker compose logs -f --tail=200 checker ;;
  restart) docker compose restart checker ;;
  update) docker compose pull checker && docker compose up -d --remove-orphans checker ;;
  config) "\${EDITOR:-nano}" "$DIR/.env" ;;
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
