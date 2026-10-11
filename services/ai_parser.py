import json
import re
import pytz
from datetime import datetime
from groq import Groq
import config

groq_client = Groq(api_key=config.GROQ_API_KEY)
# Используем openai/gpt-oss-120b - у нее нет жесткого лимита 1000 токенов в минуту
MODEL_NAME = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """
Ты — ассистент по учету времени. Проанализируй рассказ пользователя и верни ТОЛЬКО JSON.

Текущее время: {now_str}. Сегодня: {today_date}. Часовой пояс: {tz}.

КАТЕГОРИИ (utility):
- productive: работа, учеба, спорт, чтение, проекты.
- routine: быт, уборка, еда, гигиена.
- unproductive: залипание, прокрастинация.

ПРАВИЛА ВРЕМЕНИ:
- Если названы часы ('с 14:00 до 18:00'), ставь их точно: start_time: {today_date}T14:00:00, end_time: {today_date}T18:00:00.
- Если действия шли подряд, продолжай цепочку (следующее начинается в конец предыдущего) вплоть до текущего момента ({current_time}).
- Не ставь накладывающиеся интервалы.

ФОРМАТ JSON:
{{
  "activities": [
    {{
      "title": "Работа",
      "category": "Работа",
      "utility": "productive",
      "duration_minutes": 240,
      "start_time": "{today_date}T14:00:00",
      "end_time": "{today_date}T18:00:00"
    }}
  ],
  "tasks_to_add": [],
  "habit_identified": null,
  "encouragement": "Отличная работа!"
}}
"""

async def parse_voice_text(text: str) -> dict:
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    today_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    prompt = SYSTEM_PROMPT.format(
        now_str=now_str,
        today_date=today_date,
        current_time=current_time,
        tz=config.TIMEZONE
    )

    try:
        response = groq_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=600,
            response_format={"type": "json_object"}
        )
        raw_text = response.choices[0].message.content.strip()
    except Exception as e:
        # Резерв на быструю модель gpt-oss-20b
        print(f"Fallback to gpt-oss-20b due to: {e}")
        response = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"}
        )
        raw_text = response.choices[0].message.content.strip()

    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
        raw_text = re.sub(r"\n?```$", "", raw_text)

    data = json.loads(raw_text.strip())
    if "activities" not in data:
        for k, v in data.items():
            if isinstance(v, dict) and "activities" in v:
                return v
        data["activities"] = []
    return data