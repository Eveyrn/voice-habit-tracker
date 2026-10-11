import os
import pytz
import asyncio
from datetime import datetime
from aiogram import Router, F, types
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database import db_manager
from services.stt import transcribe_audio
from services.ai_parser import parse_voice_text
from services.calendar_api import add_event

router = Router()

def check_auth(user_id: int) -> bool:
    if config.ALLOWED_USER_IDS and user_id not in config.ALLOWED_USER_IDS:
        return False
    return True

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    if not check_auth(message.from_user.id):
        await message.reply("Извините, этот бот является приватным.")
        return

    tz = pytz.timezone(config.TIMEZONE)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    profile = await db_manager.get_or_create_profile(message.from_user.id, today_str)

    welcome_text = (
        "Привет! Я твой персональный AI-трекер жизни и привычек.\n\n"
        f"🕵️‍♂️ **Режим наблюдения: День {profile['days_passed']} из {profile['target_days']}**\n"
        "В эти 3 недели Календарь отдыхает и не засоряется. Я просто внимательно собираю статистику "
        "твоего естественного ритма дня, циклы быта и отдыха.\n\n"
        "Через 3 недели я автоматически сформирую твоё идеальное расписание и подключу Google / Samsung / Redmi календарь!\n\n"
        "🎙 Просто присылай голосовые о том, как прошёл день или дело:\n"
        "- 'С 14:00 до 18:00 работала, потом час смотрела сериал'\n\n"
        "Команды:\n"
        "/patterns — выявленные циклы и ритмы\n"
        "/todo — список текущих задач\n"
        "/habits — прогресс привычек\n"
        "/summary — баланс времени за день"
    )
    await message.answer(welcome_text)

@router.message(Command("patterns"))
async def cmd_patterns(message: types.Message):
    if not check_auth(message.from_user.id):
        return

    tz = pytz.timezone(config.TIMEZONE)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    profile = await db_manager.get_or_create_profile(message.from_user.id, today_str)
    patterns = await db_manager.get_top_patterns(message.from_user.id)

    status_str = f"Изучаю твой ритм (до включения календаря: {max(0, profile['target_days'] - profile['days_passed'])} дн.)" if profile['phase'] == 'observation' else "Активный режим (Календарь подключен)"

    header = (
        f"🕵️‍♂️ **Профиль наблюдения: День {profile['days_passed']} из {profile['target_days']}**\n"
        f"Статус: {status_str}\n\n"
    )

    if not patterns:
        text = header + "Пока недостаточно данных. Продолжай надиктовывать свои активности за день!"
        await message.answer(text)
        return

    text = header + "📊 **Зафиксированные активности и периодичность:**\n\n"
    for title, utility, count, last_date, avg_interval in patterns:
        icon = "🟢" if utility == "productive" else ("⚪" if utility == "routine" else "🔴")
        interval_txt = f"каждые ~{avg_interval} дн." if avg_interval > 0 else "пока 1 раз"
        text += f"{icon} **{title}**: зафиксировано {count} раз ({interval_txt}, посл. {last_date})\n"

    await message.answer(text)

@router.message(Command("todo"))
async def cmd_todo(message: types.Message):
    if not check_auth(message.from_user.id):
        return

    tasks = await db_manager.get_pending_tasks(message.from_user.id)
    if not tasks:
        await message.answer("🎉 У тебя нет незавершенных задач!")
        return

    builder = InlineKeyboardBuilder()
    text = "📋 Твой список To-Do:\n\n"
    for task_id, title, due_date in tasks:
        text += f"- {title}\n"
        builder.button(text=f"✅ {title[:20]}", callback_data=f"done_{task_id}")
    
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("done_"))
async def process_task_done(callback: types.CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    await db_manager.mark_task_done(task_id, callback.from_user.id)
    await callback.answer("Задача выполнена!")
    
    tasks = await db_manager.get_pending_tasks(callback.from_user.id)
    if not tasks:
        await callback.message.edit_text("🎉 Все задачи закрыты! Отличная работа.")
        return
        
    builder = InlineKeyboardBuilder()
    text = "📋 Твой список To-Do:\n\n"
    for t_id, title, due_date in tasks:
        text += f"- {title}\n"
        builder.button(text=f"✅ {title[:20]}", callback_data=f"done_{t_id}")
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup())

@router.message(Command("habits"))
async def cmd_habits(message: types.Message):
    if not check_auth(message.from_user.id):
        return

    habits = await db_manager.get_habits(message.from_user.id)
    if not habits:
        await message.answer("🌱 Пока нет отслеживаемых привычек.")
        return

    text = "🔥 Твои привычки:\n\n"
    for name, streak, last_completed in habits:
        text += f"- {name}: {streak} дн. подряд (посл. {last_completed})\n"
    await message.answer(text)

@router.message(Command("summary"))
async def cmd_summary(message: types.Message):
    if not check_auth(message.from_user.id):
        return

    tz = pytz.timezone(config.TIMEZONE)
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    stats = await db_manager.get_today_summary(message.from_user.id, today_str)

    if not stats:
        await message.answer("📊 За сегодня пока нет записанных активностей.")
        return

    total_time = sum(item[2] for item in stats)
    text = f"📊 Итоги дня ({today_str}):\nОбщее учтённое время: {total_time} мин ({round(total_time/60, 1)} ч)\n\n"

    for utility, category, duration in stats:
        icon = "[Польза]" if utility == "productive" else ("[Быт]" if utility == "routine" else "[Прокрастинация]")
        text += f"{icon} {category}: {duration} мин\n"

    await message.answer(text)

@router.message(F.voice | F.audio)
async def handle_voice(message: types.Message):
    if not check_auth(message.from_user.id):
        return

    status_msg = await message.reply("Слушаю и расшифровываю...")

    file_id = message.voice.file_id if message.voice else message.audio.file_id
    file_info = await message.bot.get_file(file_id)
    temp_path = f"temp_{file_id}.ogg"
    await message.bot.download_file(file_info.file_path, destination=temp_path)

    try:
        # 1. Быстрый Groq Whisper
        transcript = await transcribe_audio(temp_path)
        if not transcript.strip():
            await status_msg.edit_text("Не удалось распознать слова в аудиосообщении.")
            return

        tz = pytz.timezone(config.TIMEZONE)
        now_dt = datetime.now(tz)
        today_date = now_dt.strftime("%Y-%m-%d")

        profile = await db_manager.get_or_create_profile(message.from_user.id, today_date)

        await status_msg.edit_text(f"\"{transcript}\"\n\nСоставляю хронологию...")

        # 2. AI-анализ
        analysis = await parse_voice_text(transcript)

        response_lines = [f"\"{transcript}\"\n"]

        # 3. Обработка активностей
        activities = analysis.get("activities", [])

        for act in activities:
            title = act.get("title", "Активность")
            utility = act.get("utility", "routine")
            category = act.get("category", "Быт")
            duration = act.get("duration_minutes", 30)
            start_time = act.get("start_time")
            end_time = act.get("end_time")

            time_label = ""
            if start_time and end_time:
                try:
                    s_clean = start_time.replace("Z", "")
                    e_clean = end_time.replace("Z", "")
                    s_dt = datetime.fromisoformat(s_clean)
                    e_dt = datetime.fromisoformat(e_clean)
                    time_label = f"[{s_dt.strftime('%H:%M')} - {e_dt.strftime('%H:%M')}] "
                except Exception:
                    pass

            # КАЛЕНДАРЬ: если ещё идёт фаза наблюдения, события в календарь НЕ отправляются!
            if profile['phase'] == 'coaching':
                await asyncio.to_thread(
                    add_event,
                    summary=title,
                    start_time=start_time,
                    end_time=end_time,
                    utility=utility,
                    description=f"Категория: {category} | Из голосового трекера"
                )

            await db_manager.add_activity(
                user_id=message.from_user.id,
                title=title,
                category=category,
                utility=utility,
                duration_min=duration,
                start_time=start_time,
                end_time=end_time
            )

            # Записываем паттерн в базу
            await db_manager.record_pattern(
                user_id=message.from_user.id,
                title=title,
                utility=utility,
                date_str=today_date
            )

            icon = "🟢 Полезное" if utility == "productive" else ("⚪ Бытовое" if utility == "routine" else "🔴 Прокрастинация / Отдых")
            sync_label = "Синхронизировано в Календарь" if profile['phase'] == 'coaching' else "Сохранено в профиль наблюдения (Календарь подключится через 3 нед.)"
            response_lines.append(f"⏱ {time_label}{title} ({duration} мин)\nКатегория: {icon}\n{sync_label}")

        # 4. To-Do
        new_tasks = analysis.get("tasks_to_add", [])
        if new_tasks:
            response_lines.append("\nДобавлено в To-Do:")
            for t in new_tasks:
                await db_manager.add_task(message.from_user.id, t)
                response_lines.append(f"- {t}")

        # 5. Привычки
        habit = analysis.get("habit_identified")
        if habit:
            streak = await db_manager.update_habit(message.from_user.id, habit, today_date)
            response_lines.append(f"\nПривычка: {habit} (Серия: {streak} дн.)")

        # 6. Поддержка
        encouragement = analysis.get("encouragement")
        if encouragement:
            response_lines.append(f"\n{encouragement}")

        await status_msg.edit_text("\n".join(response_lines))

    except Exception as e:
        import traceback
        traceback.print_exc()
        await status_msg.edit_text(f"Ошибка обработки: {str(e)[:200]}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)