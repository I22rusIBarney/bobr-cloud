from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    admin_ids: str = ""
    support_username: str = ""

    public_url: str = "http://localhost:8080"
    webhook_secret: str = "change-me"

    astra_api_key: str
    astra_base_url: str = "https://astravps.co/api/reseller/v1"

    # необязательно: ключи теперь вводятся в админке; значения из .env — запасной вариант
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""

    nalog_inn: str = ""
    nalog_password: str = ""

    # свой домен для консоли VNC (прокси на vnc.astravps.co через Caddy): bcloud vnc vnc.example.com
    vnc_domain: str = ""

    # служебный API /api/svc для своих ботов (бот развёртывания нод); пусто — выключен. bcloud api-token
    service_token: str = ""

    database_url: str = "sqlite+aiosqlite:///data/bot.db"
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    @property
    def admins(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x}

    @property
    def nalog_enabled(self) -> bool:
        return bool(self.nalog_inn and self.nalog_password)


@lru_cache
def env() -> Env:
    return Env()  # type: ignore[call-arg]
