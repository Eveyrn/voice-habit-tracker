import json
import math
import re
import asyncio
import time
import groq
import pytz
from datetime import datetime, timedelta
from groq import Groq
import config
from services import timeline

groq_client = Groq(api_key=config.GROQ_API_KEY)

PRIMARY_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "openai/gpt-oss-20b"

SYSTEM_PROMPT = """Ты — помощник в Telegram-трекере дня. Пользователь пишет или наговаривает сообщение.
Пойми, что это за сообщение, и верни ТОЛЬКО валидный JSON.

Сейчас: {now_str}, {weekday}. Сегодня: {today_date}. Завтра: {tomorrow_date}. Часовой пояс: {tz}.

О пользователе: {about}

ДАННЫЕ ПОЛЬЗОВАТЕЛЯ (номера # — чтобы ссылаться на них в edits):
{context}

ТИП СООБЩЕНИЯ (intent):
- "log" — рассказ о том, что делал(а), и/или планы, задачи, просьбы напомнить.
- "question" — вопрос про свой день, планы, задачи, напоминания («что у нас сегодня?», «во сколько я к маме?»,
  «сколько я сидела в телефоне?»). Ответь в "answer" по ДАННЫМ выше, коротко и по делу, без выдумок.
  Данные есть ТОЛЬКО за дни, перечисленные выше. Если спрашивают про период шире («за неделю», «за месяц») —
  посчитай по тем дням, что есть, и прямо назови их («записи есть только за сегодня и вчера: …»).
  Если в данных ответа нет — так и скажи. Ничего не создавай.
- "correction" — поправка к тому, что уже записано или создано («не каждый день, только сегодня»,
  «нет, я играла до 11», «удали это напоминание»). Опиши изменения в "edits", коротко подтверди в "answer".
- "chat" — реплика без данных («спасибо», «привет»). Коротко и по-человечески ответь в "answer".
Сообщение может и рассказывать, и поправлять — тогда intent "log", а поправки всё равно в "edits".
Обращайся к пользователю на «ты». О себе (ты — бот) говори в мужском роде: «записал», «поправил».
«Это», «его», «то напоминание» обычно относится к ПРЕДЫДУЩЕМУ СООБЩЕНИЮ.

Верни JSON строго в этом формате:
{{
  "intent": "log",
  "answer": "",
  "activities": [
    {{
      "title": "Тренировка",
      "category": "Спорт",
      "utility": "productive",
      "day": "today",
      "start": null,
      "end": null,
      "duration_min": 90,
      "ended_ago_min": 60,
      "part_of_day": null
    }}
  ],
  "tasks_to_add": [{{"text": "Купить корм коту", "due": "tomorrow"}}],
  "habit_identified": null,
  "reminders": [{{"text": "Быть у мамы", "time": "15:00", "repeat": "once", "date": "{today_date}", "kind": "event"}}],
  "edits": [{{"op": "reminder_once", "id": 12, "date": "{today_date}"}}],
  "encouragement": ""
}}

ДЕЛА (activities) — только то, что УЖЕ произошло.
- utility: productive — работа, учёба, спорт, чтение, проекты; routine — быт, еда, гигиена, дорога, покупки,
  общение с близкими, сон; unproductive — залипание в телефоне, бесцельные соцсети, сериалы и игры «залипательно»,
  прокрастинация.
- category — СТРОГО одно из: {categories}.
- title — коротко, существительным, без подробностей в скобках. Обычные дела называй так:
  {canonical_titles}.
  Уже известные дела пользователя: {known_titles}
  Если дело по смыслу совпадает с известным — используй ТОЧНО то же название. Новое — только если такого правда нет.
- Действия, которые шли вместе и одновременно («встретилась с мамой, купили куртки»), — ОДНО дело с общим
  названием («Покупки с мамой»), а не два.

ВРЕМЯ ДЕЛА. НИЧЕГО НЕ ВЫЧИСЛЯЙ — только перенеси то, что сказано. Время посчитает бот.
- day: "today", "yesterday" или дата "ГГГГ-ММ-ДД".
- start / end: время "ЧЧ:ММ" (24 часа), ТОЛЬКО если человек его назвал («с 8 до 10» → "08:00" и "10:00»;
  «в 3 дня» → "15:00"). Если в ДАННЫХ есть связанное событие со временем (напоминание «В 15:00 быть у мамы»,
  а пишут «встретилась с мамой») — start = время того события. Иначе null.
- duration_min: сколько длилось, если сказано («полтора часа» → 90) или очевидно («позавтракала» → 20). Иначе null.
- ended_ago_min: если сказано, сколько времени назад закончилось («час назад закончила» → 60, «только что» → 0). Иначе null.
- part_of_day: если сказано только «утром» / «днём» / «вечером» / «ночью» → "morning" / "day" / "evening" / "night". Иначе null.
- Если о времени вообще ничего не сказано — всё null: бот поставит такие дела подряд до «сейчас».
- В массиве — по порядку времени.

ПРИМЕРЫ (сейчас для примера 20:30):
«с 8 до 10 сидела в телефоне» → {{"title": "Залипание в телефоне", "day": "today", "start": "08:00", "end": "10:00", "duration_min": null, "ended_ago_min": null, "part_of_day": null}}
«час назад закончила тренировку, шла полтора часа» → {{"title": "Тренировка", "day": "today", "start": null, "end": null, "duration_min": 90, "ended_ago_min": 60, "part_of_day": null}}
«утром ходила в зал» → {{"title": "Тренировка", "day": "today", "start": null, "end": null, "duration_min": null, "ended_ago_min": null, "part_of_day": "morning"}}
«вчера весь вечер смотрела сериал» → {{"title": "Сериал", "day": "yesterday", "start": null, "end": null, "duration_min": 180, "ended_ago_min": null, "part_of_day": "evening"}}
«поела, а потом 2 часа работала над ботом» → [{{"title": "Еда", "duration_min": 20, "start": null, "end": null, "ended_ago_min": null, "part_of_day": null, "day": "today"}}, {{"title": "Работа над ботом", "duration_min": 120, "start": null, "end": null, "ended_ago_min": null, "part_of_day": null, "day": "today"}}]
«поспала с часу ночи до девяти» → {{"title": "Сон", "day": "today", "start": "01:00", "end": "09:00", "duration_min": null, "ended_ago_min": null, "part_of_day": null}}
«потом пойду работать» → это будущее: НЕ дело. Если нет времени — в tasks_to_add.

ЗАДАЧИ (tasks_to_add) — дела на будущее БЕЗ конкретного времени. Объекты {{"text": "…", "due": …}}:
due — "today", "tomorrow" или "ГГГГ-ММ-ДД", если назван день («завтра купить корм коту» → "tomorrow",
«в пятницу сдать отчёт» → дата пятницы); иначе null. text — без слов про день («Купить корм коту»).
Каждое упомянутое дело на будущее — отдельная задача, даже без срока: «завтра купить корм, сегодня позвонить в банк
и когда-нибудь помыть машину» → 3 задачи (due: tomorrow, today, null).

ПРИВЫЧКИ. habit_identified — если пользователь сделал то, о чём у него есть ЕЖЕДНЕВНОЕ напоминание в ДАННЫХ
(«выпила витамины» ↔ напоминание «Выпить витамины»), впиши ТОЧНО текст этого напоминания. Иначе null.
Такое короткое действие (витамины, таблетки, стакан воды) в activities НЕ добавляй.

НАПОМИНАНИЯ (reminders):
- Разовое: repeat "once", date — дата. Это любые планы с временем на сегодня/завтра/дату.
  kind "event" — встреча или дело в своё время («сегодня к 3 быть у мамы», «завтра в 9 к врачу», «в 10 созвон»):
  time — время САМОГО события, text — без времени («Быть у мамы»). Бот сам напомнит заранее.
  kind "reminder" — человек просит напомнить именно в этот момент («напомни в 15:00 позвонить маме»): time — этот момент.
- Ежедневное: repeat "daily", date null, kind "reminder" — ТОЛЬКО если явно просят регулярно («каждый день»,
  «каждое утро», «напоминай мне пить витамины»). text — без времени («Читать»).
- Время всегда в 24-часовом формате: «в 10 вечера», «каждый вечер в 10» → "22:00"; «в 3 дня» → "15:00".
- После еды: repeat "daily", time "meal".
- Время не названо и непонятно — time null.
- Что попало в reminders, НЕ дублируй в tasks_to_add.

ПОПРАВКИ (edits) — только к элементам с номерами из ДАННЫХ:
- {{"op": "reminder_once", "id": N, "date": "ГГГГ-ММ-ДД"}} — сделать напоминание разовым
- {{"op": "reminder_daily", "id": N}} — сделать ежедневным
- {{"op": "reminder_time", "id": N, "time": "ЧЧ:ММ"}}
- {{"op": "reminder_delete", "id": N}}
- {{"op": "activity_update", "id": N, "title": "...", "start": "ЧЧ:ММ", "end": "ЧЧ:ММ", "utility": "...", "category": "..."}} — только изменённые поля
- {{"op": "activity_delete", "id": N}}
- {{"op": "task_delete", "id": N}}

answer для intent "log" — "" (бот сам перечислит, что записал), если только в сообщении нет ещё и вопроса.
encouragement — одна короткая живая фраза, только для intent "log"; для остальных "". Без пафоса и банальностей.

ТЕКСТ БЫВАЕТ РАСШИФРОВКОЙ ГОЛОСА, в нём встречаются искажённые слова.
- Если слово явно искажено и по контексту понятно, что имелось в виду, — исправь.
- Если слово непонятно — НЕ придумывай смысл: оставь его в названии как есть и поставь utility "routine".

Никакого текста кроме JSON."""


# Бесплатный Groq: ~1000 запросов в день и ~8000 токенов в минуту на каждую модель.
# QUOTA — что осталось по последнему ответу; LIMIT_HIT — во что упёрлись последний раз («day» / «minute»).
QUOTA: dict = {}
LIMIT_HIT: dict = {"kind": None, "at": 0.0, "primary_day": 0.0}


def _remember_quota(model: str, headers):
    try:
        QUOTA[model] = {"left": int(headers.get("x-ratelimit-remaining-requests")),
                        "limit": int(headers.get("x-ratelimit-limit-requests"))}
    except (TypeError, ValueError):
        pass


def _remember_limit(error: Exception):
    text = str(error).lower()
    LIMIT_HIT["kind"] = "day" if ("per day" in text or "rpd" in text or "tpd" in text) else "minute"
    LIMIT_HIT["at"] = time.time()


def limit_answer() -> str:
    if LIMIT_HIT["kind"] == "day":
        return ("⏳ На сегодня закончился бесплатный лимит нейросети — разбирать сообщения смогу завтра. "
                "Команды /summary, /todo, /reminders работают и сейчас.")
    return "⏳ Нейросеть просит передышку (бесплатный лимит в минуту). Пришли это ещё раз через пару минут."


def _call_groq(model: str, messages: list, use_json_format: bool = False) -> str:
    """Синхронный вызов Groq API (будет запущен в отдельном потоке)."""
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0,  # меньше случайности: одинаковые фразы — одинаковые ответы
        # gpt-oss сначала «думает», и эти токены входят в лимит — иначе ответ пустой
        "max_tokens": 4000,
        "reasoning_effort": "low",
    }
    if use_json_format:
        kwargs["response_format"] = {"type": "json_object"}

    raw = groq_client.chat.completions.with_raw_response.create(**kwargs)
    _remember_quota(model, raw.headers)
    response = raw.parse()
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


RHYTHM_PROMPT = """Ты — спокойный коуч по распорядку дня. Разбираешь записи пользователя в Telegram-трекере
и пишешь ему напрямую, на «ты». О пользователе: {about}

Записи есть за {days} дн. Время — местное. Говори о «днях с записями», не о неделях.

ПОВТОРЯЮЩИЕСЯ ДЕЛА (были в 2+ днях). Цифры уже посчитаны В СРЕДНЕМ ЗА ДЕНЬ, когда дело было, — не складывай и не умножай их:
{repeating}

РАЗОВЫЕ ДЕЛА (было 1 день) — только для фона, в расписание их НЕ включай:
{single}

Напиши по-русски, коротко, обычным текстом без markdown-таблиц и без символов * и #:
1. Ритм — 2–3 наблюдения с цифрами из сводки (когда ты продуктивнее, сколько в день уходит на телефон и когда).
2. Что съедает время — одно главное.
3. Расписание обычного дня: 5–8 строк вида «09:00–10:00 — …» ТОЛЬКО из повторяющихся дел и базовых вещей
   (сон, еда), опираясь на реальное время из сводки, а не на идеал из книжек. Залипание и прокрастинацию
   в расписание как цель не ставь — вместо них короткое окно отдыха.
4. Один маленький шаг на завтра."""

# Пока записей меньше 3 дней — никаких «ритмов» и расписаний: модель иначе выдумывает их по одному дню
FIRST_DAYS_PROMPT = """Ты — спокойный коуч по распорядку дня. Пользователь только начал вести Telegram-трекер:
записи есть всего за {days} дн. Пиши ему напрямую, на «ты». О пользователе: {about}

Дела за эти дни (время — местное):
{items}

Напиши по-русски, очень коротко, обычным текстом без символов * и #:
1. 2–3 наблюдения о том, как прошли эти дни, с цифрами из списка. Не называй это ритмом или привычками —
   это просто первые наблюдения. Не упоминай недели и не обобщай.
2. Один маленький шаг на завтра.
Расписание НЕ составляй."""


def _fmt_hour(hours: list) -> str:
    """Среднее время по кругу суток: 23:00 и 01:00 → 00:00, а не 12:00, как дало бы обычное среднее."""
    angles = [h / 24 * 2 * math.pi for h in hours]
    avg = math.atan2(sum(math.sin(a) for a in angles), sum(math.cos(a) for a in angles)) / (2 * math.pi) * 24 % 24
    minutes = int(round(avg * 60)) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def analyze_rhythm(stats: dict, days: int, about: str = "", period: int = 21) -> str:
    """Разбор ритма по сводке get_activity_stats. Возвращает готовый текст для Telegram."""
    if not stats:
        return "Пока нет записей за этот период — расскажи мне о паре дней, и я разберу твой ритм."

    repeating, single = [], []
    for title, s in sorted(stats.items(), key=lambda kv: -kv[1]["minutes"]):
        n_days = max(1, len(s["days"]))
        when = f", обычно начинается около {_fmt_hour(s['hours'])}" if s["hours"] else ""
        if n_days >= 2:
            repeating.append(f"- {title} [{s['utility']}, {s['category']}]: в {n_days} из {days} дн., "
                             f"в среднем {round(s['minutes'] / n_days)} мин в такой день{when}")
        else:
            single.append(f"- {title} [{s['utility']}]: {s['minutes']} мин{when}")

    about = about.strip() or "о себе не рассказывал(а) — пиши без рода: не «сделала/сделал»"
    if days < 3:
        items = [f"- {title} [{s['utility']}]: {s['minutes']} мин" + (f", около {_fmt_hour(s['hours'])}" if s["hours"] else "")
                 for title, s in sorted(stats.items(), key=lambda kv: -kv[1]["minutes"])]
        prompt = FIRST_DAYS_PROMPT.format(days=days, about=about, items="\n".join(items[:30]))
    else:
        prompt = RHYTHM_PROMPT.format(days=days, about=about,
                                      repeating="\n".join(repeating) or "пока нет",
                                      single="\n".join(single[:20]) or "нет")
    messages = [{"role": "user", "content": prompt}]
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            return (await asyncio.to_thread(_call_groq, model, messages, False)).replace("**", "").strip()
        except Exception as e:
            print(f"[analyze_rhythm] {model} упала: {e}")
    return "Не получилось сделать разбор — модель сейчас недоступна, попробуй через пару минут."


CATEGORIES = ["Работа", "Учёба", "Спорт", "Еда", "Быт", "Здоровье", "Сон", "Дорога", "Общение", "Отдых", "Телефон", "Другое"]


WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

EMPTY_RESULT = {
    "intent": "log", "answer": "", "activities": [], "tasks_to_add": [],
    "habit_identified": None, "reminders": [], "edits": [], "encouragement": "",
}


async def _ask(messages: list):
    """Сырой ответ модели: основная с json_object → основная без него → резервная. None, если все упали."""
    limited_models = set()
    for model, as_json in ((PRIMARY_MODEL, True), (PRIMARY_MODEL, False), (FALLBACK_MODEL, False)):
        if model in limited_models:
            continue  # упёрлись в лимит этой модели — сразу к запасной (у неё свой лимит)
        try:
            raw = await asyncio.to_thread(_call_groq, model, messages, as_json)
            print(f"[ai_parser] {model}{'' if as_json else ' (без json)'} ответил: {raw[:200]}")
            LIMIT_HIT["kind"] = None
            return raw
        except groq.RateLimitError as e:
            print(f"[ai_parser] {model}: лимит Groq: {e}")
            _remember_limit(e)
            limited_models.add(model)
            if model == PRIMARY_MODEL and LIMIT_HIT["kind"] == "day":
                LIMIT_HIT["primary_day"] = time.time()  # основная на сегодня всё — дальше работаем на запасной
        except Exception as e:
            print(f"[ai_parser] {model}{'' if as_json else ' (без json)'} упал: {e}")
    return None


def _load(raw: str):
    try:
        data = json.loads(_clean_json(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # Если ответ вложен внутрь другого ключа ({"result": {...}})
    if "activities" not in data and "intent" not in data:
        for v in data.values():
            if isinstance(v, dict) and ("activities" in v or "intent" in v):
                return v
    return data


AGO_RE = re.compile(r"\bназад\b|только что", re.I)
RANGE_RE = re.compile(r"\bс\s+\d{1,2}([:.]\d{2})?\s+(до|по)\s+\d{1,2}", re.I)
REGULAR_RE = re.compile(r"кажд|ежедневн|по утрам|по вечерам", re.I)


def _problems(data: dict, text: str) -> list[str]:
    """Проверка ответа модели по самому сообщению. Нашлось что-то — переспрашиваем один раз."""
    problems = []
    acts = [a for a in data.get("activities") or [] if isinstance(a, dict)]
    rems = [r for r in data.get("reminders") or [] if isinstance(r, dict)]
    if acts and AGO_RE.search(text) and not any(a.get("ended_ago_min") is not None or a.get("end") or a.get("start") for a in acts):
        problems.append("В сообщении сказано, сколько времени назад это закончилось, — заполни ended_ago_min.")
    if acts and RANGE_RE.search(text) and not any(a.get("start") for a in acts):
        problems.append("В сообщении названо время «с … до …» — заполни start и end (ЧЧ:ММ).")
    if REGULAR_RE.search(text) and any(str(r.get("repeat", "")).lower() == "once" for r in rems):
        problems.append("Просят напоминать регулярно — у такого напоминания repeat \"daily\".")
    if text.strip().endswith("?") and data.get("intent") == "log" and not data.get("answer"):
        problems.append("Это вопрос: intent \"question\", ответ в answer.")
    for a in acts:
        if any(k in a for k in ("start_time", "end_time")) and not any(k in a for k in ("start", "end", "ended_ago_min", "part_of_day")):
            problems.append("Время дела не вычисляй сам: верни поля day / start / end / duration_min / ended_ago_min / part_of_day.")
            break
    return problems


async def parse_voice_text(text: str, known_titles: list | None = None, about: str = "", context: str = "") -> dict:
    """about — что человек рассказал о себе (/about); context — его день: записи, задачи, напоминания,
    предыдущее сообщение (с номерами, чтобы модель могла отвечать на вопросы и вносить поправки)."""
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    today_date = now.strftime("%Y-%m-%d")

    prompt = SYSTEM_PROMPT.format(
        now_str=now.strftime("%Y-%m-%d %H:%M:%S"),
        weekday=WEEKDAYS[now.weekday()],
        today_date=today_date,
        tomorrow_date=(now + timedelta(days=1)).strftime("%Y-%m-%d"),
        current_time=now.strftime("%H:%M"),
        tz=config.TIMEZONE,
        about=about.strip() or "о себе не рассказывал(а). Обращайся нейтрально, без рода: не «сделала/сделал», а «готово», «записано».",
        context=context.strip() or "пока ничего нет",
        categories=", ".join(CATEGORIES),
        canonical_titles=timeline.CANONICAL_HINT,
        known_titles=", ".join(f"«{t}»" for t in known_titles) if known_titles else "пока нет",
    )
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": text}]

    raw = await _ask(messages)
    if raw is None:
        print("[ai_parser] Все модели упали, возвращаю заглушку.")
        if LIMIT_HIT["kind"]:
            return {**EMPTY_RESULT, "intent": "chat", "answer": limit_answer(), "rate_limited": LIMIT_HIT["kind"]}
        return {**EMPTY_RESULT, "intent": "chat",
                "answer": "Не получилось разобрать — модель сейчас недоступна. Попробуй ещё раз через минуту."}
    data = _load(raw)

    # Проверка и одна попытка исправить: модель от раза к разу ошибается по-разному
    problems = _problems(data, text) if data is not None else ["Ответ — не JSON. Верни только JSON в нужном формате."]
    if problems:
        print(f"[ai_parser] переспрашиваю: {problems}")
        retry = messages + [{"role": "assistant", "content": raw},
                            {"role": "user", "content": "Проверь свой ответ:\n- " + "\n- ".join(problems) +
                             "\nВерни исправленный JSON целиком, без пояснений."}]
        raw2 = await _ask(retry)
        data2 = _load(raw2) if raw2 else None
        if data2 is not None and (data is None or len(_problems(data2, text)) < len(problems)):
            data = data2

    if data is None:
        return {**EMPTY_RESULT, "intent": "chat",
                "answer": "Сообщение дошло, но разобрать его не вышло. Попробуй ещё раз — иногда нужна пара попыток."}
    return _normalize(data, text, now)


def _normalize(data: dict, text: str, now: datetime) -> dict:
    today_date = now.strftime("%Y-%m-%d")
    for key, default in EMPTY_RESULT.items():
        if data.get(key) is None:
            data[key] = [] if isinstance(default, list) else default
    for key in ("activities", "tasks_to_add", "reminders", "edits"):
        if not isinstance(data[key], list):
            data[key] = []

    # Категория — только из фиксированного списка, иначе /summary дробится на «залипание в телефоне», «телефон», …
    lower_map = {c.lower(): c for c in CATEGORIES}
    data["activities"] = [a for a in data["activities"] if isinstance(a, dict)]
    for act in data["activities"]:
        act["category"] = lower_map.get(str(act.get("category", "")).strip().lower(), "Другое")
        timeline.normalize_title(act)          # «Поела» → «Еда», «зал» → «Тренировка»
        timeline.refine_meal(act, text)        # «Еда», а в сообщении «позавтракала» → «Завтрак»
    timeline.resolve_times(data["activities"], now.replace(tzinfo=None))   # время считает код
    data["activities"].sort(key=lambda a: a["start_time"])

    # Модель иногда присылает задачи/привычку объектами — в БД нужны строки
    data["tasks_to_add"] = _clean_tasks(data.get("tasks_to_add"), now)
    data["habit_identified"] = _as_text(data.get("habit_identified")) or None
    data["reminders"] = _clean_reminders(data.get("reminders"), today_date, text)
    data["edits"] = _clean_edits(data.get("edits"))
    data["intent"] = data["intent"] if data["intent"] in ("log", "question", "correction", "chat") else "log"
    data["answer"] = _as_text(data.get("answer")).replace("**", "")
    data["encouragement"] = _as_text(data.get("encouragement")) if data["intent"] == "log" else ""
    return data


def _clean_tasks(raw, now: datetime) -> list:
    """[{text, due}]: due — ГГГГ-ММ-ДД или None. Модель иногда присылает строки — тоже принимаем."""
    result = []
    for item in raw or []:
        text = _as_text(item)
        if not text:
            continue
        due = item.get("due") if isinstance(item, dict) else None
        due = str(due or "").strip().lower()
        if due == "today":
            due = now.strftime("%Y-%m-%d")
        elif due == "tomorrow":
            due = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            due = _clean_date(due)
        result.append({"text": text[0].upper() + text[1:], "due": due})
    return result


def _clean_time(value):
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(value).strip()) if value else None
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 else None


def _clean_date(value):
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


EVENING_RE = re.compile(r"вечер", re.I)
MORNING_RE = re.compile(r"утр", re.I)


def _evening_fix(time, source_text):
    """«Каждый вечер в 10» модель иногда отдаёт как 10:00. Если речь о вечере, а не об утре, — переводим в 22:00."""
    if time and time != "meal" and EVENING_RE.search(source_text or "") and not MORNING_RE.search(source_text or ""):
        hour, minute = map(int, time.split(":"))
        if 1 <= hour <= 11:
            return f"{hour + 12:02d}:{minute:02d}"
    return time


def _clean_reminders(raw, today_date, source_text="") -> list:
    """[{text, time, date}]: time — ЧЧ:ММ, 'meal' или None; date — ГГГГ-ММ-ДД для разовых, None для ежедневных."""
    result = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = _as_text(item)
        if not text:
            continue
        time = "meal" if str(item.get("time")).strip().lower() == "meal" else _clean_time(item.get("time"))
        time = _evening_fix(time, source_text)
        once = str(item.get("repeat", "")).lower() == "once" and time != "meal"
        date = (_clean_date(item.get("date")) or today_date) if once else None
        # Время в тексте («В 22:00 читать») модель иногда дописывает сама — убираем, бот добавит его, где нужно
        text = TIME_PREFIX_RE.sub("", text).strip() or text
        if once and time and str(item.get("kind", "")).lower() == "event":
            # Встреча: напоминаем за 30 минут, а время события пишем в текст
            event_at = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            remind_at = event_at - timedelta(minutes=EVENT_LEAD_MIN)
            text = f"В {time} {text[0].lower() + text[1:]}"
            date, time = remind_at.strftime("%Y-%m-%d"), remind_at.strftime("%H:%M")
        result.append({"text": text[0].upper() + text[1:], "time": time, "date": date})
    return result


TIME_PREFIX_RE = re.compile(r"^(в|к)\s*\d{1,2}[:.]\d{2}\s*[—–-]?\s*", re.I)
EVENT_LEAD_MIN = 30


EDIT_OPS = {"reminder_once", "reminder_daily", "reminder_time", "reminder_delete",
            "activity_update", "activity_delete", "task_delete"}


def _clean_edits(raw) -> list:
    result = []
    for item in raw or []:
        if not isinstance(item, dict) or item.get("op") not in EDIT_OPS:
            continue
        try:
            item["id"] = int(str(item.get("id")).lstrip("#"))
        except (TypeError, ValueError):
            continue
        if item["op"] == "reminder_time":
            item["time"] = _clean_time(item.get("time"))
            if not item["time"]:
                continue
        if item["op"] == "reminder_once":
            item["date"] = _clean_date(item.get("date"))
        result.append(item)
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