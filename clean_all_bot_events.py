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
    singleEvents=True
).execute().get('items', [])

deleted = 0
for e in events:
    summary = e.get('summary', '')
    if any(tag in summary for tag in ['🟢', '🔴', '⚪', 'Проверка связи']):
        service.events().delete(calendarId='primary', eventId=e['id']).execute()
        deleted += 1

print(f"Календарь очищен от тестовых меток бота: {deleted}")