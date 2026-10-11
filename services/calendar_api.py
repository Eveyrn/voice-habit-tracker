import os
import os.path
import datetime
import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
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

def get_calendar_service():
    global _service_cache
    if _service_cache is not None:
        return _service_cache

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise FileNotFoundError(f"Файл {CREDENTIALS_FILE} не найден!")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
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

def add_event(summary: str, start_time: str, end_time: str, utility: str = 'productive', description: str = ''):
    """
    Добавляет событие в календарь Google с валидацией таймзон и дат.
    """
    try:
        service = get_calendar_service()
        color_id = COLOR_MAP.get(utility, '10')

        tz = pytz.timezone(config.TIMEZONE)
        now_dt = datetime.datetime.now(tz)

        # Валидация и нормализация времени
        valid_start = normalize_iso(start_time, now_dt, -30)
        valid_end = normalize_iso(end_time, now_dt, 0)

        # Проверка чтобы конец был позже начала
        if valid_end <= valid_start:
            s_obj = datetime.datetime.fromisoformat(valid_start)
            valid_end = (s_obj + datetime.timedelta(minutes=30)).isoformat()

        prefix = "🟢 " if utility == "productive" else ("⚪ " if utility == "routine" else "🔴 ")
        event_title = f"{prefix}{summary}"

        event = {
            'summary': event_title,
            'description': description,
            'start': {
                'dateTime': valid_start,
                'timeZone': config.TIMEZONE,
            },
            'end': {
                'dateTime': valid_end,
                'timeZone': config.TIMEZONE,
            },
            'colorId': color_id
        }

        created_event = service.events().insert(calendarId='primary', body=event).execute()
        return created_event.get('htmlLink')
    except Exception as e:
        print(f"Ошибка Google Calendar API: {e}")
        return None