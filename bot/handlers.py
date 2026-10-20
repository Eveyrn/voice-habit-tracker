import os
import re
import time
import secrets
import pytz
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Router, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from google.auth.exceptions import RefreshError

import config
from database import db_manager
from services import google_oauth
from services.stt import transcribe_audio
from services.ai_parser import parse_voice_text, analyze_rhythm, CATEGORIES
from services.calendar_api import add_event, build_event

router = Router()

# Меню «/» в Telegram — main.py ставит его при запуске (админам — с их командами)
BOT_COMMANDS = [
    ("todo", "Задачи"),
    ("summary", "Итоги дня"),
    ("plan", "Разбор ритма и расписание"),
    ("habits", "Привычки и серии"),
    ("reminders", "Мои напоминания"),
    ("remind", "Новое напоминание: /remind 10:00 Витамины"),
    ("calendar", "Подключить свой Google Календарь"),
    ("limit", "Стоп-сигнал на телефон"),
    ("about", "Рассказать о себе"),
    ("patterns", "Повторяющиеся дела"),
    ("deleteme", "Стереть мои данные"),
    ("start", "Справка"),
]
ADMIN_COMMANDS = BOT_COMMANDS + [
    ("users", "Кто пользуется ботом"),
    ("backup", "Копия базы"),
]

# «О себе» владельца по умолчанию — раньше это было зашито в промпт для всех
DEFAULT_OWNER_ABOUT = (
    "Разработчица: делает Telegram-ботов и проект ELGE (сайт недвижимости). "
    "Работа над ботом, кодом, ELGE, дизайном — это полезное (productive)."
)


def _now_local():
    return datetime.now(pytz.timezone(config.TIMEZONE))


def _fmt_min(minutes: int) -> str:
    h, m = divmod(int(minutes or 0), 60)
    return f"{h} ч {m} мин" if h and m else (f"{h} ч" if h else f"{m} мин")


def _hm(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "")).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


# ---------- Доступ ----------

ACTIVE_USERS: set[int] = set()  # кого пустили по заявке; админы (ALLOWED_USER_IDS) — всегда


def is_admin(user_id: int) -> bool:
    return user_id in config.ALLOWED_USER_IDS


def check_auth(user_id: int) -> bool:
    return is_admin(user_id) or user_id in ACTIVE_USERS


def all_user_ids() -> list[int]:
    return list(dict.fromkeys([*config.ALLOWED_USER_IDS, *ACTIVE_USERS]))


async def load_users():
    ACTIVE_USERS.clear()
    ACTIVE_USERS.update(await db_manager.active_user_ids())


async def ensure_owner_about():
    if config.OWNER_ID and not await db_manager.get_setting(config.OWNER_ID, "about"):
        await db_manager.set_setting(config.OWNER_ID, "about", DEFAULT_OWNER_ABOUT)


def _access_keyboard(user_id: int, name: str):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Пустить", callback_data=f"uok_{user_id}")
    builder.button(text="🚫 Нет", callback_data=f"uno_{user_id}")
    builder.adjust(2)
    return builder.as_markup()


async def request_access(message: types.Message):
    """Чужой человек нажал /start: заявка владельцу с кнопками «Пустить / Нет»."""
    uid = message.from_user.id
    user = await db_manager.get_user(uid)
    if user and user[3] == "blocked":
        await message.answer("Доступ к этому боту закрыт.")
        return
    if user and user[3] == "pending":
        await message.answer("Запрос уже у владельца бота — напишу, как только доступ откроют.")
        return

    name = message.from_user.full_name or "Без имени"
    username = message.from_user.username
    await db_manager.add_user_request(uid, name, username)
    await message.answer(
        "Привет! Это трекер дня: рассказываешь голосом или текстом, как прошёл день, а бот раскладывает "
        "дела по времени, ведёт задачи, привычки и напоминания.\n\n"
        "Бот личный, поэтому сначала владелец открывает доступ. Запрос отправлен — напишу, как только ответят."
    )
    who = f"{name}" + (f" (@{username})" if username else "") + f", id {uid}"
    for admin_id in config.ALLOWED_USER_IDS:
        try:
            await message.bot.send_message(admin_id, f"🙋 Просит доступ к боту: {who}",
                                           reply_markup=_access_keyboard(uid, name))
        except Exception as e:
            print(f"Не удалось отправить заявку админу {admin_id}: {e}")


async def deny(message: types.Message):
    user = await db_manager.get_user(message.from_user.id)
    if user and user[3] == "pending":
        await message.answer("Запрос на доступ ещё у владельца бота — напишу, как только откроют.")
    elif user and user[3] == "blocked":
        await message.answer("Доступ к этому боту закрыт.")
    else:
        await message.answer("Это личный трекер дня. Чтобы попросить доступ, нажми /start.")


@router.callback_query(F.data.startswith("uok_") | F.data.startswith("uno_"))
async def process_access(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    allow = callback.data.startswith("uok_")
    uid = int(callback.data.split("_")[1])
    user = await db_manager.get_user(uid)
    name = user[1] if user else str(uid)
    await db_manager.set_user_status(uid, "active" if allow else "blocked")
    if allow:
        ACTIVE_USERS.add(uid)
    else:
        ACTIVE_USERS.discard(uid)
    await callback.answer("Готово")
    await callback.message.edit_text(f"{'✅ Доступ открыт' if allow else '🚫 Доступ закрыт'}: {name}")
    try:
        if allow:
            profile = await db_manager.get_or_create_profile(uid, _now_local().strftime("%Y-%m-%d"))
            await callback.bot.send_message(uid, "🎉 Доступ открыт!\n\n" + welcome_text(uid, profile))
        else:
            await callback.bot.send_message(uid, "Владелец бота закрыл доступ.")
    except Exception as e:
        print(f"Не удалось написать пользователю {uid}: {e}")


@router.message(Command("users"))
async def cmd_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    users = await db_manager.list_users()
    if not users:
        await message.answer("Пока никто не просил доступ.")
        return
    labels = {"active": "✅", "pending": "⏳", "blocked": "🚫"}
    builder = InlineKeyboardBuilder()
    text = "Кто пользуется ботом:\n\n"
    for uid, name, username, status in users:
        text += f"{labels.get(status, '')} {name}" + (f" @{username}" if username else "") + "\n"
        if status == "active":
            builder.button(text=f"🚫 Закрыть: {name[:20]}", callback_data=f"uno_{uid}")
        else:
            builder.button(text=f"✅ Пустить: {name[:20]}", callback_data=f"uok_{uid}")
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup())


# ---------- Старт, о себе, удаление данных ----------

def welcome_text(user_id: int, profile: dict) -> str:
    text = (
        "Привет! Я трекер дня и привычек.\n\n"
        "🎙 Присылай голосовые или пиши текстом, как проходит день:\n"
        "«С 14 до 18 работа, потом час сериала»\n"
        "«Завтра в 10 созвон», «Напоминай каждый день пить витамины»\n"
        "А ещё можно спрашивать: «что у меня сегодня?» — и поправлять: «нет, игра была до 11».\n\n"
        f"🕵️ Режим наблюдения: день {profile['days_passed']} из {profile['target_days']}. "
        "Я собираю статистику твоего ритма; разбор и предложение расписания — по /plan "
        "(точнее всего через 1–3 недели записей).\n\n"
    )
    text += ("📅 /calendar — подключить свой Google Календарь: дела будут появляться в нём "
             "(и на телефоне — Samsung, Redmi, iPhone).\n\n")
    if user_id != config.OWNER_ID:
        text += ("🔒 Записи видишь только ты в этом чате; хранятся они на сервере владельца бота. "
                 "Стереть всё — /deleteme.\n"
                 "Расскажи о себе одной фразой — так я лучше пойму голосовые: /about\n\n")
    text += (
        "Команды:\n"
        "/summary — итоги дня\n"
        "/todo — задачи\n"
        "/reminders — напоминания, /remind — новое\n"
        "/habits — привычки и серии\n"
        "/plan — разбор ритма и расписание (по воскресеньям приходит недельный)\n"
        "/limit 120 — предупредить, когда телефон за день больше 2 ч\n"
        "/about — о себе"
    )
    return text


@router.message(CommandStart())
async def cmd_start(message: types.Message):
    if not check_auth(message.from_user.id):
        await request_access(message)
        return
    profile = await db_manager.get_or_create_profile(message.from_user.id, _now_local().strftime("%Y-%m-%d"))
    await message.answer(welcome_text(message.from_user.id, profile))


@router.message(Command("about"))
async def cmd_about(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        about = parts[1].strip()[:500]
        await db_manager.set_setting(message.from_user.id, "about", about)
        await message.answer(f"Запомнил: {about}")
        return
    current = await db_manager.get_setting(message.from_user.id, "about", "")
    await message.answer(
        "Расскажи о себе одной фразой — так я лучше расслышу голосовые и пойму, что для тебя полезно:\n"
        "/about Учусь на дизайнера, хожу в зал, работаю бариста\n\n"
        f"Сейчас: {current or 'пусто'}"
    )


@router.message(Command("deleteme"))
async def cmd_deleteme(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, стереть всё", callback_data="delme_yes")
    builder.button(text="Отмена", callback_data="delme_no")
    builder.adjust(2)
    await message.answer("Стереть все твои записи, задачи, привычки и напоминания? Это нельзя отменить.",
                         reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("delme_"))
async def process_deleteme(callback: types.CallbackQuery):
    if callback.data == "delme_yes":
        google = await db_manager.get_google(callback.from_user.id)
        if google:
            await asyncio.to_thread(google_oauth.revoke, google["token"])
        await db_manager.delete_user_data(callback.from_user.id)
        await callback.message.edit_text("Готово: все твои данные стёрты. Можно начинать с чистого листа.")
    else:
        await callback.message.edit_text("Ок, ничего не трогаю.")
    await callback.answer()


# ---------- Google Календарь каждого пользователя ----------

OAUTH_STATE_TTL = 15 * 60  # ссылка на вход живёт 15 минут


class CalendarLost(Exception):
    """Человек отозвал доступ в своём Google — ключ больше не работает."""


@router.message(Command("calendar"))
async def cmd_calendar(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    uid = message.from_user.id
    if not google_oauth.enabled():
        await message.answer("Подключение Google Календаря ещё не настроено владельцем бота — скоро будет.")
        return

    builder = InlineKeyboardBuilder()
    if await db_manager.get_google(uid):
        builder.button(text="Отключить календарь", callback_data="gcal_off")
        await message.answer(
            f"✅ Google Календарь подключён: дела появляются в календаре «{google_oauth.CALENDAR_NAME}».\n"
            "На телефоне: в приложении календаря включи этот календарь в списке, если его не видно.",
            reply_markup=builder.as_markup())
        return

    nonce = secrets.token_urlsafe(16)
    await db_manager.set_setting(uid, "oauth_state", f"{nonce}|{int(time.time())}")
    builder.button(text="📅 Подключить Google Календарь", url=google_oauth.auth_url(f"{uid}.{nonce}"))
    await message.answer(
        "Дела будут появляться в твоём Google Календаре — в отдельном календаре "
        f"«{google_oauth.CALENDAR_NAME}». Остальной календарь бот не видит и не трогает.\n\n"
        "Нажми кнопку, выбери свой Google-аккаунт и разреши доступ.\n"
        "• Если Google напишет «приложение не проверено» — нажми «Дополнительно» → «Перейти». "
        "Это небольшой личный бот, у него нет проверки Google.\n"
        "• Если Google покажет ошибку «disallowed_useragent» — открой ссылку в обычном браузере (⋮ → «Открыть в браузере»).\n\n"
        "Ссылка действует 15 минут.",
        reply_markup=builder.as_markup())


@router.callback_query(F.data == "gcal_off")
async def process_calendar_off(callback: types.CallbackQuery):
    google = await db_manager.get_google(callback.from_user.id)
    if google:
        await asyncio.to_thread(google_oauth.revoke, google["token"])
        await db_manager.delete_google(callback.from_user.id)
    await callback.answer("Отключено")
    await callback.message.edit_text(
        f"Календарь отключён, бот больше туда не пишет. Сам календарь «{google_oauth.CALENDAR_NAME}» остался "
        "в твоём Google — можно удалить его вручную. Подключить снова — /calendar")


def _page(text: str, status: int = 200):
    html = ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Трекер дня</title>"
            "<body style='font-family:system-ui,sans-serif;max-width:480px;margin:15vh auto;padding:0 16px;"
            "text-align:center;background:#f6f7f9;color:#111'>"
            f"<p style='font-size:22px;line-height:1.4'>{text}</p>"
            "<p style='color:#777'>Эту страницу можно закрыть и вернуться в Telegram.</p></body></html>")
    return web.Response(text=html, content_type="text/html", status=status)


async def oauth_callback(request: web.Request):
    """Сюда Google возвращает человека после «Разрешить»: PUBLIC_URL/oauth/google?code=…&state=…"""
    bot = request.app["bot"]
    state, code, error = request.query.get("state", ""), request.query.get("code"), request.query.get("error")
    try:
        uid_str, nonce = state.split(".", 1)
        uid = int(uid_str)
    except ValueError:
        return _page("Ссылка неправильная. Нажми /calendar в боте ещё раз.", 400)

    saved = await db_manager.get_setting(uid, "oauth_state", "")
    saved_nonce, _, saved_at = (saved or "").partition("|")
    if not saved_nonce or saved_nonce != nonce or time.time() - float(saved_at or 0) > OAUTH_STATE_TTL:
        return _page("Ссылка устарела. Нажми /calendar в боте ещё раз.", 400)
    await db_manager.set_setting(uid, "oauth_state", "")  # ссылка одноразовая

    if error or not code:
        await _notify(bot, uid, "Календарь не подключён: доступ не разрешили. Попробовать снова — /calendar")
        return _page("Доступ не разрешён — календарь не подключён.")

    try:
        token = await asyncio.to_thread(google_oauth.exchange_code, code)
        old_calendar = await db_manager.get_setting(uid, "google_calendar_id")
        calendar_id = await asyncio.to_thread(google_oauth.ensure_calendar, token, old_calendar)
    except PermissionError:
        await _notify(bot, uid, "Календарь не подключён: при входе не отметили галочку доступа к календарю. "
                                "Нажми /calendar и оставь галочку.")
        return _page("Не хватает разрешения на календарь — попробуй ещё раз и оставь галочку.")
    except Exception as e:
        print(f"[oauth] {uid}: {e}")
        await _notify(bot, uid, "Не получилось подключить календарь — попробуй ещё раз через минуту: /calendar")
        return _page("Что-то пошло не так. Попробуй ещё раз из бота.", 500)

    await db_manager.save_google(uid, token, calendar_id)
    await db_manager.set_setting(uid, "google_calendar_id", calendar_id)
    await _notify(bot, uid,
                  f"✅ Google Календарь подключён! Новые дела будут появляться в календаре «{google_oauth.CALENDAR_NAME}».\n"
                  "На телефоне: если его не видно — включи этот календарь в настройках приложения календаря.")
    return _page("✅ Календарь подключён!")


async def _notify(bot, user_id: int, text: str):
    try:
        await bot.send_message(user_id, text)
    except Exception as e:
        print(f"[notify] {user_id}: {e}")


async def push_event(user_id: int, google: dict, title, start_time, end_time, utility, description):
    """Событие в личный календарь человека. id события или None; CalendarLost, если доступ отозван."""
    event = build_event(title, start_time, end_time, utility, description)
    try:
        return await asyncio.to_thread(google_oauth.insert_event, google["token"], google["calendar_id"], event)
    except RefreshError:
        await db_manager.delete_google(user_id)
        raise CalendarLost()
    except Exception as e:
        print(f"[calendar] {user_id}: {e}")
        return None


async def drop_events(user_id: int, event_ids):
    """Убрать из Google события удалённых дел."""
    google = await db_manager.get_google(user_id)
    for event_id in event_ids or []:
        if google and event_id:
            try:
                await asyncio.to_thread(google_oauth.delete_event, google["token"], google["calendar_id"], event_id)
            except Exception as e:
                print(f"[calendar] удаление {event_id}: {e}")


async def patch_event(user_id: int, row):
    """После поправки дела — поправить и событие в Google."""
    _, title, utility, _, _, start, end, event_id = row
    google = await db_manager.get_google(user_id)
    if not (google and event_id):
        return
    event = build_event(title, start, end, utility)
    fields = {k: event[k] for k in ("summary", "start", "end", "colorId")}
    try:
        await asyncio.to_thread(google_oauth.patch_event, google["token"], google["calendar_id"], event_id, fields)
    except Exception as e:
        print(f"[calendar] правка {event_id}: {e}")


# ---------- Стоп-сигнал на телефон ----------

DEFAULT_LIMIT_MIN = 120


async def phone_limit_note(user_id: int, today_date: str):
    """Текст предупреждения, если порог превышен и сегодня ещё не предупреждал; иначе None."""
    limit = int(await db_manager.get_setting(user_id, "phone_limit_min", DEFAULT_LIMIT_MIN))
    if limit <= 0 or await db_manager.get_setting(user_id, "phone_limit_warned") == today_date:
        return None
    spent = await db_manager.get_unproductive_minutes(user_id, today_date)
    if spent < limit:
        return None
    await db_manager.set_setting(user_id, "phone_limit_warned", today_date)
    return (f"🔴 Сегодня на телефон и залипание уже {_fmt_min(spent)} (твой порог — {_fmt_min(limit)}). "
            "Без оценок, просто отмечаю. Может, 10 минут пройтись или воды выпить?")


@router.message(Command("limit"))
async def cmd_limit(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
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

def _days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    return "дня" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "дней"


async def rhythm_report(user_id: int, days: int) -> str:
    """Разбор за последние `days` дней. Шапка честно говорит, за сколько дней на самом деле есть записи."""
    since = (_now_local() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    stats, days_with_data = await db_manager.get_activity_stats(user_id, since)
    if not stats:
        return "Пока нет записей — расскажи о паре дней, и я разберу твой ритм."
    about = await db_manager.get_setting(user_id, "about", "")
    text = await analyze_rhythm(stats, days_with_data, about, period=days)
    header = f"По записям за {days_with_data} {_days_word(days_with_data)}"
    if days_with_data < 3:
        header += (" — это первые наблюдения, а не выводы. Расписание появится, когда наберётся хотя бы 3 дня записей; "
                   "точнее всего — через 1–2 недели.")
    elif days_with_data < 7:
        header += " — пока прикидка, через неделю-две станет точнее."
    return f"{header}\n\n{text}"


@router.message(Command("plan"))
async def cmd_plan(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    status = await message.answer("🧭 Смотрю твои записи…")
    text = await rhythm_report(message.from_user.id, 21)
    await status.edit_text(f"🧭 Твой ритм\n\n{text}")


# ---------- Бэкап (только админам: в файле данные всех пользователей) ----------

BACKUP_CAPTION = ("💾 Копия базы трекера (данные всех пользователей — не пересылай). "
                  "Если хостинг потеряет данные, из этого файла всё восстановится.")


def backup_file():
    return BufferedInputFile(db_manager.backup_bytes(), filename=f"tracker_{_now_local().strftime('%Y-%m-%d')}.db")


@router.message(Command("backup"))
async def cmd_backup(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer_document(backup_file(), caption=BACKUP_CAPTION)


# ---------- Напоминания ----------

TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")

# Напоминание «после еды» хранится с time = "meal": планировщик по часам его не трогает
# ("meal" > "23:59" при сравнении строк), а срабатывает оно, когда в записи есть дело категории «Еда».
MEAL = "meal"
MEAL_RE = re.compile(r"после\s+(еды|едой|при[её]ма\s+пищи|завтрака|обеда|ужина|перекуса)|во\s+время\s+еды|когда\s+поем", re.I)
PENDING_REMINDER = {}  # user_id -> текст напоминания, для которого ждём выбор времени
SNOOZE_MIN = 30


def format_when(time: str, date: str | None) -> str:
    if time == MEAL:
        return "после еды"
    if not date:
        return f"каждый день в {time}"
    today = _now_local().date()
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return f"{date} в {time}"
    if d == today:
        return f"сегодня в {time}"
    if d == today + timedelta(days=1):
        return f"завтра в {time}"
    return f"{d.strftime('%d.%m')} в {time}"


def reminder_keyboard(reminder_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data=f"remdone_{reminder_id}")
    builder.button(text=f"⏰ Через {SNOOZE_MIN} мин", callback_data=f"remsnooze_{reminder_id}")
    builder.adjust(2)
    return builder.as_markup()


async def save_reminder(user_id: int, text: str, time_str: str, date: str | None = None):
    """Создаёт напоминание. Для разового с уже прошедшим временем возвращает None."""
    now = _now_local()
    now_time, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
    if date and (date < today or (date == today and time_str <= now_time)):
        return None
    return await db_manager.add_reminder(user_id, text, time_str, now_time, today, date)


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


@router.message(Command("remind"))
async def cmd_remind(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return

    parts = (message.text or "").split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        await message.answer(
            "Напиши, что напомнить и когда:\n"
            "/remind 10:00 Выпить витамины — каждый день\n"
            "/remind сегодня 15:00 Быть у мамы — один раз\n"
            "/remind завтра 9:30 Созвон\n"
            "/remind Пить таблетки после еды\n\n"
            "Или просто скажи боту голосом: «завтра в 10 созвон», «напоминай каждое утро пить воду»."
        )
        return

    # «сегодня» / «завтра» в начале — разовое
    date = None
    first, _, tail = rest.partition(" ")
    if first.lower() in ("сегодня", "завтра"):
        date = (_now_local() + timedelta(days=0 if first.lower() == "сегодня" else 1)).strftime("%Y-%m-%d")
        rest = tail.strip()
        first, _, tail = rest.partition(" ")

    m = TIME_RE.match(first)
    if m and tail.strip() and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        time_str = f"{int(m.group(1)):02d}:{m.group(2)}"
        text = tail.strip()
        text = text[0].upper() + text[1:]
        reminder_id = await save_reminder(message.from_user.id, text, time_str, date)
        if reminder_id is None:
            await message.answer(f"Сегодня {time_str} уже прошло — поставь на завтра: /remind завтра {time_str} {text}")
        else:
            await message.answer(f"⏰ Напомню {format_when(time_str, date)}: {text}")
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
        f"Когда напоминать «{text}» (каждый день)?\nИли напиши своё время: /remind 15:30 {text}",
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
    reminders = await db_manager.get_reminders(user_id, from_date=_now_local().strftime("%Y-%m-%d"))
    if not reminders:
        return "Напоминаний пока нет.\nДобавить: /remind 10:00 Выпить витамины", None

    builder = InlineKeyboardBuilder()
    text = "⏰ Напоминания:\n\n"
    for r_id, r_text, r_time, r_date in reminders:
        when = format_when(r_time, r_date)
        text += f"{when} — {r_text}\n"
        builder.button(text=f"🗑 {r_text[:28]}", callback_data=f"remdel_{r_id}")
    builder.adjust(1)
    return text, builder.as_markup()


@router.message(Command("reminders"))
async def cmd_reminders(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
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

    _, r_text, _, r_date = reminder
    await callback.answer("Отмечено!")
    if r_date:
        # Разовое — дело сделано, напоминание больше не нужно
        await db_manager.delete_reminder(reminder_id, callback.from_user.id)
        await callback.message.edit_text(f"✅ {r_text} — готово")
        return
    streak = await db_manager.update_habit(callback.from_user.id, r_text, _now_local().strftime("%Y-%m-%d"))
    await callback.message.edit_text(f"✅ {r_text} — готово\n🔥 Серия: {streak} дн. подряд")


# ---------- Списки ----------

@router.message(Command("patterns"))
async def cmd_patterns(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return

    profile = await db_manager.get_or_create_profile(message.from_user.id, _now_local().strftime("%Y-%m-%d"))
    patterns = await db_manager.get_top_patterns(message.from_user.id)

    header = f"🕵️ Наблюдение: день {profile['days_passed']} из {profile['target_days']}\n\n"
    if not patterns:
        await message.answer(header + "Пока недостаточно данных. Продолжай рассказывать о своих делах!")
        return

    text = header + "📊 Повторяющиеся дела:\n\n"
    for title, utility, count, last_date, avg_interval in patterns:
        icon = "🟢" if utility == "productive" else ("⚪" if utility == "routine" else "🔴")
        interval_txt = f"каждые ~{avg_interval} дн." if avg_interval > 0 else "пока 1 раз"
        text += f"{icon} {title}: {count} дн. ({interval_txt}, посл. {last_date})\n"
    await message.answer(text)


async def _todo_view(user_id: int):
    tasks = await db_manager.get_pending_tasks(user_id)
    if not tasks:
        return "🎉 Открытых задач нет!", None
    builder = InlineKeyboardBuilder()
    text = "📋 Задачи:\n\n"
    for task_id, title, _ in tasks:
        text += f"- {title}\n"
        builder.button(text=f"✅ {title[:28]}", callback_data=f"done_{task_id}")
    builder.adjust(1)
    return text, builder.as_markup()


@router.message(Command("todo"))
async def cmd_todo(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    text, markup = await _todo_view(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("done_"))
async def process_task_done(callback: types.CallbackQuery):
    await db_manager.mark_task_done(int(callback.data.split("_")[1]), callback.from_user.id)
    await callback.answer("Задача выполнена!")
    text, markup = await _todo_view(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)


@router.message(Command("habits"))
async def cmd_habits(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    habits = await db_manager.get_habits(message.from_user.id)
    if not habits:
        await message.answer("🌱 Пока нет привычек. Поставь ежедневное напоминание (/remind 10:00 Выпить витамины) — "
                             "кнопка «✅ Готово» будет считать серию.")
        return
    text = "🔥 Привычки:\n\n"
    for name, streak, last_completed in habits:
        text += f"- {name}: {streak} дн. подряд (посл. {last_completed})\n"
    await message.answer(text)


@router.message(Command("summary"))
async def cmd_summary(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    await message.answer(await day_report(message.from_user.id, _now_local().strftime("%Y-%m-%d")))


async def plans_for_day(user_id: int, date_str: str) -> list[str]:
    """Разовые напоминания на этот день: «14:30 — В 15:00 быть у мамы»."""
    return [f"{r_time} — {r_text}" for _, r_text, r_time, r_date in await db_manager.get_reminders(user_id, from_date=date_str)
            if r_date == date_str]


async def day_report(user_id: int, today_str: str) -> str:
    """Итог дня: баланс полезное/быт/телефон, категории, планы, привычки, открытые задачи."""
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

    plans = await plans_for_day(user_id, today_str)
    if plans:
        lines.append("\nПланы:\n" + "\n".join(plans))

    daily = [r for r in await db_manager.get_reminders(user_id) if not r[3]]
    if daily:
        done_today = {name for name, _, last in await db_manager.get_habits(user_id) if last == today_str}
        marks = [("✅ " if text in done_today else "▫️ ") + text for _, text, _, _ in daily]
        lines.append("\nПривычки:\n" + "\n".join(marks))

    tasks = await db_manager.get_pending_tasks(user_id)
    if tasks:
        shown = "\n".join(f"- {title}" for _, title, _ in tasks[:5])
        more = f"\n…и ещё {len(tasks) - 5}" if len(tasks) > 5 else ""
        lines.append(f"\nОткрытые задачи ({len(tasks)}):\n{shown}{more}")

    return "\n".join(lines)


# ---------- Записи: голос и текст ----------

@router.message(F.voice | F.audio)
async def handle_voice(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return

    status_msg = await message.reply("Слушаю и расшифровываю...")
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    file_info = await message.bot.get_file(file_id)
    temp_path = f"temp_{file_id}.ogg"
    await message.bot.download_file(file_info.file_path, destination=temp_path)

    try:
        about = await db_manager.get_setting(message.from_user.id, "about", "")
        transcript = await transcribe_audio(temp_path, about)
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
        await deny(message)
        return
    status_msg = await message.reply("Разбираю...")
    await process_entry(message, status_msg, message.text.strip())


async def build_context(user_id: int, now: datetime) -> str:
    """Что модель должна знать, чтобы отвечать на вопросы и вносить поправки: день, задачи, напоминания,
    предыдущее сообщение. Номера (#) — настоящие id в базе."""
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    lines = []

    acts = await db_manager.get_activities_for_day(user_id, today)
    if acts:
        lines.append("Записано сегодня:")
        for a_id, title, utility, category, duration, start, end in acts[-40:]:
            lines.append(f"- дело #{a_id}: {_hm(start)}–{_hm(end)} {title} ({utility}, {category}, {duration} мин)")

    acts_y = await db_manager.get_activities_for_day(user_id, yesterday)
    if acts_y:
        lines.append("Записано вчера:")
        lines.extend(f"- {_hm(start)}–{_hm(end)} {title} ({duration} мин)" for _, title, _, _, duration, start, end in acts_y[-25:])

    tasks = await db_manager.get_pending_tasks(user_id)
    if tasks:
        lines.append("Открытые задачи:")
        lines.extend(f"- задача #{t_id}: {title}" for t_id, title, _ in tasks[:30])

    reminders = await db_manager.get_reminders(user_id, from_date=today)
    if reminders:
        lines.append("Напоминания:")
        lines.extend(f"- напоминание #{r_id}: {format_when(r_time, r_date)} — {r_text}" for r_id, r_text, r_time, r_date in reminders)

    done_today = [name for name, _, last in await db_manager.get_habits(user_id) if last == today]
    if done_today:
        lines.append("Привычки, отмеченные сегодня: " + ", ".join(done_today))

    last = await db_manager.get_last_entry(user_id)
    if last:
        _, text, data, created_at = last
        try:
            ago = int((now.replace(tzinfo=None) - datetime.fromisoformat(created_at)).total_seconds() // 60)
        except (TypeError, ValueError):
            ago = None
        if ago is not None and ago <= 6 * 60:
            lines.append(f"\nПРЕДЫДУЩЕЕ СООБЩЕНИЕ ({ago} мин назад): «{text}»")
            if data.get("summary"):
                lines.append("Что бот из него создал: " + "; ".join(data["summary"]))

    return "\n".join(lines)


async def apply_edits(user_id: int, edits: list, now: datetime) -> list[str]:
    """Поправки от модели → изменения в базе. Возвращает строки для ответа."""
    out = []
    now_time, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
    for e in edits:
        op, item_id = e["op"], e["id"]
        try:
            if op.startswith("reminder_"):
                reminder = await db_manager.get_reminder(item_id, user_id)
                if not reminder:
                    continue
                text = reminder[1]
                if op == "reminder_delete":
                    await db_manager.delete_reminder(item_id, user_id)
                    out.append(f"🗑 Напоминание удалено: {text}")
                    continue
                if op == "reminder_once":
                    updated = await db_manager.update_reminder(item_id, user_id, now_time, today, date=e.get("date") or today)
                elif op == "reminder_daily":
                    updated = await db_manager.update_reminder(item_id, user_id, now_time, today, date=None)
                else:
                    updated = await db_manager.update_reminder(item_id, user_id, now_time, today, time=e["time"])
                if updated:
                    once = " (один раз)" if updated[3] else ""
                    out.append(f"✏️ Напоминание «{text}»: {format_when(updated[2], updated[3])}{once}")

            elif op == "activity_update":
                fields = {k: e.get(k) for k in ("title", "start_time", "end_time", "utility", "category")}
                if fields["category"] and fields["category"] not in CATEGORIES:
                    fields["category"] = None
                if fields["utility"] not in ("productive", "routine", "unproductive"):
                    fields["utility"] = None
                row = await db_manager.update_activity(item_id, user_id, **fields)
                if row:
                    _, title, _, _, duration, start, end, _ = row
                    await patch_event(user_id, row)
                    out.append(f"✏️ Исправлено: {_hm(start)}–{_hm(end)} {title} · {_fmt_min(duration)}")

            elif op == "activity_delete":
                event_ids = await db_manager.delete_items(user_id, activity_ids=[item_id])
                if event_ids:
                    await drop_events(user_id, event_ids)
                    out.append("🗑 Дело удалено")

            elif op == "task_delete":
                task = await db_manager.get_task(item_id, user_id)
                if task:
                    await db_manager.delete_items(user_id, task_ids=[item_id])
                    out.append(f"🗑 Задача удалена: {task[1]}")
        except Exception as ex:
            print(f"[apply_edits] {e}: {ex}")
    return out


def _join(lines):
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


UTILITY_LABEL = {"productive": "🟢 полезное", "routine": "⚪ быт", "unproductive": "🔴 прокрастинация"}


async def process_entry(message: types.Message, status_msg: types.Message, transcript: str):
    """Сообщение пользователя: рассказ о дне, вопрос, поправка или просто реплика."""
    user_id = message.from_user.id
    try:
        now = _now_local()
        today_date = now.strftime("%Y-%m-%d")

        if not is_admin(user_id) and await db_manager.bump_usage(user_id, today_date) > config.DAILY_ENTRY_LIMIT:
            await status_msg.edit_text(
                f"На сегодня лимит сообщений исчерпан ({config.DAILY_ENTRY_LIMIT}): бот бесплатный и делит квоту на всех. "
                "Завтра снова можно — а /summary, /todo и другие команды работают и сейчас.")
            return

        profile = await db_manager.get_or_create_profile(user_id, today_date)
        await status_msg.edit_text(f"«{transcript}»\n\nРазбираю…")

        about = await db_manager.get_setting(user_id, "about", "")
        known_titles = [row[0] for row in await db_manager.get_top_patterns(user_id)][:60]
        context = await build_context(user_id, now)
        analysis = await parse_voice_text(transcript, known_titles, about, context)

        lines = [f"«{transcript}»\n"]
        created = {"a": [], "t": [], "r": [], "summary": []}

        # 1. Поправки (могут прийти и вместе с рассказом)
        edit_lines = await apply_edits(user_id, analysis["edits"], now)
        lines.extend(edit_lines)

        activities = analysis["activities"]
        has_new = bool(activities or analysis["tasks_to_add"] or analysis["reminders"] or analysis["habit_identified"])

        # 2. Вопрос / реплика / только поправка — просто ответ
        if not has_new:
            # После поправки строки «✏️ …» уже всё говорят — ответ модели был бы повтором
            if analysis["answer"] and not (edit_lines and analysis["intent"] == "correction"):
                lines.append(("\n" if edit_lines else "") + analysis["answer"])
            elif not edit_lines:
                lines.append("Не нашёл здесь дел, задач или напоминаний. Расскажи, что было, "
                             "или спроси, например: «что у меня сегодня?»")
            await status_msg.edit_text(_join(lines))
            return

        # 3. Дела. Календарь: свой Google человека (/calendar) — сразу; у владельца без /calendar —
        # старый путь (token.json) после 3 недель наблюдения
        is_owner = user_id == config.OWNER_ID
        google = await db_manager.get_google(user_id) if google_oauth.enabled() else None
        legacy_calendar = not google and is_owner and profile["phase"] == "coaching"
        calendar_ok, calendar_lost = True, False
        for act in activities:
            title = act.get("title") or "Дело"
            utility = act.get("utility") if act.get("utility") in UTILITY_LABEL else "routine"
            category = act.get("category", "Другое")
            start_time, end_time = act.get("start_time"), act.get("end_time")
            try:
                duration = int(act.get("duration_minutes") or 30)
            except (TypeError, ValueError):
                duration = 30

            event_id = None
            description = f"Категория: {category} | Из трекера дня"
            if google and not calendar_lost:
                try:
                    event_id = await push_event(user_id, google, title, start_time, end_time, utility, description)
                    calendar_ok = calendar_ok and bool(event_id)
                except CalendarLost:
                    calendar_lost = True
            elif legacy_calendar:
                link = await asyncio.to_thread(add_event, summary=title, start_time=start_time, end_time=end_time,
                                               utility=utility, description=description)
                calendar_ok = calendar_ok and bool(link)

            activity_id = await db_manager.add_activity(user_id=user_id, title=title, category=category, utility=utility,
                                                        duration_min=duration, start_time=start_time, end_time=end_time,
                                                        calendar_event_id=event_id)
            await db_manager.record_pattern(user_id=user_id, title=title, utility=utility, date_str=today_date)

            time_label = f"{_hm(start_time)}–{_hm(end_time)} " if _hm(start_time) and _hm(end_time) else ""
            lines.append(f"⏱ {time_label}{title} · {_fmt_min(duration)} · {UTILITY_LABEL[utility]}")
            created["a"].append(activity_id)
            created["summary"].append(f"дело #{activity_id}: {time_label}{title}")

        if activities:
            if calendar_lost:
                lines.append("⚠️ Доступ к Google Календарю отозван — подключи заново: /calendar")
            elif google or legacy_calendar:
                lines.append("📅 Добавлено в Google Календарь" if calendar_ok else
                             "📥 Сохранено в боте (Google Календарь сейчас не ответил)")

        # 4. Задачи
        if analysis["tasks_to_add"]:
            lines.append("\n📋 В задачи:")
            for t in analysis["tasks_to_add"]:
                task_id = await db_manager.add_task(user_id, t)
                created["t"].append(task_id)
                created["summary"].append(f"задача #{task_id}: {t}")
                lines.append(f"- {t}")

        # 5. Привычка
        habit = analysis["habit_identified"]
        if habit:
            streak = await db_manager.update_habit(user_id, habit, today_date)
            lines.append(f"\n🔥 Привычка: {habit} (серия: {streak} дн.)")

        # 6. Напоминания
        for rem in analysis["reminders"]:
            text, time_str, date = rem["text"], rem["time"], rem["date"]
            if not time_str:
                if MEAL_RE.search(text):
                    time_str, date = MEAL, None
                else:
                    example = f"/remind {'сегодня ' if date == today_date else ('завтра ' if date else '')}10:00 {text}"
                    lines.append(f"\n⏰ Во сколько напомнить «{text}»? Напиши, например:\n{example}")
                    continue
            reminder_id = await save_reminder(user_id, text, time_str, date)
            if reminder_id is None:
                lines.append(f"\n⏰ «{text}» — {time_str} уже прошло, не ставлю.")
                continue
            when = format_when(time_str, date)
            lines.append(f"\n🍽 Буду напоминать после еды: {text}" if time_str == MEAL else f"\n⏰ Напомню {when}: {text}")
            created["r"].append(reminder_id)
            created["summary"].append(f"напоминание #{reminder_id}: {when} — {text}")

        if analysis["answer"]:
            lines.append(f"\n{analysis['answer']}")
        if analysis["encouragement"]:
            lines.append(f"\n{analysis['encouragement']}")

        # Мягкий стоп-сигнал: один раз в день, когда телефон/игры перевалили за порог
        if any(a.get("utility") == "unproductive" for a in activities):
            note = await phone_limit_note(user_id, today_date)
            if note:
                lines.append(f"\n{note}")

        builder = InlineKeyboardBuilder()

        # Напоминания «после еды»: если в записи была еда
        if any(a.get("category") == "Еда" for a in activities):
            for r_id, r_text, r_time, _ in await db_manager.get_reminders(user_id):
                if r_time == MEAL:
                    lines.append(f"\n🍽 Не забудь: {r_text}")
                    builder.button(text=f"✅ {r_text[:30]}", callback_data=f"remdone_{r_id}")

        if created["a"] or created["t"] or created["r"]:
            entry_id = await db_manager.add_entry(user_id, transcript, created, now.replace(tzinfo=None).isoformat())
            builder.button(text="↩️ Отменить запись", callback_data=f"undo2_{entry_id}")
        builder.adjust(1)
        markup = builder.as_markup() if list(builder.buttons) else None
        await status_msg.edit_text(_join(lines), reply_markup=markup)

    except Exception as e:
        import traceback
        traceback.print_exc()
        await status_msg.edit_text(f"Ошибка обработки: {str(e)[:200]}")


@router.callback_query(F.data.startswith("undo2_"))
async def process_undo(callback: types.CallbackQuery):
    entry = await db_manager.get_entry(int(callback.data.split("_")[1]), callback.from_user.id)
    if not entry:
        await callback.answer("Эту запись уже не отменить")
        return
    data = entry[2]
    event_ids = await db_manager.delete_items(callback.from_user.id, data.get("a", []), data.get("t", []), data.get("r", []))
    await drop_events(callback.from_user.id, event_ids)
    await callback.answer("Запись отменена")
    note = "\n\n↩️ Отменено — дела, задачи и напоминания из этой записи удалены."
    if any(e is None for e in event_ids) and callback.from_user.id == config.OWNER_ID \
            and await db_manager._phase(callback.from_user.id) == "coaching" and not await db_manager.get_google(callback.from_user.id):
        note += "\nСобытия в Google Календаре удали вручную."
    await callback.message.edit_text((callback.message.text or "") + note)


@router.callback_query(F.data.startswith("undo_"))
async def process_undo_legacy(callback: types.CallbackQuery):
    """Кнопки «Отменить» из сообщений до обновления (диапазоны id)."""
    _, acts, tasks = callback.data.split("_")
    a_from, a_to = map(int, acts.split("-"))
    t_from, t_to = map(int, tasks.split("-"))
    had_calendar = await db_manager.undo_entry(callback.from_user.id, a_from, a_to, t_from, t_to)
    await callback.answer("Запись отменена")
    note = "\n\n↩️ Отменено — дела и задачи из этой записи удалены."
    if had_calendar:
        note += "\nСобытия в Google Календаре удали вручную."
    await callback.message.edit_text((callback.message.text or "") + note)
