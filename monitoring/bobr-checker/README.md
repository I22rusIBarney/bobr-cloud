# Bobr Cloud Checker

Центральный мониторинг VPN-локаций BobrGO на отдельном VPS. Развёртывание через Docker Compose, публичный доступ — через Caddy с автоматическим HTTPS (Let's Encrypt).

## Требования

- Ubuntu 22.04+ или Debian 12+ (x86_64/ARM64)
- Домен или поддомен с DNS A-записью на публичный IPv4 сервера
- Открытые входящие TCP-порты 80 и 443; UDP 443 рекомендуется для HTTP/3
- URL подписки, доступный с сервера мониторинга

## Установка

```bash
git clone -b feature/bobr-cloud-checker https://github.com/I22rusIBarney/bobr-cloud.git
cd bobr-cloud
sudo bash monitoring/bobr-checker/install.sh
```

Установщик спросит URL подписки, домен и логин. Пароль сгенерирует автоматически. Установит Docker при необходимости, запустит Checker и Caddy, выпустит HTTPS-сертификат и настроит автопродление.

После установки панель будет доступна по адресу `https://ВАШ-ДОМЕН`.

**До запуска:** направьте DNS A-запись домена на IP сервера. Если используете AAAA-запись, она тоже должна вести на этот сервер и IPv6 должен быть доступен на портах 80/443. При DNS proxy (например, Cloudflare) для первичного запуска лучше проверить режим DNS-only, а затем включать proxy при необходимости.

## Управление

```bash
bobr-checker status
bobr-checker logs
bobr-checker restart
bobr-checker update
bobr-checker config
```

Логи Caddy: `docker logs -f bobr-cloud-caddy`.

Конфигурация хранится в `/opt/bobr-checker/.env` (права 600). Данные и сертификаты Caddy — в `/opt/bobr-checker/caddy_data`. При повторном запуске установщика настройки сохраняются, перед обновлением создаётся резервная копия .env.

## Настройка формата подписки

По умолчанию `SUBSCRIPTION_JSON_FORMAT=false`. Для Remnawave subscription, возвращающей JSON с balancers, установите `true` в `/opt/bobr-checker/.env`, затем выполните `bobr-checker restart`. Для Base64/share-ссылки оставьте `false`.

## Безопасность

- Checker не публикует порт 2112 на хосте; доступ к веб-интерфейсу идёт через Caddy.
- Caddy автоматически получает и продлевает TLS-сертификат.
- Не публикуйте файл `.env`: в нём URL подписки и пароль.
- Не отключайте авторизацию панели для публичного домена.
