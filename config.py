import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = [int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()]
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Bishkek")

# Владелец — первый в ALLOWED_USER_IDS: его Google Календарь, ему бэкапы и заявки на доступ.
# Остальные из ALLOWED_USER_IDS — тоже админы. Другие люди попадают в бота по заявке (/start → «Пустить»).
OWNER_ID = ALLOWED_USER_IDS[0] if ALLOWED_USER_IDS else None
# Сколько сообщений в день можно не-админу: квота Groq бесплатная и одна на всех
DAILY_ENTRY_LIMIT = int(os.getenv("DAILY_ENTRY_LIMIT", "50"))

# Подключение Google Календаря каждым пользователем (/calendar). Без этих трёх переменных команда
# просто скажет, что календарь ещё не настроен. Секрет — только в «Окружении» хостинга, не в git.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # https://….apps.bot-hosting.cloud
