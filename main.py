import asyncio
import logging
import pytz
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from bot.handlers import router, reminder_keyboard, backup_file, BACKUP_CAPTION
from database.db_manager import (
    init_db, get_or_create_profile, get_today_summary,
    get_due_reminders, mark_reminder_sent,
)

logging.basicConfig(level=logging.INFO)

async def send_checkin(bot: Bot, text: str):
    for user_id in config.ALLOWED_USER_IDS:
        try:
            await bot.send_message(chat_id=user_id, text=text)
        except Exception as e:
            logging.error(f"Ошибка отправки чекина {user_id}: {e}")

async def morning_checkin(bot: Bot):
    tz = pytz.timezone(config.TIMEZONE)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    profile = await get_or_create_profile(config.ALLOWED_USER_IDS[0], today_str)
    
    msg = (
        f"☀️ Доброе утро!\n\n"
        f"🕵️‍♂️ Фаза наблюдения: День {profile['days_passed']} из {profile['target_days']}.\n"
        "Какие планы на первую половину дня? Запиши мне голосовое или набросай список задач!"
    )
    await send_checkin(bot, msg)

async def afternoon_checkin(bot: Bot):
    msg = (
        "🌤 Привет! Как проходит день?\n\n"
        "Чем успела позаниматься за прошедшие часы? Запиши мне короткое голосовое, чтобы ничего не забыть зафиксировать 🎙"
    )
    await send_checkin(bot, msg)

async def evening_checkin(bot: Bot):
    tz = pytz.timezone(config.TIMEZONE)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    stats = await get_today_summary(config.ALLOWED_USER_IDS[0], today_str)
    
    total = sum(i[2] for i in stats) if stats else 0
    msg = (
        f"🌙 Добрый вечер!\n\n"
        f"Сегодня учтено: {total} мин ({round(total/60, 1)} ч).\n"
        "Расскажи в голосовом, как прошёл вечер и чем ещё занималась перед сном!"
    )
    await send_checkin(bot, msg)

async def send_due_reminders(bot: Bot):
    """Раз в минуту: шлёт напоминания, время которых наступило и которые сегодня ещё не уходили.
    Если бот лежал в нужную минуту — напоминание придёт сразу после запуска."""
    now = datetime.now(pytz.timezone(config.TIMEZONE))
    today_str = now.strftime("%Y-%m-%d")
    for reminder_id, user_id, text in await get_due_reminders(now.strftime("%H:%M"), today_str):
        try:
            await bot.send_message(chat_id=user_id, text=f"⏰ {text}", reply_markup=reminder_keyboard(reminder_id))
        except Exception as e:
            logging.error(f"Ошибка отправки напоминания {reminder_id}: {e}")
        # Отмечаем в любом случае, чтобы при ошибке не слать каждую минуту
        await mark_reminder_sent(reminder_id, today_str)

async def send_backup(bot: Bot):
    """Раз в неделю присылает файл базы в Telegram — копия на случай, если хостинг потеряет данные."""
    for user_id in config.ALLOWED_USER_IDS:
        try:
            await bot.send_document(chat_id=user_id, document=backup_file(), caption=BACKUP_CAPTION)
        except Exception as e:
            logging.error(f"Ошибка отправки бэкапа {user_id}: {e}")

async def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env файле!")
    if not config.ALLOWED_USER_IDS:
        # Без списка бот открыт всем, и чужие люди тратят лимит Groq
        raise ValueError("ALLOWED_USER_IDS не задан в .env — бот не запущен, чтобы не быть открытым для всех")

    await init_db()

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Планировщик периодических напоминаний
    tz = pytz.timezone(config.TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)
    
    # Расписание чекинов (11:00, 16:00, 21:30 по Бишкеку)
    scheduler.add_job(morning_checkin, 'cron', hour=11, minute=0, args=[bot])
    scheduler.add_job(afternoon_checkin, 'cron', hour=16, minute=0, args=[bot])
    scheduler.add_job(evening_checkin, 'cron', hour=21, minute=30, args=[bot])
    scheduler.add_job(send_due_reminders, 'cron', minute='*', args=[bot])
    scheduler.add_job(send_backup, 'cron', day_of_week='sun', hour=22, minute=0, args=[bot])

    scheduler.start()
    logging.info("Планировщик напоминаний запущен (11:00, 16:00, 21:30).")

    logging.info("Бот запущен и готов к работе...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")