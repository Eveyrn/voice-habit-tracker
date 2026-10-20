import os
import re
import pytz
import asyncio
from datetime import datetime, timedelta
from aiogram import Router, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database import db_manager
from services.stt import transcribe_audio
from services.ai_parser import parse_voice_text, analyze_rhythm
from services.calendar_api import add_event

router = Router()

# Меню «/» в Telegram — main.py ставит его при запуске
BOT_COMMANDS = [
    ("todo", "Задачи"),
    ("summary", "Итоги дня"),
    ("plan", "Разбор ритма и расписание"),
    ("habits", "Привычки и серии"),
    ("reminders", "Мои напоминания"),
    ("remind", "Новое напоминание: /remind 10:00 Витамины"),
    ("limit", "Стоп-сигнал на телефон"),
    ("patterns", "Повторяющиеся дела"),
    ("backup", "Копия базы"),
    ("start", "Справка"),
]

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
        f"🕵️‍♂️ Режим наблюдения: день {profile['days_passed']} из {profile['target_days']}\n"
        "В эти 3 недели Календарь отдыхает и не засоряется. Я просто внимательно собираю статистику "
        "твоего естественного ритма дня, циклы быта и отдыха.\n\n"
        "Разбор ритма и предложение расписания — в любой момент по /plan (точнее всего через 1–3 недели записей). "
        "После 3 недель дела начнут попадать в Google Календарь (он же виден в Samsung / Redmi).\n\n"
        "🎙 Просто присылай голосовые (или пиши текстом) о том, как прошёл день или дело:\n"
        "- 'С 14:00 до 18:00 работала, потом час смотрела сериал'\n\n"
        "Команды:\n"
        "/patterns — выявленные циклы и ритмы\n"
        "/todo — список текущих задач\n"
        "/habits — прогресс привычек\n"
        "/remind 10:00 Выпить витамины — ежедневное напоминание\n"
        "/reminders — мои напоминания\n"
        "/summary — баланс времени за день\n"
        "/plan — разбор ритма и расписание (по воскресеньям приходит недельный)\n"
        "/limit 120 — предупредить, когда телефон за день больше 2 ч\n"
        "/backup — прислать копию базы (раз в неделю приходит сама)"
    )
    await message.answer(welcome_text)

# ---------- Стоп-сигнал на телефон ----------

DEFAULT_LIMIT_MIN = 120


async def phone_limit_note(user_id: int, today_date: str):
    """Текст предупреждения, если порог превышен и сегодня ещё не предупреждала; иначе None."""
    limit = int(await db_manager.get_setting(user_id, "phone_limit_min", DEFAULT_LIMIT_MIN))
    if limit <= 0 or await db_manager.get_setting(user_id, "phone_limit_warned") == today_date:
        return None
    spent = await db_manager.get_unproductive_minutes(user_id, today_date)
    if spent < limit:
        return None
    await db_manager.set_setting(user_id, "phone_limit_warned", today_date)
    return (f"🔴 Сегодня на телефон и залипание уже {_fmt_min(spent)} (твой порог — {_fmt_min(limit)}). "
            "Без оценок, просто чтобы ты знала. Может, 10 минут пройтись или воды выпить?")


@router.message(Command("limit"))
async def cmd_limit(message: types.Message):
    if not check_auth(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) == 2 and parts[1].isdigit():
        minutes = int(parts[1])
        await db_manager.set_setting(message.from_user.id, "phone_limit_min", minutes)
        await message.answer("Стоп-сигнал выключен." if minutes == 0 else
                             f"Ок: скажу один раз за день, когда телефон и залипание перевалят за {_fmt_min(minutes)}.")
        return
    current = int(await db_manager.get_setting(message.from_user.id, "phone_limit_min", DEFAULT_LIMIT_MIN))
    state = "выключен" if current == 0 else f"сейчас {_fmt_min(current)} в день"
    await message.answer(f"Стоп-сигнал на телефон: {state}.\nПоменять: /limit 90 (в минутах), выключить: /limit 0")

# ---------- Разбор ритма ----------

async def rhythm_report(user_id: int, days: int) -> str:
    since = (_now_local() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    stats, days_with_data = await db_manager.get_activity_stats(user_id, since)
    return await analyze_rhythm(stats, days_with_data)


@router.message(Command("plan"))
async def cmd_plan(message: types.Message):
    if not check_auth(message.from_user.id):
        return
    status = await message.answer("🧭 Смотрю твои записи за 3 недели…")
    text = await rhythm_report(message.from_user.id, 21)
    await status.edit_text(f"🧭 Твой ритм и расписание\n\n{text}")

# ---------- Бэкап ----------

BACKUP_CAPTION = "💾 Копия базы трекера. Сохрани её — если хостинг потеряет данные, из этого файла всё восстановится."


def backup_file():
    today = datetime.now(pytz.timezone(config.TIMEZONE)).strftime("%Y-%m-%d")
    return BufferedInputFile(db_manager.backup_bytes(), filename=f"tracker_{today}.db")


@router.message(Command("backup"))
async def cmd_backup(message: types.Message):
    if not check_auth(message.from_user.id):
        return
    await message.answer_document(backup_file(), caption=BACKUP_CAPTION)

# ---------- Напоминания ----------

TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")

# Напоминание «после еды» хранится с time = "meal": планировщик по часам его не трогает
# ("meal" > "23:59" при сравнении строк), а срабатывает оно, когда в записи есть дело категории «Еда».
MEAL = "meal"
MEAL_RE = re.compile(r"после\s+(еды|едой|при[её]ма\s+пищи|завтрака|обеда|ужина|перекуса)|во\s+время\s+еды|когда\s+поем", re.I)
PENDING_REMINDER = {}  # user_id -> текст напоминания, для которого ждём выбор времени


def _now_local():
    return datetime.now(pytz.timezone(config.TIMEZONE))


def reminder_keyboard(reminder_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Сделала", callback_data=f"remdone_{reminder_id}")
    builder.button(text="⏰ Через 30 мин", callback_data=f"remsnooze_{reminder_id}")
    builder.adjust(2)
    return builder.as_markup()


SNOOZE_MIN = 30


@router.callback_query(F.data.startswith("remsnooze_"))
async def process_reminder_snooze(callback: types.CallbackQuery):
    reminder_id = int(callback.data.split("_")[1])
    reminder = await db_manager.get_reminder(reminder_id, callback.from_user.id)
    if not reminder:
        await callback.answer("Это напоминание уже удалено")
        return
    until = _now_local() + timedelta(minutes=SNOOZE_MIN)
    await db_manager.snooze_reminder(reminder_id, callback.from_user.id, until.strftime("%Y-%m-%dT%H:%M"))
    await callback.answer("Отложено")
    await callback.message.edit_text(f"⏰ {reminder[1]}\nНапомню ещё раз в {until.strftime('%H:%M')}")


async def save_reminder(user_id: int, text: str, time_str: str):
    now = _now_local()
    await db_manager.add_reminder(user_id, text, time_str, now.strftime("%H:%M"), now.strftime("%Y-%m-%d"))


@router.message(Command("remind"))
async def cmd_remind(message: types.Message):
    if not check_auth(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        await message.answer(
            "Напиши, что напомнить, и, если хочешь, время:\n"
            "/remind 10:00 Выпить витамины\n"
            "/remind Пить таблетки после еды\n\n"
            "Буду напоминать каждый день, а кнопка «✅ Сделала» отметит привычку."
        )
        return

    first, _, tail = rest.partition(" ")
    m = TIME_RE.match(first)
    if m and tail.strip() and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        time_str = f"{int(m.group(1)):02d}:{m.group(2)}"
        text = tail.strip()
        await save_reminder(message.from_user.id, text, time_str)
        await message.answer(f"⏰ Буду напоминать каждый день в {time_str}: {text}")
        return

    text = rest[0].upper() + rest[1:]
    if MEAL_RE.search(text):
        await save_reminder(message.from_user.id, text, MEAL)
        await message.answer(f"🍽 Буду напоминать, как только запишешь еду: {text}")
        return

    # Времени нет — спрашиваем кнопками
    PENDING_REMINDER[message.from_user.id] = text
    builder = InlineKeyboardBuilder()
    for t in ("08:00", "10:00", "13:00", "19:00", "22:00"):
        builder.button(text=t, callback_data=f"remat_{t}")
    builder.button(text="🍽 После еды", callback_data=f"remat_{MEAL}")
    builder.adjust(5, 1)
    await message.answer(
        f"Когда напоминать «{text}»?\nИли напиши своё время: /remind 15:30 {text}",
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data.startswith("remat_"))
async def process_reminder_time(callback: types.CallbackQuery):
    when = callback.data.split("_", 1)[1]
    text = PENDING_REMINDER.pop(callback.from_user.id, None)
    if not text:
        await callback.answer("Напиши /remind ещё раз")
        return
    await save_reminder(callback.from_user.id, text, when)
    await callback.answer("Готово")
    label = "как только запишешь еду" if when == MEAL else f"каждый день в {when}"
    await callback.message.edit_text(f"⏰ Буду напоминать {label}: {text}")


async def _reminders_view(user_id: int):
    reminders = await db_manager.get_reminders(user_id)
    if not reminders:
        return "Напоминаний пока нет.\nДобавить: /remind 10:00 Выпить витамины", None

    builder = InlineKeyboardBuilder()
    text = "⏰ Ежедневные напоминания:\n\n"
    for r_id, r_text, r_time in reminders:
        when = "после еды" if r_time == MEAL else r_time
        text += f"{when} — {r_text}\n"
        builder.button(text=f"🗑 {when} {r_text[:20]}", callback_data=f"remdel_{r_id}")
    builder.adjust(1)
    return text, builder.as_markup()


@router.message(Command("reminders"))
async def cmd_reminders(message: types.Message):
    if not check_auth(message.from_user.id):
        return
    text, markup = await _reminders_view(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("remdel_"))
async def process_reminder_delete(callback: types.CallbackQuery):
    reminder_id = int(callback.data.split("_")[1])
    await db_manager.delete_reminder(reminder_id, callback.from_user.id)
    await callback.answer("Напоминание удалено")
    text, markup = await _reminders_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("remdone_"))
async def process_reminder_done(callback: types.CallbackQuery):
    reminder_id = int(callback.data.split("_")[1])
    reminder = await db_manager.get_reminder(reminder_id, callback.from_user.id)
    if not reminder:
        await callback.answer("Это напоминание уже удалено")
        return

    _, r_text, _ = reminder
    streak = await db_manager.update_habit(callback.from_user.id, r_text, _now_local().strftime("%Y-%m-%d"))
    await callback.answer("Отмечено!")
    await callback.message.edit_text(f"✅ {r_text} — сделано\n🔥 Серия: {streak} дн. подряд")

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

    today_str = _now_local().strftime("%Y-%m-%d")
    await message.answer(await day_report(message.from_user.id, today_str))


def _fmt_min(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} ч {m} мин" if h and m else (f"{h} ч" if h else f"{m} мин")


async def day_report(user_id: int, today_str: str) -> str:
    """Итог дня: баланс полезное/быт/телефон, категории, привычки из напоминаний, открытые задачи."""
    stats = await db_manager.get_today_summary(user_id, today_str)
    lines = []

    total = sum(int(d or 0) for _, _, d in stats)
    if total:
        lines.append(f"📊 Итоги дня — учтено {_fmt_min(total)}\n")
        by_utility = {}
        for utility, _, duration in stats:
            by_utility[utility] = by_utility.get(utility, 0) + int(duration or 0)
        for key, label in (("productive", "🟢 Полезное"), ("routine", "⚪ Быт"), ("unproductive", "🔴 Прокрастинация")):
            if by_utility.get(key):
                lines.append(f"{label}: {_fmt_min(by_utility[key])} · {round(by_utility[key] * 100 / total)}%")

        by_category = {}
        for _, category, duration in stats:
            by_category[category] = by_category.get(category, 0) + int(duration or 0)
        top = sorted(by_category.items(), key=lambda kv: -kv[1])
        lines.append("\n" + " · ".join(f"{c} {_fmt_min(d)}" for c, d in top))
    else:
        lines.append("📊 За сегодня пока нет записанных дел.")

    reminders = await db_manager.get_reminders(user_id)
    if reminders:
        done_today = {name for name, _, last in await db_manager.get_habits(user_id) if last == today_str}
        marks = [("✅ " if text in done_today else "▫️ ") + text for _, text, _ in reminders]
        lines.append("\nПривычки:\n" + "\n".join(marks))

    tasks = await db_manager.get_pending_tasks(user_id)
    if tasks:
        shown = "\n".join(f"- {title}" for _, title, _ in tasks[:5])
        more = f"\n…и ещё {len(tasks) - 5}" if len(tasks) > 5 else ""
        lines.append(f"\nОткрытые задачи ({len(tasks)}):\n{shown}{more}")

    return "\n".join(lines)

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
        await process_entry(message, status_msg, transcript)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    """То же, что голосовое, только без расшифровки."""
    if not check_auth(message.from_user.id):
        return
    status_msg = await message.reply("Разбираю...")
    await process_entry(message, status_msg, message.text.strip())


async def process_entry(message: types.Message, status_msg: types.Message, transcript: str):
    """Разбор текста дня: дела → база (+календарь), задачи, привычки, напоминания."""
    try:
        tz = pytz.timezone(config.TIMEZONE)
        now_dt = datetime.now(tz)
        today_date = now_dt.strftime("%Y-%m-%d")

        profile = await db_manager.get_or_create_profile(message.from_user.id, today_date)

        await status_msg.edit_text(f"\"{transcript}\"\n\nСоставляю хронологию...")

        # 2. AI-анализ
        known_titles = [row[0] for row in await db_manager.get_top_patterns(message.from_user.id)][:60]
        analysis = await parse_voice_text(transcript, known_titles)
        activity_ids, task_ids = [], []

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
            calendar_link = None
            if profile['phase'] == 'coaching':
                calendar_link = await asyncio.to_thread(
                    add_event,
                    summary=title,
                    start_time=start_time,
                    end_time=end_time,
                    utility=utility,
                    description=f"Категория: {category} | Из голосового трекера"
                )

            activity_id = await db_manager.add_activity(
                user_id=message.from_user.id,
                title=title,
                category=category,
                utility=utility,
                duration_min=duration,
                start_time=start_time,
                end_time=end_time
            )
            activity_ids.append(activity_id)

            # Записываем паттерн в базу
            await db_manager.record_pattern(
                user_id=message.from_user.id,
                title=title,
                utility=utility,
                date_str=today_date
            )

            icon = "🟢 Полезное" if utility == "productive" else ("⚪ Бытовое" if utility == "routine" else "🔴 Прокрастинация / Отдых")
            if profile['phase'] != 'coaching':
                sync_label = "Сохранено в профиль наблюдения (Календарь подключится через 3 нед.)"
            elif calendar_link:
                sync_label = "Синхронизировано в Календарь"
            else:
                sync_label = "Сохранено в боте (Календарь сейчас недоступен)"
            response_lines.append(f"⏱ {time_label}{title} ({duration} мин)\nКатегория: {icon}\n{sync_label}")

        # 4. To-Do
        new_tasks = analysis.get("tasks_to_add", [])
        if new_tasks:
            response_lines.append("\nДобавлено в To-Do:")
            for t in new_tasks:
                task_ids.append(await db_manager.add_task(message.from_user.id, t))
                response_lines.append(f"- {t}")

        # 5. Привычки
        habit = analysis.get("habit_identified")
        if habit:
            streak = await db_manager.update_habit(message.from_user.id, habit, today_date)
            response_lines.append(f"\nПривычка: {habit} (Серия: {streak} дн.)")

        # 5.1 Просьбы напоминать
        for rem in analysis.get("reminders", []):
            if rem["time"]:
                await save_reminder(message.from_user.id, rem["text"], rem["time"])
                response_lines.append(f"\n⏰ Буду напоминать каждый день в {rem['time']}: {rem['text']}")
            elif MEAL_RE.search(rem["text"]):
                await save_reminder(message.from_user.id, rem["text"], MEAL)
                response_lines.append(f"\n🍽 Буду напоминать, как только запишешь еду: {rem['text']}")
            else:
                response_lines.append(
                    f"\n⏰ Во сколько напоминать «{rem['text']}»? Напиши, например:\n"
                    f"/remind 10:00 {rem['text']}"
                )

        # 6. Поддержка
        encouragement = analysis.get("encouragement")
        if encouragement:
            response_lines.append(f"\n{encouragement}")

        # Мягкий стоп-сигнал: один раз в день, когда телефон/игры перевалили за порог
        if any(isinstance(a, dict) and a.get("utility") == "unproductive" for a in activities):
            note = await phone_limit_note(message.from_user.id, today_date)
            if note:
                response_lines.append(f"\n{note}")

        builder = InlineKeyboardBuilder()

        # Напоминания «после еды»: если в записи была еда
        if any(isinstance(a, dict) and a.get("category") == "Еда" for a in activities):
            for r_id, r_text, r_time in await db_manager.get_reminders(message.from_user.id):
                if r_time == MEAL:
                    response_lines.append(f"\n🍽 Не забудь: {r_text}")
                    builder.button(text=f"✅ {r_text[:30]}", callback_data=f"remdone_{r_id}")

        if activity_ids or task_ids:
            builder.button(text="↩️ Отменить запись", callback_data=f"undo_{_ids(activity_ids)}_{_ids(task_ids)}")
        builder.adjust(1)
        markup = builder.as_markup() if list(builder.buttons) else None
        await status_msg.edit_text("\n".join(response_lines), reply_markup=markup)

    except Exception as e:
        import traceback
        traceback.print_exc()
        await status_msg.edit_text(f"Ошибка обработки: {str(e)[:200]}")


def _ids(ids):
    # id в одной записи идут подряд (бот для одного человека), поэтому храним диапазон
    # «первый-последний»: callback_data ограничен 64 байтами
    return f"{min(ids)}-{max(ids)}" if ids else "0-0"


@router.callback_query(F.data.startswith("undo_"))
async def process_undo(callback: types.CallbackQuery):
    _, acts, tasks = callback.data.split("_")
    a_from, a_to = map(int, acts.split("-"))
    t_from, t_to = map(int, tasks.split("-"))
    had_calendar = await db_manager.undo_entry(callback.from_user.id, a_from, a_to, t_from, t_to)
    await callback.answer("Запись отменена")
    note = "\n\n↩️ Отменено — дела и задачи из этой записи удалены."
    if had_calendar:
        note += "\nСобытия в Google Календаре удали вручную."
    await callback.message.edit_text((callback.message.text or "") + note)