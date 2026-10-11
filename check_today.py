import datetime
import pytz
import config
from services.calendar_api import get_calendar_service

tz = pytz.timezone(config.TIMEZONE)
now = datetime.datetime.now(tz)
start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

service = get_calendar_service()
events = service.events().list(
    calendarId='primary',
    timeMin=start_of_day,
    timeMax=end_of_day,
    singleEvents=True,
    orderBy='startTime'
).execute().get('items', [])

print("--- СОБЫТИЯ ЗА СЕГОДНЯ ---")
for e in events:
    title = e.get('summary')
    s = e.get('start', {}).get('dateTime')
    end = e.get('end', {}).get('dateTime')
    color = e.get('colorId')
    print(f"[{color}] {title}: {s} -> {end}")