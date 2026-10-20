"""Подключение Google Календаря каждым пользователем к СВОЕМУ аккаунту.

Человек жмёт в боте «Подключить» → входит в Google → Google возвращает его на PUBLIC_URL/oauth/google
→ бот получает ключ (refresh_token) и создаёт в его аккаунте отдельный календарь «Трекер дня».

Доступ узкий — calendar.app.created: бот может создавать свои календари и события только в них,
остальной календарь человека он не видит. Всё синхронное: вызывать через asyncio.to_thread.
"""
import urllib.parse

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config

SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
CALLBACK_PATH = "/oauth/google"
CALENDAR_NAME = "Трекер дня"


def enabled() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET and config.PUBLIC_URL)


def redirect_uri() -> str:
    return config.PUBLIC_URL.rstrip("/") + CALLBACK_PATH


def auth_url(state: str) -> str:
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # нужен refresh_token, чтобы писать в календарь без повторного входа
        "prompt": "consent",        # иначе при повторном подключении Google не выдаст refresh_token
        "state": state,
    }
    return AUTH_URI + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> dict:
    resp = requests.post(TOKEN_URI, timeout=20, data={
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    data = resp.json()
    if not data.get("refresh_token"):
        raise ValueError("Google не выдал refresh_token")
    granted = data.get("scope", "").split()
    if SCOPES[0] not in granted:
        raise PermissionError("доступ к календарю не выдан (галочку при входе сняли)")
    return {"token": data["access_token"], "refresh_token": data["refresh_token"]}


def _service(token_data: dict):
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data["refresh_token"],
        token_uri=TOKEN_URI,
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    if not creds.valid:
        creds.refresh(Request())  # RefreshError — если человек отозвал доступ в своём Google
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def ensure_calendar(token_data: dict, calendar_id: str | None) -> str:
    """Календарь «Трекер дня» в аккаунте человека: прежний (если ещё есть) или новый."""
    service = _service(token_data)
    if calendar_id:
        try:
            service.calendars().get(calendarId=calendar_id).execute()
            return calendar_id
        except Exception:
            pass  # удалили вручную — создадим заново
    calendar = service.calendars().insert(body={
        "summary": CALENDAR_NAME,
        "description": "Дела из Telegram-бота @MHabitT_bot. Можно скрыть или удалить — бот перестанет сюда писать.",
        "timeZone": config.TIMEZONE,
    }).execute()
    return calendar["id"]


def insert_event(token_data: dict, calendar_id: str, event: dict) -> str:
    return _service(token_data).events().insert(calendarId=calendar_id, body=event).execute()["id"]


def patch_event(token_data: dict, calendar_id: str, event_id: str, fields: dict):
    _service(token_data).events().patch(calendarId=calendar_id, eventId=event_id, body=fields).execute()


def delete_event(token_data: dict, calendar_id: str, event_id: str):
    _service(token_data).events().delete(calendarId=calendar_id, eventId=event_id).execute()


def revoke(token_data: dict):
    try:
        requests.post(REVOKE_URI, params={"token": token_data["refresh_token"]}, timeout=10)
    except Exception:
        pass  # не страшно: ключ всё равно удалим у себя
