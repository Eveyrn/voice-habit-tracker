import os
import re
import html
import json
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
import groq
from services import timeline
from services.ai_parser import parse_voice_text, analyze_rhythm, condense_about, clean_category, CATEGORIES, PRIMARY_MODEL, \
    REGULAR_RE, ABOUT_MAX, WEEKDAYS_SHORT, QUOTA as ai_quota, LIMIT_HIT as ai_limit
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
    ("feedback", "Где бот ошибся (👎 от пользователей)"),
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


def _tidy_activity(title, category, utility):
    act = {"title": title or "Дело", "category": clean_category(category),
           "utility": utility if utility in UTILITY_LABEL else "routine"}
    timeline.normalize_title(act)
    return act["title"], act["category"], act["utility"]


async def tidy_history():
    """При запуске: старые «Играть», «Завтрак (гречка)», категории «unproductive» — к одному виду."""
    changed = await db_manager.tidy_history(_tidy_activity)
    if changed:
        print(f"[tidy] приведено к одному виду записей: {changed}")
    # Длинное «о себе», сохранённое до сжатия, — сжимаем один раз (оно уходит в каждый запрос к модели)
    for uid, about in await db_manager.settings_with_key("about"):
        if about and len(about) > ABOUT_MAX + 100:
            await save_about(uid, about)
            print(f"[tidy] сжато «о себе» пользователя {uid}")


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
            await db_manager.get_or_create_profile(uid, _now_local().strftime("%Y-%m-%d"))
            await start_onboarding(callback.bot, uid, "🎉 Доступ открыт! Давай за минуту настроим бота.\n\n")
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
        "🧭 Из записей я собираю статистику твоего ритма: разбор и предложение расписания — по /plan. "
        "Первые выводы — после 3 дней записей, точнее всего — через 1–2 недели.\n\n"
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
    uid = message.from_user.id
    if not check_auth(uid):
        await request_access(message)
        return
    profile = await db_manager.get_or_create_profile(uid, _now_local().strftime("%Y-%m-%d"))
    # Пущенные до появления знакомства и ещё ничего не рассказавшие о себе — проходят его сейчас
    if not is_admin(uid) and not await db_manager.get_setting(uid, "onboarding") \
            and not await db_manager.get_setting(uid, "about") and not await db_manager.get_last_entry(uid):
        await start_onboarding(message.bot, uid, "Привет! Давай за минуту настроим бота.\n\n")
        return
    await message.answer(welcome_text(uid, profile))


# ---------- Знакомство: 3 шага после «Пустить» ----------

ONB_STEP1 = ("Шаг 1 из 3. Расскажи о себе одной фразой — так я лучше расслышу голосовые и пойму, что для тебя "
             "полезно. Например: «учусь на дизайнера, хожу в зал, работаю бариста».\n\n"
             "Можно текстом или голосом.")
ONB_STEP2 = ("Шаг 2 из 3. Теперь запиши кусочек дня — голосовым или текстом. Например:\n"
             "«с 10 до 13 учёба, потом час в телефоне»\n"
             "Время можно не называть — прикину сам и помечу «≈».")
ONB_STEP3 = ("Шаг 3 из 3 ✅ Получилось! Что ещё умею:\n"
             "• напоминания — «завтра в 10 созвон», «напоминай каждый день пить витамины»;\n"
             "• вопросы — «что у меня сегодня?»;\n"
             "• поправки — «нет, это было с 9 до 10»;\n"
             "• итоги дня — /summary, разбор ритма — /plan (через пару дней записей).\n\n"
             "📅 Хочешь, чтобы дела появлялись в твоём Google Календаре — /calendar.\n"
             "Если я ошибся — жми «👎 Бот ошибся» под ответом. Все команды — /start.")


async def start_onboarding(bot, user_id: int, intro: str):
    await db_manager.set_setting(user_id, "onboarding", "about")
    builder = InlineKeyboardBuilder()
    builder.button(text="Пропустить", callback_data="onb_skip")
    await _notify_markup(bot, user_id, intro + ONB_STEP1, builder.as_markup())


async def _notify_markup(bot, user_id: int, text: str, markup):
    try:
        await bot.send_message(user_id, text, reply_markup=markup)
    except Exception as e:
        print(f"[notify] {user_id}: {e}")


@router.callback_query(F.data == "onb_skip")
async def process_onboarding_skip(callback: types.CallbackQuery):
    if await db_manager.get_setting(callback.from_user.id, "onboarding") == "about":
        await db_manager.set_setting(callback.from_user.id, "onboarding", "first_entry")
    await callback.answer()
    await callback.message.edit_text(ONB_STEP2)


async def onboarding_about(message: types.Message, about_text: str) -> bool:
    """Шаг 1: если ждём «о себе» — сохраняем и переходим к шагу 2. True — сообщение обработано."""
    if await db_manager.get_setting(message.from_user.id, "onboarding") != "about":
        return False
    await save_about(message.from_user.id, about_text)
    await db_manager.set_setting(message.from_user.id, "onboarding", "first_entry")
    await message.answer(f"Запомнил ✅\n\n{ONB_STEP2}")
    return True


async def save_about(user_id: int, text: str) -> str:
    about = await condense_about(text)
    await db_manager.set_setting(user_id, "about", about)
    return about


ABOUT_WAIT_MIN = 15  # после /about без текста следующее сообщение (голос или текст) — это «о себе»


async def pending_about(message: types.Message, about_text: str) -> bool:
    """Ждём «о себе» после /about — сохраняем. True — сообщение обработано."""
    waiting = await db_manager.get_setting(message.from_user.id, "await_about")
    if not waiting:
        return False
    await db_manager.set_setting(message.from_user.id, "await_about", "")
    try:
        if (_now_local().replace(tzinfo=None) - datetime.fromisoformat(waiting)) > timedelta(minutes=ABOUT_WAIT_MIN):
            return False
    except ValueError:
        return False
    about = await save_about(message.from_user.id, about_text)
    await message.answer(f"Запомнил о тебе: {about}\n\nПоменять — снова /about.")
    return True


@router.message(Command("about"))
async def cmd_about(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        about = await save_about(message.from_user.id, parts[1])
        await message.answer(f"Запомнил о тебе: {about}")
        return
    current = await db_manager.get_setting(message.from_user.id, "about", "")
    await db_manager.set_setting(message.from_user.id, "await_about", _now_local().replace(tzinfo=None).isoformat())
    await message.answer(
        "Расскажи о себе следующим сообщением — голосовым или текстом: чем занимаешься, что для тебя полезно, "
        "к чему стремишься. Так я лучше расслышу голосовые и правильнее отмечу полезное.\n\n"
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
        builder.button(text=f"🗑 {_short(r_text)}", callback_data=f"remdel_{r_id}")
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
    if r_date:
        # Разовое — дело сделано, напоминание больше не нужно
        await db_manager.delete_reminder(reminder_id, callback.from_user.id)
        done = f"✅ {r_text} — готово"
    else:
        streak = await db_manager.update_habit(callback.from_user.id, r_text, _now_local().strftime("%Y-%m-%d"))
        done = f"✅ {r_text} — готово\n🔥 Серия: {streak} дн. подряд"
    # Кнопка под ответом на запись («🍽 Не забудь: …») — убираем только её, сам ответ и «Отменить» оставляем
    others = [[b for b in row if b.callback_data != callback.data] for row in (callback.message.reply_markup.inline_keyboard
                                                                               if callback.message.reply_markup else [])]
    others = [row for row in others if row]
    if any(b.callback_data.startswith(("undo2_", "ed_", "fb")) for row in others for b in row):
        await callback.answer(done.replace("\n", " · "))
        await callback.message.edit_reply_markup(reply_markup=types.InlineKeyboardMarkup(inline_keyboard=others))
        return
    await callback.answer("Отмечено!")
    await callback.message.edit_text(done)


# ---------- Списки ----------

@router.message(Command("patterns"))
async def cmd_patterns(message: types.Message):
    if not check_auth(message.from_user.id):
        await deny(message)
        return

    profile = await db_manager.get_or_create_profile(message.from_user.id, _now_local().strftime("%Y-%m-%d"))
    patterns = await db_manager.get_top_patterns(message.from_user.id)

    header = f"🗓 Ты с трекером {profile['days_passed']} {_days_word(profile['days_passed'])}\n\n"
    if not patterns:
        await message.answer(header + "Пока недостаточно данных. Продолжай рассказывать о своих делах!")
        return

    repeating = [p for p in patterns if p[2] >= 2]
    once = [p[0] for p in patterns if p[2] < 2]
    text = header
    if repeating:
        text += "📊 Повторяющиеся дела:\n\n"
        for title, utility, count, last_date, avg_interval in repeating:
            icon = "🟢" if utility == "productive" else ("⚪" if utility == "routine" else "🔴")
            every = "каждый день" if (avg_interval or 0) <= 1.2 else f"примерно раз в {round(avg_interval)} {_days_word(round(avg_interval))}"
            last = datetime.strptime(last_date, "%Y-%m-%d").strftime("%d.%m") if last_date else "—"
            text += f"{icon} {title}: {count} {_days_word(count)}, {every} (последний раз {last})\n"
    else:
        text += "📊 Повторяющихся дел пока нет — нужно, чтобы одно и то же дело было хотя бы в 2 разных днях.\n"
    if once:
        text += f"\nПока по разу: {', '.join(once[:20])}" + (f" и ещё {len(once) - 20}" if len(once) > 20 else "")
    await message.answer(text)


def _due_label(due: str | None, today: str) -> str:
    """« (сегодня)», « (завтра)», « (до 24.10)», « (просрочено, 18.10)» или пусто."""
    if not due:
        return ""
    try:
        d, t = datetime.strptime(due, "%Y-%m-%d").date(), datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return ""
    if d < t:
        return f" (просрочено, {d.strftime('%d.%m')})"
    if d == t:
        return " (сегодня)"
    if d == t + timedelta(days=1):
        return " (завтра)"
    return f" (до {d.strftime('%d.%m')})"


TODO_GROUPS = [("⚠️ Просрочено", lambda d, t: d and d < t), ("Сегодня", lambda d, t: d == t),
               ("Завтра", lambda d, t: d and d == _plus_day(t)), ("Позже", lambda d, t: d and d > _plus_day(t)),
               ("Без срока", lambda d, t: not d)]


def _plus_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


async def _todo_view(user_id: int):
    tasks = await db_manager.get_pending_tasks(user_id)
    if not tasks:
        return "🎉 Открытых задач нет!", None
    today = _now_local().strftime("%Y-%m-%d")
    builder = InlineKeyboardBuilder()
    text = "📋 Задачи\n"
    for group, belongs in TODO_GROUPS:
        items = [(t_id, title, due) for t_id, title, due in tasks if belongs(due, today)]
        if not items:
            continue
        text += f"\n{group}:\n"
        for task_id, title, due in sorted(items, key=lambda x: x[2] or ""):
            label = f" ({datetime.strptime(due, '%Y-%m-%d').strftime('%d.%m')})" if due and group in ("Позже", "⚠️ Просрочено") else ""
            text += f"- {title}{label}\n"
            builder.button(text=f"✅ {_short(title)}", callback_data=f"done_{task_id}")
    builder.adjust(1)
    return text, builder.as_markup()


async def tasks_for_morning(user_id: int, today: str) -> list[str]:
    """Задачи на сегодня и просроченные — для утреннего сообщения."""
    return [f"{title}{_due_label(due, today) if due != today else ''}"
            for _, title, due in await db_manager.get_pending_tasks(user_id) if due and due <= today]


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


def _covered_minutes(acts, day_str: str) -> int:
    """Сколько минут дня покрыто делами — пересечения считаются один раз."""
    day_start = datetime.strptime(day_str, "%Y-%m-%d")
    day_end = day_start + timedelta(days=1)
    intervals = []
    for *_, duration, start, end in acts:
        s = _parse_dt(start)
        e = _parse_dt(end) or (s + timedelta(minutes=int(duration or 0)) if s else None)
        if s and e and e > s:
            intervals.append((max(s, day_start), min(e, day_end)))
    covered, cursor = 0, day_start
    for s, e in sorted(intervals):
        s = max(s, cursor)
        if e > s:
            covered += (e - s).total_seconds() / 60
            cursor = e
    return int(covered)


async def day_report(user_id: int, today_str: str) -> str:
    """Итог дня: баланс полезное/быт/телефон, категории, планы, привычки, открытые задачи."""
    stats = await db_manager.get_today_summary(user_id, today_str)
    lines = []

    total = sum(int(d or 0) for _, _, d in stats)
    if total:
        # Дела могут накладываться (одно и то же записали дважды, «ела и смотрела сериал») — время дня
        # считаем один раз, иначе выходило «учтено 26 ч» за сутки
        covered = _covered_minutes(await db_manager.get_activities_for_day(user_id, today_str), today_str) or total
        lines.append(f"📊 Итоги дня — учтено {_fmt_min(covered)}\n")
        by_utility = {}
        for utility, _, duration in stats:
            utility = utility if utility in UTILITY_LABEL else "routine"
            by_utility[utility] = by_utility.get(utility, 0) + int(duration or 0)
        for key, label in (("productive", "🟢 Полезное"), ("routine", "⚪ Быт"), ("unproductive", "🔴 Прокрастинация")):
            if by_utility.get(key):
                lines.append(f"{label}: {_fmt_min(by_utility[key])} · {round(by_utility[key] * 100 / total)}%")
        if total - covered >= 15:
            lines.append(f"(часть дел шла одновременно — {_fmt_min(total - covered)} посчитаны дважды)")

        by_category = {}
        for _, category, duration in stats:
            category = clean_category(category)
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
        shown = "\n".join(f"- {title}{_due_label(due, today_str)}" for _, title, due in tasks[:5])
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
        try:
            transcript = await transcribe_audio(temp_path, about)
        except groq.RateLimitError as e:
            day = "per day" in str(e).lower()
            await status_msg.edit_text(
                "⏳ Закончился бесплатный лимит распознавания голоса на сегодня — до завтра пиши текстом, пожалуйста."
                if day else "⏳ Распознаванию голоса нужна передышка (лимит в минуту). Пришли ещё раз через пару минут "
                            "или напиши текстом.")
            await quota_watch(message.bot, "day" if day else None)
            return
        if not transcript.strip():
            await status_msg.edit_text("Не удалось распознать слова в аудиосообщении.")
            return
        if await onboarding_about(message, transcript) or await pending_about(message, transcript):
            await status_msg.edit_text(f"«{transcript}»")
            return
        await process_entry(message, status_msg, transcript, voice=True)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: types.Message):
    """То же, что голосовое, только без расшифровки."""
    if not check_auth(message.from_user.id):
        await deny(message)
        return
    # Ответ (reply) на «что было не так?» — это комментарий к 👎, а не запись дня
    reply_to = message.reply_to_message
    if reply_to and reply_to.from_user and reply_to.from_user.is_bot:
        feedback_id = await db_manager.add_feedback_comment(message.from_user.id, reply_to.message_id, message.text.strip())
        if feedback_id:
            await message.answer("Записал, спасибо! 🙏 Это поможет починить бота.")
            await _notify_admins(message.bot, message.from_user,
                                 f"💬 Комментарий к ошибке #{feedback_id}: {message.text.strip()}")
            return
    if await onboarding_about(message, message.text) or await pending_about(message, message.text):
        return
    status_msg = await message.reply("Разбираю...")
    await process_entry(message, status_msg, message.text.strip())


# Когда модели нужен ПОЛНЫЙ контекст (номера дел и задач, прошлое сообщение, правила поправок).
# Обычное «час работала» обходится без него — так одно сообщение стоит примерно вдвое меньше токенов.
CORRECTION_RE = re.compile(r"^\s*(нет|не,|неа|не так|неправ|исправ|поправ|удали|убери|отмени|измени|поменяй|перенеси|"
                           r"это было|было не|не каждый|только сегодня|только завтра)", re.I)
QUESTION_RE = re.compile(r"\?|^\s*(что|сколько|когда|во сколько|какие|какой|какая|где|покажи|расскажи|есть ли|а что)\b", re.I)
REFERENCE_RE = re.compile(r"\b(это|этого|его|её|ее|их|то напоминание|ту задачу|то дело|последн)", re.I)


async def needs_full_context(user_id: int, text: str, now: datetime) -> bool:
    if CORRECTION_RE.search(text) or QUESTION_RE.search(text):
        return True
    if REFERENCE_RE.search(text):
        last = await db_manager.get_last_entry(user_id)
        if last:
            try:
                return (now.replace(tzinfo=None) - datetime.fromisoformat(last[3])).total_seconds() <= 30 * 60
            except (TypeError, ValueError):
                return False
    return False


async def build_context(user_id: int, now: datetime, full: bool = True, text: str = "") -> str:
    """Что модель должна знать о пользователе. Номера (#) — настоящие id в базе.
    full=False — только напоминания (для привычек и «встретилась с мамой» ↔ «В 15:00 быть у мамы»).
    full=True — ещё дела за сегодня, задачи, прошлое сообщение; вчера — если в тексте «вчера»,
    итоги недели — если спрашивают про период."""
    today = now.strftime("%Y-%m-%d")
    lines = []

    reminders = await db_manager.get_reminders(user_id, from_date=today)
    if reminders:
        lines.append("Напоминания:")
        lines.extend(f"- напоминание #{r_id}: {format_when(r_time, r_date)} — {r_text}" for r_id, r_text, r_time, r_date in reminders)
    if not full:
        return "\n".join(lines)

    acts = await db_manager.get_activities_for_day(user_id, today)
    if acts:
        lines.append("Записано сегодня:")
        for a_id, title, utility, category, duration, start, end in acts[-40:]:
            lines.append(f"- дело #{a_id}: {_hm(start)}–{_hm(end)} {title} ({utility}, {category}, {duration} мин)")

    if re.search(r"вчера", text, re.I):
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        acts_y = await db_manager.get_activities_for_day(user_id, yesterday)
        lines.append("Записано вчера:" if acts_y else "Вчера записей нет.")
        lines.extend(f"- {_hm(start)}–{_hm(end)} {title} ({duration} мин)" for _, title, _, _, duration, start, end in acts_y[-25:])

    if PERIOD_RE.search(text):
        # Итоги по дням за неделю — чтобы модель считала по фактам и видела пустые дни
        week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        totals = {}
        for day, category, minutes in await db_manager.get_daily_totals(user_id, week_start):
            totals.setdefault(day, {})[category] = int(minutes or 0)
        lines.append(f"Итоги по категориям за последние 7 дней (с {week_start}); дней с записями: {len(totals)}:")
        for day in sorted(totals):
            lines.append(f"- {day}: " + ", ".join(f"{c} {m} мин" for c, m in totals[day].items()))
        if not totals:
            lines.append("- записей нет")

    tasks = await db_manager.get_pending_tasks(user_id)
    if tasks:
        lines.append("Открытые задачи:")
        lines.extend(f"- задача #{t_id}: {title}{_due_label(due, today)}" for t_id, title, due in tasks[:30])

    done_today = [name for name, _, last in await db_manager.get_habits(user_id) if last == today]
    if done_today:
        lines.append("Привычки, отмеченные сегодня: " + ", ".join(done_today))

    last = await db_manager.get_last_entry(user_id)
    if last:
        _, last_text, data, created_at = last
        try:
            ago = int((now.replace(tzinfo=None) - datetime.fromisoformat(created_at)).total_seconds() // 60)
        except (TypeError, ValueError):
            ago = None
        if ago is not None and ago <= 6 * 60:
            lines.append(f"ПРЕДЫДУЩЕЕ СООБЩЕНИЕ ({ago} мин назад): «{last_text}»")
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
                current = await db_manager.get_activity(item_id, user_id)
                if not current:
                    continue
                fields = {k: e.get(k) for k in ("title", "utility", "category")}
                if fields["category"] and fields["category"] not in CATEGORIES:
                    fields["category"] = None
                if fields["utility"] not in ("productive", "routine", "unproductive"):
                    fields["utility"] = None
                # Время модель присылает как «15:00» (так её учит подсказка) — достраиваем дату самого дела
                start, end = _edit_time(e.get("start") or e.get("start_time"), current[5]), \
                    _edit_time(e.get("end") or e.get("end_time"), current[6])
                if start and end and end <= start:
                    end += timedelta(days=1)
                fields["start_time"] = start.isoformat(timespec="seconds") if start else None
                fields["end_time"] = end.isoformat(timespec="seconds") if end else None
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


PERIOD_RE = re.compile(r"недел|месяц|всего|в среднем|обычно|за все|за последн", re.I)


async def period_note(user_id: int, now: datetime) -> str:
    """Честная пометка к ответу про период: за какие дни на самом деле есть записи."""
    since = (now - timedelta(days=29)).strftime("%Y-%m-%d")
    days = sorted({row[0] for row in await db_manager.get_daily_totals(user_id, since)}, reverse=True)
    if not days:
        return "\nℹ️ Записей за этот период нет."
    shown = ", ".join(datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m") for d in days[:10])
    more = f" и ещё {len(days) - 10}" if len(days) > 10 else ""
    return f"\nℹ️ Считаю по записям за {len(days)} {_days_word(len(days))}: {shown}{more}. За другие дни записей нет."


def _edit_time(value, current_iso):
    """Время из поправки: «15:00» (на дату дела) или полная дата. None — не меняется."""
    if not value:
        return None
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(value).strip())
    if m:
        base = _parse_dt(current_iso) or _now_local().replace(tzinfo=None)
        if int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return None
        return base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return _parse_dt(value)


def _parse_dt(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", ""))
        return dt.replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _day_label(dt: datetime, now: datetime) -> str:
    """Пусто для сегодня, иначе «вчера» / «21.10» — чтобы вчерашнее дело не выглядело сегодняшним."""
    delta = (now.date() - dt.date()).days
    if delta == 0:
        return ""
    if delta == 1:
        return "вчера "
    return f"{dt.strftime('%d.%m')} "


def _stems(text: str) -> set:
    return {w[:5] for w in re.findall(r"[а-яёa-z]+", text.lower()) if len(w) >= 4}


def _match_habit(habit: str, daily: list):
    """Ежедневное напоминание, к которому относится отмеченное действие («Витамины» ↔ «Выпить витамины»)."""
    for r_id, r_text in daily:
        if r_text.lower() == habit.lower():
            return r_id, r_text
    habit_stems = _stems(habit)
    for r_id, r_text in daily:
        if habit_stems & _stems(r_text):
            return r_id, r_text
    return None


def _short(text: str, limit: int = 40) -> str:
    """Подпись кнопки: по целому слову и с «…», а не «после приема пищ»."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut if len(cut) > limit // 2 else text[:limit]) + "…"


async def _reply(status_msg: types.Message, transcript: str, voice: bool, lines: list, markup):
    """Ответ на запись. Расшифровка голосового — свёрнутой цитатой (раскрывается по нажатию): проверить, как бот
    расслышал, можно, но она не занимает пол-экрана."""
    body = html.escape(_join(lines))
    head = f"<blockquote expandable>{html.escape(transcript)}</blockquote>\n" if voice else ""
    await status_msg.edit_text(head + body, reply_markup=markup, parse_mode="HTML")


HINT_SHOWS = 3


async def _hint_once(user_id: int, key: str) -> bool:
    """Подсказку показываем первые HINT_SHOWS раз, потом молчим."""
    shown = int(await db_manager.get_setting(user_id, f"hint_{key}", "0") or 0)
    if shown >= HINT_SHOWS:
        return False
    await db_manager.set_setting(user_id, f"hint_{key}", shown + 1)
    return True


def _join(lines):
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


UTILITY_LABEL = {"productive": "🟢 полезное", "routine": "⚪ быт", "unproductive": "🔴 прокрастинация"}


async def process_entry(message: types.Message, status_msg: types.Message, transcript: str, voice: bool = False):
    """Сообщение пользователя: рассказ о дне, вопрос, поправка или просто реплика.
    voice — голосовое: его расшифровку показываем свёрнутой цитатой (текстовое сообщение и так видно выше)."""
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
        full = await needs_full_context(user_id, transcript, now)
        context = await build_context(user_id, now, full, transcript)
        analysis = await parse_voice_text(transcript, known_titles, about, context, with_edits=full)
        # Подстраховка: модель увидела вопрос или поправку, а полного контекста ей не дали — переспрашиваем с ним
        if not full and not analysis.get("rate_limited") and \
                (analysis["intent"] in ("question", "correction") or analysis["edits"]):
            print("[context] нужен полный контекст — переспрашиваю")
            context = await build_context(user_id, now, True, transcript)
            analysis = await parse_voice_text(transcript, known_titles, about, context, with_edits=True)
        await quota_watch(message.bot, analysis.get("rate_limited"))
        if analysis.get("rate_limited"):
            # Ничего не разобрано — не «👎», а просто честное «лимит, попробуй позже»
            await status_msg.edit_text(f"«{transcript}»\n\n{analysis['answer']}")
            return

        lines = []
        created = {"a": [], "t": [], "r": [], "summary": []}

        # 1. Поправки (могут прийти и вместе с рассказом)
        edit_lines = await apply_edits(user_id, analysis["edits"], now)
        lines.extend(edit_lines)
        # «Дальше буду работать над ботом» — план, а не сделанное (код убрал его из дел)
        plan_lines = [f"⏭ «{title}» — ещё впереди, не записываю как сделанное" for title in analysis.get("planned") or []]
        # «Буду работать либо над ELGE, либо над работой» — выбор, а не две задачи
        plan_lines += [f"⏭ Дальше: {choice[0].lower() + choice[1:]} — потом расскажешь, что было."
                       for choice in analysis.get("choices") or []]

        activities = analysis["activities"]
        has_new = bool(activities or analysis["tasks_to_add"] or analysis["reminders"] or analysis["habit_identified"])

        # 2. Вопрос / реплика / только поправка — просто ответ
        if not has_new:
            lines.extend(plan_lines)
            # После поправки строки «✏️ …» уже всё говорят — ответ модели был бы повтором
            if analysis["answer"] and not (edit_lines and analysis["intent"] == "correction"):
                lines.append(("\n" if edit_lines else "") + analysis["answer"])
                if analysis["intent"] == "question" and PERIOD_RE.search(transcript):
                    lines.append(await period_note(user_id, now))
            elif not edit_lines and not plan_lines:
                lines.append("Не нашёл здесь дел, задач или напоминаний. Расскажи, что было, "
                             "или спроси, например: «что у меня сегодня?»")
            await _reply(status_msg, transcript, voice, lines, _feedback_only())
            return

        # 3. Дела. Календарь: свой Google человека (/calendar) — сразу; у владельца без /calendar —
        # старый путь (token.json) после 3 недель наблюдения
        is_owner = user_id == config.OWNER_ID
        google = await db_manager.get_google(user_id) if google_oauth.enabled() else None
        legacy_calendar = not google and is_owner and profile["phase"] == "coaching"
        calendar_ok, calendar_lost, any_approx = True, False, False
        naive_now = now.replace(tzinfo=None)
        for act in activities:
            title = act.get("title") or "Дело"
            utility = act.get("utility") if act.get("utility") in UTILITY_LABEL else "routine"
            category = act.get("category", "Другое")
            start_time, end_time = act.get("start_time"), act.get("end_time")
            start_dt, end_dt = _parse_dt(start_time), _parse_dt(end_time)

            # Будущее — это план, а не сделанное дело
            if start_dt and start_dt > naive_now + timedelta(minutes=5):
                lines.append(f"⏭ «{title}» — ещё впереди, не записываю как сделанное")
                continue

            # Длительность — по времени, если оно есть: модель иногда пишет одно, а время ставит другое
            if start_dt and end_dt and end_dt > start_dt:
                duration = int((end_dt - start_dt).total_seconds() // 60)
            else:
                try:
                    duration = max(1, int(act.get("duration_minutes") or 30))
                except (TypeError, ValueError):
                    duration = 30
                # «20:46–20:46» или нет конца/начала — достраиваем интервал из длительности
                if end_dt and (not start_dt or start_dt >= end_dt):
                    start_dt = end_dt - timedelta(minutes=duration)
                elif start_dt and not end_dt:
                    end_dt = start_dt + timedelta(minutes=duration)
                if start_dt and end_dt:
                    start_time, end_time = start_dt.isoformat(), end_dt.isoformat()

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
            # День — по самому делу: вчерашние игры, записанные в 00:59, — это вчера
            await db_manager.record_pattern(user_id=user_id, title=title, utility=utility,
                                            date_str=start_dt.strftime("%Y-%m-%d") if start_dt else today_date)

            time_label = f"{_hm(start_time)}–{_hm(end_time)} " if _hm(start_time) and _hm(end_time) else ""
            # Время прикинуто (пользователь его не называл) — показываем «≈», чтобы догадка не выглядела фактом
            approx = "≈ " if time_label and act.get("time_exact") is False else ""
            day_label = _day_label(start_dt, now) if start_dt else ""
            lines.append(f"⏱ {day_label}{approx}{time_label}{title} · {_fmt_min(duration)} · {UTILITY_LABEL[utility]}")
            if approx:
                any_approx = True
            created["a"].append(activity_id)
            created["summary"].append(f"дело #{activity_id}: {time_label}{title}")

        # Подсказку про «≈» — только первые разы: дальше значок и так понятен, а ответ короче
        if any_approx and await _hint_once(user_id, "approx"):
            lines.append("≈ — время прикинул сам. Не так — напиши, например: «это было с 9 до 10»")
        # Про календарь — только если что-то не так («добавлено» каждый раз только удлиняло ответ)
        if activities:
            if calendar_lost:
                lines.append("⚠️ Доступ к Google Календарю отозван — подключи заново: /calendar")
            elif (google or legacy_calendar) and not calendar_ok:
                lines.append("📥 Сохранено в боте (Google Календарь сейчас не ответил)")

        lines.extend(plan_lines)

        # 4. Задачи
        if analysis["tasks_to_add"]:
            lines.append("\n📋 В задачи:")
            for t in analysis["tasks_to_add"]:
                if isinstance(t, str):  # старый формат — на всякий случай
                    t = {"text": t, "due": None}
                task_id = await db_manager.add_task(user_id, t["text"], t.get("due"))
                due = _due_label(t.get("due"), today_date)
                created["t"].append(task_id)
                created["summary"].append(f"задача #{task_id}: {t['text']}{due}")
                lines.append(f"- {t['text']}{due}")

        # 5. Привычка: привязываем к ежедневному напоминанию, иначе «Витамины» и «Выпить витамины» — разные
        # привычки, отметка в итогах дня не появится, а напоминание всё равно придёт
        habit = analysis["habit_identified"]
        if habit:
            daily = [(r_id, r_text) for r_id, r_text, _, r_date in await db_manager.get_reminders(user_id) if not r_date]
            match = _match_habit(habit, daily)
            if not match and REGULAR_RE.search(transcript):
                # «Каждый день заходить в Sky» — это просьба о новой привычке, а не отметка «сделано»
                lines.append(f"\n⏰ Во сколько напоминать каждый день «{habit}»? Напиши, например:\n/remind 10:00 {habit}")
                habit = None
            elif match:
                habit = match[1]
                await db_manager.mark_reminder_sent(match[0], today_date)  # сегодня уже сделано — не напоминать
        if habit:
            streak = await db_manager.update_habit(user_id, habit, today_date)
            lines.append(f"✅ {habit} — отмечено (серия: {streak} дн.)")

        # 6. Напоминания. Неточные («скорее всего в 3–4, либо в пятницу») не ставим, а позже спрашиваем
        tentative = [r for r in analysis["reminders"] if r.get("tentative")]
        if tentative:
            title, ask_at = await save_tentative(user_id, tentative, transcript, now.replace(tzinfo=None))
            created["p"] = [title[0]]
            created["summary"].append(f"неточный план: {title[1]} ({', '.join(_option_label(o) for o in tentative)})")
            labels = " / ".join(_option_label(o) for o in tentative)
            lines.append(f"🤔 {title[1]} — пока не точно ({labels}). В {ask_at.strftime('%H:%M')} спрошу, договорились ли.")
        for rem in analysis["reminders"]:
            if rem.get("tentative"):
                continue
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
            lines.append(f"🍽 Буду напоминать после еды: {text}" if time_str == MEAL else f"⏰ Напомню {when}: {text}")
            created["r"].append(reminder_id)
            created["summary"].append(f"напоминание #{reminder_id}: {when} — {text}")

        # В рассказе ответ модели обычно пересказывает уже перечисленное («Записал напоминание…») —
        # показываем его, только если в сообщении был ещё и вопрос
        if analysis["answer"] and (analysis["intent"] != "log" or "?" in transcript):
            lines.append(f"\n{analysis['answer']}")
        # Мягкий стоп-сигнал: один раз в день, когда телефон/игры перевалили за порог
        # (только если залипание добавлено за СЕГОДНЯ — на запись про вчерашний сериал не реагируем)
        if any(a.get("utility") == "unproductive" and str(a.get("start_time", "")).startswith(today_date) for a in activities):
            note = await phone_limit_note(user_id, today_date)
            if note:
                lines.append(f"\n{note}")

        builder = InlineKeyboardBuilder()

        # Напоминания «после еды»: если в записи была еда
        if any(a.get("category") == "Еда" for a in activities):
            for r_id, r_text, r_time, _ in await db_manager.get_reminders(user_id):
                if r_time == MEAL:
                    lines.append(f"🍽 Не забудь: {r_text}")
                    builder.button(text=f"✅ {_short(r_text)}", callback_data=f"remdone_{r_id}")
        if analysis["encouragement"]:
            lines.append(f"\n{analysis['encouragement']}")

        if created["a"] or created["t"] or created["r"] or created.get("p"):
            entry_id = await db_manager.add_entry(user_id, transcript, created, now.replace(tzinfo=None).isoformat())
            if created["a"]:
                builder.button(text="✏️ Время", callback_data=f"ed_{entry_id}")
            builder.button(text="↩️ Отменить", callback_data=f"undo2_{entry_id}")
        builder.button(text="👎 Ошибка", callback_data="fb")
        # «Не забудь …» — каждая своей строкой, служебные — одной строкой внизу
        service = 1 + bool(created["a"] or created["t"] or created["r"] or created.get("p")) + bool(created["a"])
        builder.adjust(*([1] * (len(list(builder.buttons)) - service)), service)
        await _reply(status_msg, transcript, voice, lines, builder.as_markup())

        # Знакомство: первая настоящая запись — шаг 3
        if (created["a"] or created["t"] or created["r"]) and \
                await db_manager.get_setting(user_id, "onboarding") == "first_entry":
            await db_manager.set_setting(user_id, "onboarding", "done")
            await message.answer(ONB_STEP3)

    except Exception as e:
        import traceback
        traceback.print_exc()
        await status_msg.edit_text(f"Ошибка обработки: {str(e)[:200]}")


# ---------- Неточные планы ----------

ASK_DEFAULT_MIN = 60  # «скорее всего в 3–4» без «напишет через …» — спрашиваем через час
ASK_RE = re.compile(r"через\s+(полчаса|пару часов|час|(\d+)\s*(мин|час))", re.I)
EVENT_TIME_RE = re.compile(r"^В (\d{2}:\d{2})\s+")


def _ask_delay(text: str) -> int:
    """«Она сказала, что через час напишет» — спросить через час."""
    m = ASK_RE.search(text or "")
    if not m:
        return ASK_DEFAULT_MIN
    word = m.group(1).lower()
    if word in ("полчаса", "пару часов", "час"):
        return {"полчаса": 30, "пару часов": 120, "час": 60}[word]
    return int(m.group(2)) * (1 if m.group(3).lower().startswith("мин") else 60)


def _option_label(option: dict) -> str:
    """«сегодня 15:00» / «пт 23.10 15:00» — время самого события, а не напоминания за 30 минут."""
    m = EVENT_TIME_RE.match(option["text"])
    d, today = datetime.strptime(option["date"], "%Y-%m-%d").date(), _now_local().date()
    day = "сегодня" if d == today else ("завтра" if d == today + timedelta(days=1)
                                        else f"{WEEKDAYS_SHORT[d.weekday()]} {d.strftime('%d.%m')}")
    return f"{day} {m.group(1) if m else option['time']}"


async def save_tentative(user_id: int, options: list, source_text: str, now: datetime):
    """Запоминает неточный план; возвращает ((id, название), когда спросить)."""
    title = EVENT_TIME_RE.sub("", options[0]["text"])
    title = title[0].upper() + title[1:]
    first = min(datetime.strptime(f"{o['date']} {o['time']}", "%Y-%m-%d %H:%M") for o in options)
    # Спросить — когда обещали написать, но не позже, чем за 5 минут до самого раннего варианта
    ask_at = max(now + timedelta(minutes=1),
                 min(now + timedelta(minutes=_ask_delay(source_text)), first - timedelta(minutes=5)))
    opts = [{"text": o["text"], "time": o["time"], "date": o["date"]} for o in options]
    t_id = await db_manager.add_tentative(user_id, title, opts, ask_at.isoformat(timespec="minutes"))
    return (t_id, title), ask_at


def tentative_question(t_id: int, title: str, options: list):
    builder = InlineKeyboardBuilder()
    for i, option in enumerate(options):
        builder.button(text=f"✅ {_option_label(option).capitalize()}", callback_data=f"tn_{t_id}_{i}")
    builder.button(text="🕐 Другое время", callback_data=f"tn_{t_id}_o")
    builder.button(text="✖ Не будет", callback_data=f"tn_{t_id}_x")
    builder.adjust(1)
    return f"🤔 {title} — договорились? Когда?", builder.as_markup()


@router.callback_query(F.data.startswith("tn_"))
async def process_tentative(callback: types.CallbackQuery):
    _, t_id, choice = callback.data.split("_")
    user_id = callback.from_user.id
    row = await db_manager.get_tentative(int(t_id), user_id)
    await callback.answer()
    if not row:
        await callback.message.edit_text("Этот план уже отменён.")
        return
    _, title, options = row
    await db_manager.delete_tentative(user_id, [row[0]])
    if choice == "x":
        await callback.message.edit_text(f"Ок, {title[0].lower() + title[1:]} — не ставлю.")
    elif choice == "o":
        await callback.message.edit_text(f"Напиши или скажи, когда договорились, — например: "
                                         f"«{title[0].lower() + title[1:]} в пятницу в 15».")
    else:
        option = options[int(choice)]
        reminder_id = await save_reminder(user_id, option["text"], option["time"], option["date"])
        await callback.message.edit_text(
            f"⏰ Напомню {format_when(option['time'], option['date'])}: {option['text']}" if reminder_id else
            f"✅ {option['text']} — напоминать уже поздно, время почти пришло.")


# ---------- Пробелы в дне ----------

GAP_MIN = 90                 # о дырах короче полутора часов не спрашиваем
GAP_DAY_START = (9, 0)       # раньше 9 утра не считаем пробелом (сон)
GAP_KINDS = {
    "w": ("Работа/учёба", "Работа", "productive", "💼 Работа/учёба"),
    "p": ("Залипание в телефоне", "Телефон", "unproductive", "📱 Телефон"),
    "r": ("Отдых", "Отдых", "routine", "🛋 Отдых"),
    "d": ("Дела и дорога", "Быт", "routine", "🚶 Дела/дорога"),
}


async def find_gaps(user_id: int, now: datetime, limit: int = 2) -> list[tuple[datetime, datetime]]:
    """Самые большие незаписанные промежутки за сегодня (с 9:00 или с первого дела до «сейчас»)."""
    today = now.strftime("%Y-%m-%d")
    naive_now = now.replace(tzinfo=None)
    intervals = []
    for _, _, _, _, _, start, end in await db_manager.get_activities_for_day(user_id, today):
        s, e = _parse_dt(start), _parse_dt(end)
        if s and e and e > s:
            intervals.append((s, min(e, naive_now)))
    if not intervals:
        return []  # за день ничего не записано — спрашивать «что было с 9 до 21» бессмысленно
    day_start = naive_now.replace(hour=GAP_DAY_START[0], minute=GAP_DAY_START[1], second=0, microsecond=0)
    cursor = min(day_start, min(s for s, _ in intervals))
    gaps = []
    for s, e in sorted(intervals):
        if s - cursor >= timedelta(minutes=GAP_MIN):
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if naive_now - cursor >= timedelta(minutes=GAP_MIN):
        gaps.append((cursor, naive_now))
    gaps = sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)[:limit]
    return sorted(gaps)


def gap_keyboard(start: datetime, end: datetime):
    stamp = f"{start.strftime('%Y%m%d%H%M')}_{end.strftime('%H%M')}"
    builder = InlineKeyboardBuilder()
    for key, (_, _, _, label) in GAP_KINDS.items():
        builder.button(text=label, callback_data=f"gap_{stamp}_{key}")
    builder.button(text="🤷 Неважно", callback_data=f"gap_{stamp}_x")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


async def ask_gaps(bot, user_id: int, now: datetime):
    for start, end in await find_gaps(user_id, now):
        await _notify_markup(bot, user_id, f"🕳 С {start.strftime('%H:%M')} до {end.strftime('%H:%M')} ничего не записано — "
                                           "что было?", gap_keyboard(start, end))


@router.callback_query(F.data.startswith("gap_"))
async def process_gap(callback: types.CallbackQuery):
    _, stamp, end_hm, key = callback.data.split("_")
    start = datetime.strptime(stamp, "%Y%m%d%H%M")
    end = start.replace(hour=int(end_hm[:2]), minute=int(end_hm[2:]))
    await callback.answer()
    if key == "x" or key not in GAP_KINDS:
        await callback.message.edit_text(f"Ок, {start.strftime('%H:%M')}–{end.strftime('%H:%M')} пропускаю.")
        return
    title, category, utility, _ = GAP_KINDS[key]
    user_id = callback.from_user.id
    start_iso, end_iso = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
    event_id, google = None, (await db_manager.get_google(user_id) if google_oauth.enabled() else None)
    if google:
        try:
            event_id = await push_event(user_id, google, title, start_iso, end_iso, utility, f"Категория: {category} | Из трекера дня")
        except CalendarLost:
            pass
    await db_manager.add_activity(user_id=user_id, title=title, category=category, utility=utility,
                                  duration_min=int((end - start).total_seconds() // 60),
                                  start_time=start_iso, end_time=end_iso, calendar_event_id=event_id)
    await db_manager.record_pattern(user_id=user_id, title=title, utility=utility, date_str=start.strftime("%Y-%m-%d"))
    await callback.message.edit_text(f"✅ Записал: {start.strftime('%H:%M')}–{end.strftime('%H:%M')} {title} · "
                                     f"{_fmt_min((end - start).total_seconds() // 60)}")


# ---------- Квота бесплатного Groq ----------

QUOTA_WARN_SHARE = 0.1  # предупредить владельца, когда осталось меньше 10% дневных запросов


async def quota_watch(bot, rate_limited: str | None = None):
    """Раз в день предупредить владельца: квота на исходе / уже кончилась."""
    owner = config.OWNER_ID
    if not owner:
        return
    today = _now_local().strftime("%Y-%m-%d")
    if rate_limited == "day" and await db_manager.get_setting(owner, "quota_out") != today:
        await db_manager.set_setting(owner, "quota_out", today)
        await _notify(bot, owner, "⛔ Бесплатный дневной лимит Groq закончился: бот до завтра не разбирает сообщения "
                                  "(команды работают). Если так будет часто — можно уменьшить DAILY_ENTRY_LIMIT или "
                                  "перейти на платный тариф Groq.")
        return
    if time.time() - ai_limit["primary_day"] < 3600 and await db_manager.get_setting(owner, "quota_primary") != today:
        await db_manager.set_setting(owner, "quota_primary", today)
        await _notify(bot, owner, "⚠️ Основная модель исчерпала дневной лимит токенов Groq (200 000 в сутки, это примерно "
                                  "60 сообщений). Бот продолжает работать на запасной модели — она чуть менее точна. "
                                  "Лимит восстанавливается постепенно в течение суток.")
    quota = ai_quota.get(PRIMARY_MODEL)
    if quota and quota["limit"] and quota["left"] / quota["limit"] < QUOTA_WARN_SHARE \
            and await db_manager.get_setting(owner, "quota_low") != today:
        await db_manager.set_setting(owner, "quota_low", today)
        await _notify(bot, owner, f"⚠️ Квота Groq на исходе: осталось {quota['left']} из {quota['limit']} запросов "
                                  "на сегодня. Когда кончится, бот будет отвечать «лимит исчерпан» до завтра.")


# ---------- Правка времени кнопками ----------

EDIT_STEP_MIN = 15


def _editor(row):
    a_id, title, _, _, duration, start, end, _ = row
    builder = InlineKeyboardBuilder()
    for label, field, delta in (("◀ начало −15", "s", -EDIT_STEP_MIN), ("начало +15 ▶", "s", EDIT_STEP_MIN),
                                ("◀ конец −15", "e", -EDIT_STEP_MIN), ("конец +15 ▶", "e", EDIT_STEP_MIN)):
        builder.button(text=label, callback_data=f"et_{a_id}_{field}_{delta}")
    builder.button(text="✅ Готово", callback_data=f"etok_{a_id}")
    builder.adjust(2, 2, 1)
    text = (f"✏️ {_hm(start)}–{_hm(end)} {title} · {_fmt_min(duration)}\n\n"
            "Двигай начало и конец кнопками. Или просто напиши, как было: «это было с 9 до 10».")
    return text, builder.as_markup()


@router.callback_query(F.data.startswith("ed_"))
async def process_edit_entry(callback: types.CallbackQuery):
    entry = await db_manager.get_entry(int(callback.data.split("_")[1]), callback.from_user.id)
    rows = [r for a_id in (entry[2].get("a", []) if entry else [])
            if (r := await db_manager.get_activity(a_id, callback.from_user.id))]
    await callback.answer()
    if not rows:
        await callback.message.answer("Эти дела уже удалены.")
        return
    if len(rows) == 1:
        text, markup = _editor(rows[0])
        await callback.message.answer(text, reply_markup=markup)
        return
    builder = InlineKeyboardBuilder()
    for a_id, title, _, _, _, start, end, _ in rows:
        builder.button(text=f"{_hm(start)}–{_hm(end)} {title}"[:60], callback_data=f"eda_{a_id}")
    builder.adjust(1)
    await callback.message.answer("Какое дело поправить?", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("eda_"))
async def process_edit_pick(callback: types.CallbackQuery):
    row = await db_manager.get_activity(int(callback.data.split("_")[1]), callback.from_user.id)
    await callback.answer()
    if not row:
        await callback.message.edit_text("Это дело уже удалено.")
        return
    text, markup = _editor(row)
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("et_"))
async def process_edit_time(callback: types.CallbackQuery):
    _, a_id, field, delta = callback.data.split("_")
    user_id = callback.from_user.id
    row = await db_manager.get_activity(int(a_id), user_id)
    if not row:
        await callback.answer("Это дело уже удалено")
        return
    start, end = _parse_dt(row[5]), _parse_dt(row[6])
    if not (start and end):
        await callback.answer("У этого дела нет времени")
        return
    shift = timedelta(minutes=int(delta))
    start, end = (start + shift, end) if field == "s" else (start, end + shift)
    if end - start < timedelta(minutes=5):
        await callback.answer("Короче уже некуда")
        return
    updated = await db_manager.update_activity(int(a_id), user_id, start_time=start.isoformat(timespec="seconds"),
                                               end_time=end.isoformat(timespec="seconds"))
    await callback.answer()
    if updated:
        await patch_event(user_id, updated)
        text, markup = _editor(updated)
        await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("etok_"))
async def process_edit_done(callback: types.CallbackQuery):
    row = await db_manager.get_activity(int(callback.data.split("_")[1]), callback.from_user.id)
    await callback.answer("Сохранено")
    if row:
        await callback.message.edit_text(f"✅ {_hm(row[5])}–{_hm(row[6])} {row[1]} · {_fmt_min(row[4])}")


# ---------- «👎 Бот ошибся» ----------

FEEDBACK_LABEL = "👎 Бот ошибся"


def _feedback_only():
    builder = InlineKeyboardBuilder()
    builder.button(text=FEEDBACK_LABEL, callback_data="fb")
    return builder.as_markup()


async def _notify_admins(bot, user, text: str):
    who = user.full_name + (f" (@{user.username})" if user.username else "")
    for admin_id in config.ALLOWED_USER_IDS:
        if admin_id != user.id:
            await _notify(bot, admin_id, f"{text}\n— {who}")


@router.callback_query(F.data == "fb")
async def process_feedback(callback: types.CallbackQuery):
    bot_text = callback.message.text or ""
    feedback_id = await db_manager.add_feedback(callback.from_user.id, bot_text)
    await callback.answer("Спасибо, записал!")
    # Кнопку убираем, остальные (например «Отменить») оставляем
    markup = callback.message.reply_markup
    if markup:
        rows = [[b for b in row if b.callback_data != "fb"] for row in markup.inline_keyboard]
        rows = [row for row in rows if row]
        await callback.message.edit_reply_markup(reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    prompt = await callback.message.answer(
        "Спасибо, что отметил(а)! Что было не так? Ответь на это сообщение (свайп влево → «Ответить») — "
        "одной фразой: «время не то», «это был вопрос», «не то название»… Можно и не отвечать.")
    await db_manager.set_feedback_prompt(feedback_id, prompt.message_id)
    await _notify_admins(callback.bot, callback.from_user, f"👎 Ошибка #{feedback_id}:\n\n{bot_text[:3000]}")


@router.message(Command("feedback"))
async def cmd_feedback(message: types.Message):
    """Админу: последние ошибки списком + все файлом (из него пополняется tests/eval_phrases.py)."""
    if not is_admin(message.from_user.id):
        return
    rows = await db_manager.list_feedback(200)
    if not rows:
        await message.answer("Пока никто не нажимал «👎 Бот ошибся».")
        return
    text = "👎 Последние ошибки:\n\n"
    for f_id, _, name, bot_text, comment, created in rows[:10]:
        first = (bot_text or "").split("\n")[0][:120]
        text += f"#{f_id} · {created[:16]} · {name or 'я'}\n{first}\n" + (f"💬 {comment}\n" if comment else "") + "\n"
    await message.answer(text[:4000])
    data = [{"id": f_id, "user": name, "created": created, "bot_text": bot_text, "comment": comment}
            for f_id, _, name, bot_text, comment, created in rows]
    await message.answer_document(
        BufferedInputFile(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                          filename=f"feedback_{_now_local().strftime('%Y-%m-%d')}.json"),
        caption="Все отметки файлом — пришли его Claude, чтобы добавить эти фразы в проверку бота.")


@router.callback_query(F.data.startswith("undo2_"))
async def process_undo(callback: types.CallbackQuery):
    entry = await db_manager.get_entry(int(callback.data.split("_")[1]), callback.from_user.id)
    if not entry:
        await callback.answer("Эту запись уже не отменить")
        return
    data = entry[2]
    event_ids = await db_manager.delete_items(callback.from_user.id, data.get("a", []), data.get("t", []), data.get("r", []))
    await db_manager.delete_tentative(callback.from_user.id, data.get("p", []))
    await drop_events(callback.from_user.id, event_ids)
    await callback.answer("Запись отменена")
    note = "\n\n↩️ Отменено — дела, задачи и напоминания из этой записи удалены."
    if any(e is None for e in event_ids) and callback.from_user.id == config.OWNER_ID \
            and await db_manager._phase(callback.from_user.id) == "coaching" and not await db_manager.get_google(callback.from_user.id):
        note += "\nСобытия в Google Календаре удали вручную."
    await callback.message.edit_text((callback.message.html_text or "") + html.escape(note), parse_mode="HTML")


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
    await callback.message.edit_text((callback.message.html_text or "") + html.escape(note), parse_mode="HTML")
