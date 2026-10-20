import os
import os.path
import datetime
import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import config

SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'

COLOR_MAP = {
    'productive': '10',    # Зеленый (Basil)
    'routine': '8',         # Графит / Серый
    'unproductive': '11',   # Красный (Flamingo)
    'task': '7'             # Бирюзовый
}

_service_cache = None


class CalendarUnavailable(Exception):
    """Нет рабочего ключа Google — календарь пропускаем, бот работает дальше."""


def get_calendar_service():
    """Сервис календаря из token.json. Никогда не открывает браузер для входа:
    на сервере это вешает бота навсегда. Вход делается локально: python auth_calendar.py"""
    global _service_cache
    if _service_cache is not None:
        return _service_cache

    if not os.path.exists(TOKEN_FILE):
        raise CalendarUnavailable("нет token.json — запусти локально auth_calendar.py")

    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise CalendarUnavailable("в token.json нет refresh_token — войди заново через auth_calendar.py")
        try:
            creds.refresh(Request())
        except Exception as e:
            # invalid_grant: ключ отозван или истёк (у приложения в режиме «Тестирование» — через 7 дней)
            raise CalendarUnavailable(f"ключ Google не обновился ({e}) — войди заново через auth_calendar.py")
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())

    _service_cache = build('calendar', 'v3', credentials=creds, cache_discovery=False)
    return _service_cache

def normalize_iso(dt_str: str, default_dt: datetime.datetime, offset_minutes: int = 0) -> str:
    """Гарантирует валидную RFC3339 строку с таймзоной."""
    tz = pytz.timezone(config.TIMEZONE)
    if not dt_str or not isinstance(dt_str, str):
        target = default_dt + datetime.timedelta(minutes=offset_minutes)
        return target.isoformat()
    try:
        clean_str = dt_str.replace("Z", "").strip()
        dt = datetime.datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        return dt.isoformat()
    except Exception:
        target = default_dt + datetime.timedelta(minutes=offset_minutes)
        return target.isoformat()

def build_event(summary: str, start_time: str, end_time: str, utility: str = 'productive', description: str = '') -> dict:
    """Тело события Google: время с часовым поясом, конец позже начала, цвет и значок по пользе."""
    now_dt = datetime.datetime.now(pytz.timezone(config.TIMEZONE))
    valid_start = normalize_iso(start_time, now_dt, -30)
    valid_end = normalize_iso(end_time, now_dt, 0)
    if valid_end <= valid_start:
        valid_end = (datetime.datetime.fromisoformat(valid_start) + datetime.timedelta(minutes=30)).isoformat()

    prefix = "🟢 " if utility == "productive" else ("⚪ " if utility == "routine" else "🔴 ")
    return {
        'summary': f"{prefix}{summary}",
        'description': description,
        'start': {'dateTime': valid_start, 'timeZone': config.TIMEZONE},
        'end': {'dateTime': valid_end, 'timeZone': config.TIMEZONE},
        'colorId': COLOR_MAP.get(utility, '10'),
    }


def add_event(summary: str, start_time: str, end_time: str, utility: str = 'productive', description: str = ''):
    """Старый путь владельца: token.json и основной календарь. Если владелец подключился через /calendar,
    используется его личный «Трекер дня», а этот путь не нужен."""
    try:
        service = get_calendar_service()
        event = build_event(summary, start_time, end_time, utility, description)
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        return created_event.get('htmlLink')
    except CalendarUnavailable as e:
        print(f"Календарь недоступен: {e}")
        return None
    except Exception as e:
        print(f"Ошибка Google Calendar API: {e}")
        return None