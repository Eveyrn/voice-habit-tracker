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

О пользователе: разработчица, делает Telegram-ботов и проект ELGE (сайт недвижимости).
Работа над ботом, кодом, ELGE, дизайном — это productive.

ТЕКСТ — РАСШИФРОВКА ГОЛОСА, в нём бывают искажённые слова.
- Если слово явно искажено и по контексту понятно, что имелось в виду (например «занималась батом» после разговора о делах = «ботом»), исправь.
- Если слово непонятно — НЕ придумывай смысл. Оставь его в названии как есть и поставь utility "routine".

Верни JSON строго в этом формате:
{{
  "activities": [
    {{
      "title": "Название",
      "category": "Работа",
      "utility": "productive",
      "duration_minutes": 60,
      "start_time": "2024-01-01T08:00:00",
      "end_time": "2024-01-01T09:00:00"
    }}
  ],
  "tasks_to_add": ["Позвонить маме"],
  "habit_identified": null,
  "reminders": [{{"text": "Выпить витамины", "time": "10:00"}}],
  "encouragement": "Мотивирующая фраза на русском"
}}

ВРЕМЯ. Пользователь рассказывает о том, что УЖЕ было. Если время не названо («час залипала, потом 2 часа работала»),
выстрой дела подряд так, чтобы ПОСЛЕДНЕЕ закончилось сейчас ({current_time}), и считай назад. Ничего не ставь в будущее.

category — СТРОГО одно из: {categories}.

НАЗВАНИЯ ДЕЛ (title) — коротко, существительным, без подробностей в скобках: «Залипание в телефоне», «Завтрак», «Работа над ботом».
Уже известные дела пользователя: {known_titles}
Если дело по смыслу совпадает с известным — используй ТОЧНО то же название (иначе статистика ритмов рассыплется).
Новое название — только если такого дела в списке правда нет.

tasks_to_add — список СТРОК (только текст задачи), пустой список, если планов нет.
habit_identified — строка с названием привычки или null.
reminders — только если пользователь ПРОСИТ регулярно напоминать («напоминай мне…», «надо бы мне напоминать…»).
  text — короткое действие в инфинитиве («Выпить витамины»); time — "ЧЧ:ММ", если время названо, иначе null.
  Такое действие НЕ дублируй в tasks_to_add. Если просьб о напоминаниях нет — пустой список.

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


CATEGORIES = ["Работа", "Учёба", "Спорт", "Еда", "Быт", "Здоровье", "Сон", "Дорога", "Общение", "Отдых", "Телефон", "Другое"]


async def parse_voice_text(text: str, known_titles: list | None = None) -> dict:
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    today_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    prompt = SYSTEM_PROMPT.format(
        now_str=now_str,
        today_date=today_date,
        current_time=current_time,
        tz=config.TIMEZONE,
        categories=", ".join(CATEGORIES),
        known_titles=", ".join(f"«{t}»" for t in known_titles) if known_titles else "пока нет",
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
            "reminders": [],
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
            "reminders": [],
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
                data = v
                break

    # Модель иногда присылает задачи/привычку объектами — в БД нужны строки
    # Категория — только из фиксированного списка, иначе /summary дробится на «залипание в телефоне», «телефон», …
    lower_map = {c.lower(): c for c in CATEGORIES}
    for act in data["activities"]:
        if isinstance(act, dict):
            act["category"] = lower_map.get(str(act.get("category", "")).strip().lower(), "Другое")

    data["tasks_to_add"] = [t for t in (_as_text(x) for x in data.get("tasks_to_add") or []) if t]
    data["habit_identified"] = _as_text(data.get("habit_identified")) or None
    data["reminders"] = _clean_reminders(data.get("reminders"))

    return data


def _clean_reminders(raw) -> list:
    """Оставляет только [{text, time}] с time формата ЧЧ:ММ или None."""
    result = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"text": item, "time": None}
        if not isinstance(item, dict):
            continue
        text = _as_text(item)
        if not text:
            continue
        text = text[0].upper() + text[1:]
        time = item.get("time")
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(time).strip()) if time else None
        time = f"{int(m.group(1)):02d}:{m.group(2)}" if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 else None
        result.append({"text": text, "time": time})
    return result


def _as_text(value) -> str:
    """Достаёт текст из строки или из объекта вида {"title": ...}."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("title", "task", "text", "name", "description"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
        return ""
    return str(value).strip() if value else ""