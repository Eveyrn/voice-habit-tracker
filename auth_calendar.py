"""Вход в Google Календарь. Запускать ЛОКАЛЬНО (на компьютере с браузером), не на хостинге:

    python auth_calendar.py

Откроется браузер → войти → «Разрешить». Появится token.json — загрузить его на хостинг
(вкладка «Файлы», в корень рядом с main.py) и перезапустить бота.

Чтобы ключ не истекал через 7 дней, приложение в Google Cloud Console должно быть
опубликовано: APIs & Services → OAuth consent screen → Publish app («В продакшене»).
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
# prompt=consent — Google заново выдаёт refresh_token (без него ключ не продлевается)
creds = flow.run_local_server(port=8080, open_browser=True, prompt='consent', access_type='offline')

with open(TOKEN_FILE, 'w') as token:
    token.write(creds.to_json())

print('Готово: token.json сохранён. Загрузи его на хостинг и перезапусти бота.')
