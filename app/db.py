from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint, inspect, text, update
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import env


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """SQLite хранит даты без пояса: всегда пишем в UTC и читаем как aware-UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class User(Base):
    __tablename__ = "users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    balance_kop: Mapped[int] = mapped_column(Integer, default=0)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # версия принятых документов (0 — не принимал); сами факты согласия — в таблице consents
    consent_version: Mapped[int] = mapped_column(Integer, default=0)
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    autopay_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # скидка из промокода на следующую покупку сервера
    promo_discount_pct: Mapped[int] = mapped_column(Integer, default=0)
    promo_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # персональные цены без наценки (например, для друзей): себестоимость Astra, округление до рубля
    no_markup: Mapped[bool] = mapped_column(Boolean, default=False)
    # индивидуальная наценка в %, None — общая из настроек (no_markup важнее)
    markup_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # рассылки: отписался от новостей / заблокировал бота (не шлём, пока снова не напишет)
    news_off: Mapped[bool] = mapped_column(Boolean, default=False)
    # согласие на рекламу (38-ФЗ ст. 18): только с ним уходят рассылки «Новости и акции»
    ads_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    # реферальная программа: кто пригласил, когда, выдан ли приветственный бонус
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    referred_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    ref_welcome_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    # персональная скидка (брошенная оплата, возврат) действует до этой даты; None — бессрочно
    promo_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # когда последний раз отправляли предложение вернуться / промокод за брошенную оплату
    winback_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    abandon_promo_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Broadcast(Base):
    """Массовая рассылка: текст, кнопка, аудитория и прогресс (продолжается после перезапуска)."""
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text)
    btn_text: Mapped[str] = mapped_column(String(64), default="")
    btn_url: Mapped[str] = mapped_column(String(512), default="")
    audience: Mapped[str] = mapped_column(String(16), default="all")   # all | active | inactive
    kind: Mapped[str] = mapped_column(String(16), default="news")      # news (с отпиской) | service
    ask_ads: Mapped[bool] = mapped_column(Boolean, default=False)       # кнопка «Получать новости и акции» в важной рассылке
    status: Mapped[str] = mapped_column(String(16), default="sending")  # sending | done | cancelled
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[int] = mapped_column(BigInteger, default=0)  # последний обработанный tg_id
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    yk_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)  # id платежа у провайдера
    provider: Mapped[str] = mapped_column(String(16), default="yookassa")  # yookassa | platega
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount_kop: Mapped[int] = mapped_column(Integer)
    # pending | succeeded | canceled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # none | pending | done | failed
    receipt_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    receipt_attempts: Mapped[int] = mapped_column(Integer, default=0)
    receipt_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # автоплатёж: списание с сохранённого способа без участия клиента
    autopay: Mapped[bool] = mapped_column(Boolean, default=False)
    save_method: Mapped[bool] = mapped_column(Boolean, default=False)
    method_yk_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    method_title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # напоминания о незавершённой оплате: 0 — нет, 1 — напомнили, 2 — предложили скидку / закрыто
    remind_stage: Mapped[int] = mapped_column(Integer, default=0)


class Server(Base):
    __tablename__ = "servers"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    astra_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(60))
    location: Mapped[str] = mapped_column(String(8))
    cpu: Mapped[int] = mapped_column(Integer, default=0)
    ram: Mapped[int] = mapped_column(Integer, default=0)
    disk: Mapped[int] = mapped_column(Integer, default=0)
    os: Mapped[str] = mapped_column(String(32), default="")
    # что платит клиент в месяц / что стоит нам (копейки)
    price_kop: Mapped[int] = mapped_column(Integer)
    cost_kop: Mapped[int] = mapped_column(Integer, default=0)
    # provisioning | active | suspended | expired | deleted
    status: Mapped[str] = mapped_column(String(16), default="provisioning", index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    root_password: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    # 0 — не уведомляли, 1 — за 3 дня, 2 — за 1 день
    notified_stage: Mapped[int] = mapped_column(Integer, default=0)
    renew_try_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    # сколько клиент фактически заплатил за текущий период (со скидкой); 0 — равно price_kop
    period_paid_kop: Mapped[int] = mapped_column(Integer, default=0)
    # сколько месяцев покрывает period_paid_kop (1 — помесячно; 3/6/12 — покупка на срок; дробное после досрочного продления)
    period_months: Mapped[float] = mapped_column(Float, default=1.0)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # мониторинг доступности: следить ли, сколько проверок подряд неудачны, с какого момента не отвечает,
    # до какого времени не проверять (после перезагрузки/переустановки)
    monitor: Mapped[bool] = mapped_column(Boolean, default=True)
    mon_fail: Mapped[int] = mapped_column(Integer, default=0)
    mon_down_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    mon_quiet_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # себестоимость, о которой уже предупредили (сторож маржи не повторяет алерт)
    margin_alert_cost: Mapped[int] = mapped_column(Integer, default=0)


class LocationWatch(Base):
    """Клиент просил сообщить, когда закрытая локация снова появится в продаже."""
    __tablename__ = "location_watch"
    __table_args__ = (UniqueConstraint("tg_id", "code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(String(8), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ServerLog(Base):
    """Журнал действий по серверу: кто и что сделал (actor 0 — система)."""
    __tablename__ = "server_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(Integer, index=True)
    actor: Mapped[int] = mapped_column(BigInteger, default=0)
    action: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class ServerShare(Base):
    """Общий доступ к серверу: владелец приглашает другого клиента, тот видит сервер у себя."""
    __tablename__ = "server_shares"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(Integer, index=True)
    owner_tg: Mapped[int] = mapped_column(BigInteger, index=True)
    member_tg: Mapped[int] = mapped_column(BigInteger, index=True)
    # pending | active | declined | cancelled | revoked | left | expired
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ServerTransfer(Base):
    """Передача сервера другому клиенту: ждёт согласия получателя 24 часа."""
    __tablename__ = "server_transfers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(Integer, index=True)
    from_tg: Mapped[int] = mapped_column(BigInteger, index=True)
    to_tg: Mapped[int] = mapped_column(BigInteger, index=True)
    # pending | accepted | declined | cancelled | expired
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Tx(Base):
    """Журнал движения баланса клиентов."""

    __tablename__ = "tx"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount_kop: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24))
    ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PaymentMethod(Base):
    """Сохранённый в ЮKassa способ оплаты (для автоплатежей). Данные карты хранятся только маской."""

    __tablename__ = "payment_methods"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    yk_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(32), default="bank_card")
    title: Mapped[str] = mapped_column(String(64), default="")
    card_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    expiry: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # счёт СБП: банк плательщика (участник СБП и БИК) — для названия и эмблемы банка
    bank_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bank_bic: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bank_checked: Mapped[bool] = mapped_column(Boolean, default=False)  # устарело: см. bank_checked_at
    bank_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # user | admin | provider (ЮKassa отозвала разрешение)
    deleted_by: Mapped[str | None] = mapped_column(String(16), nullable=True)


class Promo(Base):
    __tablename__ = "promos"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # bonus — рубли на баланс, discount — % скидки на следующую покупку сервера
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[int] = mapped_column(Integer)
    max_uses: Mapped[int] = mapped_column(Integer, default=0)  # 0 — без ограничений
    used: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    new_users_only: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class PromoUse(Base):
    __tablename__ = "promo_uses"
    __table_args__ = (UniqueConstraint("promo_id", "tg_id"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(Integer, index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Consent(Base):
    """Журнал согласий: какой документ, какой версии, когда и откуда принят. Нужен как доказательство."""

    __tablename__ = "consents"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    doc: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16))  # bot | app
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)  # False — отзыв согласия
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Ticket(Base):
    """Обращение в поддержку."""

    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    subject: Mapped[str] = mapped_column(String(120))
    # open — ждёт нашего ответа, answered — ответили, ждём клиента, provider — передано провайдеру, closed — решено
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    server_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unread_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    unread_client: Mapped[bool] = mapped_column(Boolean, default=False)
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    # client | admin | note (внутренняя заметка, клиенту не видна) | system
    author: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(8), default="app")  # app | bot
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


async def init_db() -> None:
    global _engine, _sessionmaker
    url = env().database_url
    if url.startswith("sqlite") and ":///" in url:
        path = url.split(":///", 1)[1]
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
    _engine = create_async_engine(url, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn) -> None:
    """Простая миграция: добавляет в существующие таблицы колонки, появившиеся в новых версиях."""
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl_type = col.type.compile(dialect=conn.dialect)
            default = ""
            d = col.default.arg if col.default is not None and not callable(col.default.arg) else None
            if isinstance(d, bool):
                default = " DEFAULT " + ("TRUE" if d else "FALSE") if conn.dialect.name != "sqlite" else " DEFAULT " + ("1" if d else "0")
            elif isinstance(d, (int, float)):
                default = f" DEFAULT {d}"
            elif isinstance(d, str):
                default = " DEFAULT '" + d.replace("'", "''") + "'"
            null = "" if col.nullable or default else " NULL"
            conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default}{null}'))


@asynccontextmanager
async def session():
    assert _sessionmaker is not None, "init_db() не вызван"
    async with _sessionmaker() as s:
        yield s


async def get_or_create_user(s: AsyncSession, tg_id: int, username: str | None = None) -> User:
    u = await s.get(User, tg_id)
    if u is None:
        u = User(tg_id=tg_id, username=username)
        s.add(u)
        await s.commit()
    elif username and u.username != username:
        u.username = username
        await s.commit()
    return u


async def change_balance(s: AsyncSession, tg_id: int, delta_kop: int, kind: str, ref: str = "") -> bool:
    """Атомарно меняет баланс. Списание не уйдёт в минус. Возвращает успех."""
    stmt = update(User).where(User.tg_id == tg_id)
    if delta_kop < 0:
        stmt = stmt.where(User.balance_kop + delta_kop >= 0)
    res = await s.execute(stmt.values(balance_kop=User.balance_kop + delta_kop))
    if res.rowcount != 1:
        await s.rollback()
        return False
    s.add(Tx(tg_id=tg_id, amount_kop=delta_kop, kind=kind, ref=ref))
    await s.commit()
    return True
