import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from app import bot as botmod
from app.astra import Astra
from app.config import env
from app.db import init_db
from app.integrations import load_integrations
from app.nalog import Nalog
from app.platega import Platega
from app.services import Ctx
from app.web.server import build_app
from app.worker import start_workers
from app.yookassa import YooKassa


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    e = env()
    await init_db()

    bot = Bot(e.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    notify, alert_admin = botmod.make_notifier(bot)
    ctx = Ctx(astra=Astra(), yk=YooKassa(), nalog=Nalog(), notify=notify, alert_admin=alert_admin, platega=Platega())
    botmod.set_ctx(ctx)
    ctx.notify_btn = botmod.make_button_notifier(bot)
    ctx.check_member, ctx.probe_chat = botmod.make_member_checker(bot)
    ctx.send_broadcast = botmod.make_broadcast_sender(bot)
    ctx.post, ctx.post_file = botmod.make_poster(bot)
    ctx.tg_ping = bot.get_me
    ctx.send_transfer_offer = botmod.make_transfer_offer(bot)
    ctx.send_share_offer = botmod.make_transfer_offer(bot, "sh")
    ctx.notify_kb = botmod.make_kb_notifier(bot)
    from app import events
    ctx.alert_admin = events.make_alert(ctx)  # системные алерты — в чат «Алерты» (или админам)
    try:
        ctx.bot_username = (await bot.get_me()).username or ""
    except Exception:
        logging.getLogger(__name__).warning("get_me не удался", exc_info=True)
    await load_integrations(ctx)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(botmod.router)
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    app_url = e.public_url.rstrip("/") + "/app"
    if app_url.startswith("https://"):
        try:  # кнопка «Кабинет» слева от поля ввода
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=app_url)))
        except Exception:
            logging.getLogger(__name__).warning("не удалось задать кнопку меню", exc_info=True)

    server = uvicorn.Server(uvicorn.Config(build_app(ctx), host=e.web_host, port=e.web_port, log_level="info"))
    workers = start_workers(ctx)
    web = asyncio.create_task(server.serve(), name="web")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])  # подписку проверяем запросом, chat_member не нужен
    finally:
        server.should_exit = True
        for t in workers:
            t.cancel()
        await asyncio.gather(web, *workers, return_exceptions=True)
        await ctx.astra.close()
        await ctx.yk.close()
        await ctx.nalog.close()
        await ctx.platega.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
