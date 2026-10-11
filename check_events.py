import datetime
import pytz
import config
from services.calendar_api import get_calendar_service

tz = pytz.timezone(config.TIMEZONE)
now = datetime.datetime.now(tz)
time_min = (now - datetime.timedelta(days=1)).isoformat()

service = get_calendar_service()
events_result = service.events().list(
    calendarId='primary',
    timeMin=time_min,
    singleEvents=True,
    orderBy='startTime'
).execute()

events = events_result.get('items', [])
print(f"Всего событий найдено: {len(events)}")
for e in events:
    summary = e.get('summary', 'Без названия')
    start = e.get('start', {}).get('dateTime', 'Нет')
    end = e.get('end', {}).get('dateTime', 'Нет')
    color = e.get('colorId', 'По умолчанию')
    print(f"Событие: {summary} | Время: {start} -> {end} | Цвет: {color}")