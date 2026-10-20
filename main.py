import os
import asyncio
import logging
import pytz
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from bot.handlers import (
    router, BOT_COMMANDS, ADMIN_COMMANDS, reminder_keyboard, backup_file, BACKUP_CAPTION,
    day_report, rhythm_report, plans_for_day, all_user_ids, load_users, ensure_owner_about, oauth_callback,
)
from services import google_oauth, web_pages
from database.db_manager import (
    init_db, get_or_create_profile, cleanup_old_reminders,
    get_due_reminders, mark_reminder_sent, pop_snoozed_reminders,
)

logging.basicConfig(level=logging.INFO)
# Планировщик раз в минуту пишет «Running job … executed successfully» — в консоли хостинга за этим не видно ошибок
logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _today():
    return datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d")


async def send_to(bot: Bot, user_id: int, text: str, **kwargs):
    try:
        await bot.send_message(chat_id=user_id, text=text, **kwargs)
    except Exception as e:
        # Человек мог заблокировать бота — это не повод ронять рассылку остальным
        logging.error(f"Не удалось отправить {user_id}: {e}")


async def morning_checkin(bot: Bot):
    today = _today()
    for user_id in all_user_ids():
        profile = await get_or_create_profile(user_id, today)
        plans = await plans_for_day(user_id, today)
        plans_text = ("Сегодня в планах:\n" + "\n".join(plans) + "\n\n") if plans else ""
        await send_to(bot, user_id,
                      "☀️ Доброе утро!\n\n"
                      f"{plans_text}"
                      f"🕵️ Наблюдение: день {profile['days_passed']} из {profile['target_days']}.\n"
                      "Какие планы на первую половину дня? Запиши голосовое или набросай задачи!")


async def afternoon_checkin(bot: Bot):
    for user_id in all_user_ids():
        await send_to(bot, user_id,
                      "🌤 Привет! Как проходит день?\n\n"
                      "Что было за последние часы? Запиши короткое голосовое, чтобы ничего не потерялось 🎙")


async def evening_checkin(bot: Bot):
    today = _today()
    for user_id in all_user_ids():
        report = await day_report(user_id, today)
        await send_to(bot, user_id,
                      f"🌙 Добрый вечер!\n\n{report}\n\n"
                      "Расскажи в голосовом, как прошёл вечер и что ещё было перед сном!")


async def send_due_reminders(bot: Bot):
    """Раз в минуту: шлёт напоминания, время которых наступило и которые сегодня ещё не уходили.
    Если бот лежал в нужную минуту — напоминание придёт сразу после запуска."""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    today_str = now.strftime("%Y-%m-%d")
    await cleanup_old_reminders(today_str)

    for reminder_id, user_id, text in await pop_snoozed_reminders(now.strftime("%Y-%m-%dT%H:%M")):
        await send_to(bot, user_id, f"⏰ {text}", reply_markup=reminder_keyboard(reminder_id))

    for reminder_id, user_id, text in await get_due_reminders(now.strftime("%H:%M"), today_str):
        await send_to(bot, user_id, f"⏰ {text}", reply_markup=reminder_keyboard(reminder_id))
        # Отмечаем в любом случае, чтобы при ошибке не слать каждую минуту
        await mark_reminder_sent(reminder_id, today_str)


async def weekly_review(bot: Bot):
    """Воскресенье: разбор ритма за неделю — каждому."""
    for user_id in all_user_ids():
        text = await rhythm_report(user_id, 7)
        await send_to(bot, user_id, f"🗓 Итоги недели\n\n{text}")


async def send_backup(bot: Bot):
    """Раз в неделю присылает файл базы админам — копия на случай, если хостинг потеряет данные."""
    for user_id in config.ALLOWED_USER_IDS:
        try:
            await bot.send_document(chat_id=user_id, document=backup_file(), caption=BACKUP_CAPTION)
        except Exception as e:
            logging.error(f"Ошибка отправки бэкапа {user_id}: {e}")


async def set_commands(bot: Bot):
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS],
                                  scope=BotCommandScopeDefault())
        for admin_id in config.ALLOWED_USER_IDS:
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in ADMIN_COMMANDS],
                                      scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception as e:
        logging.error(f"Не удалось поставить меню команд: {e}")


async def start_web(bot: Bot):
    """Маленький веб-сервер на том же процессе: сюда Google возвращает человека после входа (/calendar).
    Хостинг пробрасывает свой HTTPS-домен на SERVER_PORT."""
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", lambda request: web.Response(text=web_pages.HOME, content_type="text/html"))
    app.router.add_get("/privacy", lambda request: web.Response(text=web_pages.PRIVACY, content_type="text/html"))
    app.router.add_get(google_oauth.CALLBACK_PATH, oauth_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("SERVER_PORT", "8080"))
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logging.info(f"Веб-адрес для входа в Google слушает порт {port} ({google_oauth.redirect_uri() if google_oauth.enabled() else 'PUBLIC_URL не задан'})")
    except OSError as e:
        # Порт занят (например, локальный запуск) — бот работает дальше, просто без /calendar
        logging.error(f"Не удалось открыть порт {port}: {e}")


async def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env файле!")
    if not config.ALLOWED_USER_IDS:
        # Без админа некому открывать доступ, а бот был бы открыт всем и тратил лимит Groq
        raise ValueError("ALLOWED_USER_IDS не задан в .env — бот не запущен")

    await init_db()
    await load_users()
    await ensure_owner_about()

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=pytz.timezone(config.TIMEZONE))
    scheduler.add_job(morning_checkin, 'cron', hour=11, minute=0, args=[bot])
    scheduler.add_job(afternoon_checkin, 'cron', hour=16, minute=0, args=[bot])
    scheduler.add_job(evening_checkin, 'cron', hour=21, minute=30, args=[bot])
    scheduler.add_job(send_due_reminders, 'cron', minute='*', args=[bot])
    scheduler.add_job(weekly_review, 'cron', day_of_week='sun', hour=20, minute=0, args=[bot])
    scheduler.add_job(send_backup, 'cron', day_of_week='sun', hour=22, minute=0, args=[bot])
    scheduler.start()
    logging.info("Планировщик запущен (11:00, 16:00, 21:30, напоминания раз в минуту).")

    await start_web(bot)
    await set_commands(bot)
    logging.info(f"Бот запущен. Пользователей по заявкам: {len(all_user_ids()) - len(config.ALLOWED_USER_IDS)}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
