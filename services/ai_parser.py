import json
import re
import asyncio
import pytz
from datetime import datetime
from groq import Groq
import config

groq_client = Groq(api_key=config.GROQ_API_KEY)

PRIMARY_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "openai/gpt-oss-20b"

SYSTEM_PROMPT = """Ты — ассистент по учёту времени. Проанализируй текст пользователя и верни ТОЛЬКО валидный JSON.

Текущее время: {now_str}. Сегодня: {today_date}. Часовой пояс: {tz}.

КАТЕГОРИИ (utility):
- productive: работа, учёба, спорт, чтение, проекты
- routine: быт, уборка, еда, гигиена, готовка
- unproductive: залипание в телефоне, прокрастинация, бесцельное время

Верни JSON строго в этом формате:
{{
  "activities": [
    {{
      "title": "Название",
      "category": "Категория",
      "utility": "productive",
      "duration_minutes": 60,
      "start_time": "2024-01-01T08:00:00",
      "end_time": "2024-01-01T09:00:00"
    }}
  ],
  "tasks_to_add": [],
  "habit_identified": null,
  "encouragement": "Мотивирующая фраза на русском"
}}

Замени дату на {today_date}. Никакого текста кроме JSON."""


def _call_groq(model: str, messages: list, use_json_format: bool = False) -> str:
    """Синхронный вызов Groq API (будет запущен в отдельном потоке)."""
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        # gpt-oss сначала «думает», и эти токены входят в лимит — иначе ответ пустой
        "max_tokens": 4000,
        "reasoning_effort": "low",
    }
    if use_json_format:
        kwargs["response_format"] = {"type": "json_object"}

    response = groq_client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("модель вернула пустой ответ")
    return content


def _clean_json(raw: str) -> str:
    """Очищает ответ от markdown и лишнего текста, оставляя только JSON."""
    raw = raw.strip()
    # Убираем ```json ... ``` блоки
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()
    # Берём только от первой { до последней }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    return raw


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

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": text}
    ]

    raw_text = None

    # Попытка 1 — основная модель с json_object (поддерживается llama-3.3-70b)
    try:
        raw_text = await asyncio.to_thread(_call_groq, PRIMARY_MODEL, messages, True)
        print(f"[ai_parser] {PRIMARY_MODEL} ответил: {raw_text[:200]}")
    except Exception as e:
        print(f"[ai_parser] {PRIMARY_MODEL} упал: {e}")
        raw_text = None

    # Попытка 2 — основная модель без json_object
    if raw_text is None:
        try:
            raw_text = await asyncio.to_thread(_call_groq, PRIMARY_MODEL, messages, False)
            print(f"[ai_parser] {PRIMARY_MODEL} (без json) ответил: {raw_text[:200]}")
        except Exception as e:
            print(f"[ai_parser] {PRIMARY_MODEL} (без json) упал: {e}")
            raw_text = None

    # Попытка 3 — резервная модель
    if raw_text is None:
        try:
            raw_text = await asyncio.to_thread(_call_groq, FALLBACK_MODEL, messages, False)
            print(f"[ai_parser] {FALLBACK_MODEL} ответил: {raw_text[:200]}")
        except Exception as e:
            print(f"[ai_parser] {FALLBACK_MODEL} упал: {e}")
            raw_text = None

    # Все модели упали
    if raw_text is None:
        print("[ai_parser] Все модели упали, возвращаю заглушку.")
        return {
            "activities": [],
            "tasks_to_add": [],
            "habit_identified": None,
            "encouragement": "Слышу тебя! Что-то пошло не так с анализом, попробуй ещё раз через минуту."
        }

    # Парсим JSON
    cleaned = _clean_json(raw_text)
    try:
        data = json.loads(cleaned)
        print(f"[ai_parser] JSON успешно распарсен, активностей: {len(data.get('activities', []))}")
    except json.JSONDecodeError as e:
        print(f"[ai_parser] JSONDecodeError: {e}")
        print(f"[ai_parser] Сырой ответ: {raw_text[:500]}")
        return {
            "activities": [],
            "tasks_to_add": [],
            "habit_identified": None,
            "encouragement": "Получила твоё сообщение! Попробуй ещё раз — иногда нужна пара попыток 🙂"
        }

    # Гарантируем нужные ключи
    data.setdefault("activities", [])
    data.setdefault("tasks_to_add", [])
    data.setdefault("habit_identified", None)
    data.setdefault("encouragement", "")

    # Если activities вложены внутрь другого ключа
    if not data["activities"]:
        for k, v in data.items():
            if isinstance(v, dict) and "activities" in v:
                return v

    return data