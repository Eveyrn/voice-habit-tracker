import json
import re
import pytz
from datetime import datetime
from groq import Groq
import config

groq_client = Groq(api_key=config.GROQ_API_KEY)

# llama-3.3-70b-versatile — надёжная модель с хорошим JSON-выводом
PRIMARY_MODEL = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama3-8b-8192"

SYSTEM_PROMPT = """Ты — ассистент по учету времени. Проанализируй рассказ пользователя и верни ТОЛЬКО валидный JSON без каких-либо пояснений, markdown-блоков или лишнего текста.

Текущее время: {now_str}. Сегодня: {today_date}. Часовой пояс: {tz}.

КАТЕГОРИИ (utility):
- productive: работа, учеба, спорт, чтение, проекты.
- routine: быт, уборка, еда, гигиена.
- unproductive: залипание, прокрастинация.

ПРАВИЛА ВРЕМЕНИ:
- Если названы часы ('с 14:00 до 18:00'), ставь их точно.
- Если действия шли подряд, продолжай цепочку вплоть до текущего момента ({current_time}).
- Не ставь накладывающиеся интервалы.

ВЕРНИ ТОЛЬКО ЭТОТ JSON (без ```json, без пояснений):
{{
  "activities": [
    {{
      "title": "Название активности",
      "category": "Категория",
      "utility": "productive",
      "duration_minutes": 60,
      "start_time": "{today_date}T14:00:00",
      "end_time": "{today_date}T15:00:00"
    }}
  ],
  "tasks_to_add": [],
  "habit_identified": null,
  "encouragement": "Мотивирующая фраза"
}}"""


def _clean_json(raw: str) -> str:
    """Очищает ответ модели от markdown-блоков и лишнего текста."""
    raw = raw.strip()
    # Убираем ```json ... ``` блоки
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    # Находим первый { и последний } — берём только JSON
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return raw


def _build_messages(prompt: str, text: str) -> list:
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": text}
    ]


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

    raw_text = None

    # Попытка 1 — основная модель без json_object (надёжнее)
    try:
        response = groq_client.chat.completions.create(
            model=PRIMARY_MODEL,
            messages=_build_messages(prompt, text),
            temperature=0.1,
            max_tokens=800
        )
        raw_text = response.choices[0].message.content
    except Exception as e:
        print(f"[ai_parser] Основная модель ({PRIMARY_MODEL}) упала: {e}")

    # Попытка 2 — резервная модель
    if raw_text is None:
        try:
            response = groq_client.chat.completions.create(
                model=FALLBACK_MODEL,
                messages=_build_messages(prompt, text),
                temperature=0.1,
                max_tokens=600
            )
            raw_text = response.choices[0].message.content
        except Exception as e:
            print(f"[ai_parser] Резервная модель ({FALLBACK_MODEL}) тоже упала: {e}")

    # Если обе модели упали — возвращаем заглушку
    if raw_text is None:
        return {
            "activities": [],
            "tasks_to_add": [],
            "habit_identified": None,
            "encouragement": "Не удалось разобрать сообщение, попробуй ещё раз."
        }

    # Парсим JSON
    cleaned = _clean_json(raw_text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"[ai_parser] Не удалось распарсить JSON: {e}\nRaw: {raw_text[:300]}")
        return {
            "activities": [],
            "tasks_to_add": [],
            "habit_identified": None,
            "encouragement": "Получила твоё сообщение, но не смогла разобрать детали. Попробуй ещё раз!"
        }

    # Гарантируем наличие ключей
    data.setdefault("activities", [])
    data.setdefault("tasks_to_add", [])
    data.setdefault("habit_identified", None)
    data.setdefault("encouragement", "")

    # Если activities вложены в другой ключ
    if not data["activities"]:
        for k, v in data.items():
            if isinstance(v, dict) and "activities" in v:
                return v

    return data