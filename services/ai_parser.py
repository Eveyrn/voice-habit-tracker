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

# Подсказка разбита на части, чтобы не платить токенами за ненужное:
# STATIC_PROMPT — всегда и одинаковая (идёт первой: так её могут кэшировать), EDITS_PROMPT — только когда
# сообщение похоже на поправку/вопрос, DYNAMIC_PROMPT — время, «о себе» и данные пользователя.
STATIC_PROMPT_TEMPLATE = """Ты разбираешь сообщения в Telegram-трекере дня и возвращаешь ТОЛЬКО JSON.

intent:
- "log" — что делал(а), планы, задачи, просьбы напомнить;
- "question" — вопрос о своих делах и планах («что у меня сегодня?»): ответ в answer строго по ДАННЫМ, без выдумок
  и без номеров #;
  если ответа в данных нет — так и скажи; ничего не создавай. Про период шире имеющихся дней — считай по тем, что есть,
  и назови их («записи есть только за сегодня и вчера: …»);
- "correction" — поправка к уже записанному («не каждый день, только сегодня», «нет, до 11», «удали это») → edits,
  короткое подтверждение в answer;
- "chat" — реплика без данных («спасибо») → короткий ответ в answer.
К пользователю — на «ты»; о себе — в мужском роде («записал»). Для "log" answer = "".

Формат:
{{"intent": "log", "answer": "",
 "activities": [{{"title": "Тренировка", "category": "Спорт", "utility": "productive", "day": "today",
   "start": null, "end": null, "duration_min": 90, "ended_ago_min": 60, "part_of_day": null}}],
 "tasks_to_add": [{{"text": "Купить корм коту", "due": "tomorrow"}}],
 "habit_identified": null,
 "reminders": [{{"text": "Быть у мамы", "time": "15:00", "repeat": "once", "date": "ГГГГ-ММ-ДД", "kind": "event"}}],
 "edits": [], "encouragement": ""}}

ДЕЛА — только то, что уже было; будущее («потом пойду работать») — не дело.
- utility: productive — работа, учёба, спорт, чтение, проекты; routine — быт, еда, сон, гигиена, дорога, покупки,
  общение с близкими; unproductive — залипание в телефоне, бесцельные соцсети, сериалы и игры, прокрастинация.
- category — одно из: {categories}.
- title — коротко, существительным: {canonical}. Если по смыслу совпадает с известным делом пользователя — ТОЧНО
  его название. Одновременные действия («встретилась с мамой, купили куртки») — одно дело.
- Время НЕ вычисляй, только перенеси сказанное: day — "today" / "yesterday" / "ГГГГ-ММ-ДД"; start, end — "ЧЧ:ММ"
  (24 ч), если названы, или время связанного события из ДАННЫХ; duration_min — если длительность сказана или очевидна;
  ended_ago_min — «час назад закончила» → 60, «только что» → 0; part_of_day — "morning" / "day" / "evening" / "night",
  если сказано только «утром» / «днём» / «вечером» / «ночью». Ничего не сказано — null.
Примеры:
«с 8 до 10 сидела в телефоне» → "Залипание в телефоне", start "08:00", end "10:00"
«час назад закончила тренировку, шла полтора часа» → ended_ago_min 60, duration_min 90
«утром ходила в зал» → "Тренировка", part_of_day "morning"
«вчера весь вечер смотрела сериал» → "Сериал", day "yesterday", part_of_day "evening", duration_min 180
«позавтракала, потом 2 часа работала над ботом» → "Завтрак" duration_min 20 и "Работа над ботом" duration_min 120

ЗАДАЧИ — дела на будущее без точного времени, каждое отдельно: {{"text", "due"}}; due — "today" / "tomorrow" /
"ГГГГ-ММ-ДД" или null; text без слов про день. «завтра купить корм коту, сегодня позвонить в банк и когда-нибудь
помыть машину» → 3 задачи (tomorrow, today, null).

ПРИВЫЧКИ — сделал(а) то, на что есть ЕЖЕДНЕВНОЕ напоминание в ДАННЫХ («выпила витамины» ↔ «Выпить витамины»):
habit_identified = ТОЧНЫЙ текст напоминания, в activities не добавлять. Иначе null.

НАПОМИНАНИЯ:
- разовое: repeat "once", date. kind "event" — дело в своё время («сегодня к 3 быть у мамы», «завтра в 9 к врачу»):
  time — время события, text без времени. kind "reminder" — «напомни в 15:00 позвонить маме»: time — этот момент;
- ежедневное: repeat "daily", date null — только если просят регулярно («каждый день», «каждое утро»,
  «напоминай пить витамины»); text без времени;
- после еды: repeat "daily", time "meal"; время неясно — time null;
- время в 24 ч: «каждый вечер в 10» → "22:00". Что в напоминаниях — не дублируй в задачах.

encouragement — одна короткая живая фраза, только для "log".
Текст бывает расшифровкой голоса: явно искажённое слово исправь по смыслу, непонятное оставь как есть (utility "routine")."""

EDITS_PROMPT = """ПОПРАВКИ (edits) — только к элементам с номерами # из ДАННЫХ; «это», «то напоминание» — обычно из
ПРЕДЫДУЩЕГО СООБЩЕНИЯ. Варианты:
{"op": "reminder_once", "id": N, "date": "ГГГГ-ММ-ДД"} | {"op": "reminder_daily", "id": N} |
{"op": "reminder_time", "id": N, "time": "ЧЧ:ММ"} | {"op": "reminder_delete", "id": N} |
{"op": "activity_update", "id": N, "title": "…", "start": "ЧЧ:ММ", "end": "ЧЧ:ММ", "utility": "…", "category": "…"}
(только изменённые поля) | {"op": "activity_delete", "id": N} | {"op": "task_delete", "id": N}"""

DYNAMIC_PROMPT = """Сейчас: {now_str}, {weekday} (сегодня {today}, завтра {tomorrow}), часовой пояс {tz}.{night}
Даты дней недели (бери отсюда, не считай): {week}.
О пользователе: {about}
Известные дела пользователя: {known_titles}
ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
{context}"""


# Бесплатный Groq: ~1000 запросов в день и ~8000 токенов в минуту на каждую модель.
# QUOTA — что осталось по последнему ответу; LIMIT_HIT — во что упёрлись последний раз («day» / «minute»).
QUOTA: dict = {}
LAST_USAGE: dict = {}
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
    usage = getattr(response, "usage", None)
    if usage:
        LAST_USAGE.update(model=model, prompt=usage.prompt_tokens, completion=usage.completion_tokens,
                          total=usage.total_tokens)
        print(f"[ai_parser] токены {model}: вход {usage.prompt_tokens}, ответ {usage.completion_tokens}")
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


ABOUT_PROMPT = """Сожми рассказ человека о себе в заметку для трекера дня: 1–2 предложения, до 300 символов, в третьем
лице и в его роде («Разработчица и художница…»). Оставь: чем занимается, что для него полезно (работа, учёба, цели),
увлечения. Только текст заметки, без кавычек.

Рассказ: {text}"""
ABOUT_MAX = 300


async def condense_about(text: str) -> str:
    """«О себе» уходит в каждый запрос к модели — длинный рассказ сжимаем (один раз, при сохранении)."""
    text = text.strip()
    if len(text) <= ABOUT_MAX:
        return text
    messages = [{"role": "user", "content": ABOUT_PROMPT.format(text=text[:3000])}]
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            short = (await asyncio.to_thread(_call_groq, model, messages, False)).replace("**", "").strip().strip("«»\"")
            if short:
                return short[:ABOUT_MAX + 100]
        except Exception as e:
            print(f"[condense_about] {model} упала: {e}")
    cut = text[:ABOUT_MAX]
    return cut[:cut.rfind(".") + 1] if "." in cut else cut


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
# Что модель писала в category до фиксированного списка (в старых записях это осталось)
CATEGORY_ALIASES = {"развлечение": "Отдых", "развлечения": "Отдых", "игры": "Отдых", "досуг": "Отдых",
                    "залипание в телефоне": "Телефон", "соцсети": "Телефон", "учеба": "Учёба", "покупки": "Быт",
                    "семья": "Общение", "друзья": "Общение", "гигиена": "Быт", "уборка": "Быт", "питание": "Еда"}


def clean_category(value) -> str:
    """Категория — только из фиксированного списка, иначе /summary дробится на «залипание в телефоне», «телефон», …"""
    low = str(value or "").strip().lower()
    return {c.lower(): c for c in CATEGORIES}.get(low) or CATEGORY_ALIASES.get(low) or "Другое"


STATIC_PROMPT = STATIC_PROMPT_TEMPLATE.format(categories=", ".join(CATEGORIES), canonical=timeline.CANONICAL_HINT)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

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
CLOCK_RE = re.compile(r"(^|\s)(в|до|к|с|около)\s+\d{1,2}([:.]\d{2})?(\s|$|[,.!?])", re.I)


def _size(data: dict) -> int:
    """Сколько всего разобрано — чтобы переспрос не заменил полный ответ пустым."""
    return sum(len(data.get(k) or []) for k in ("activities", "tasks_to_add", "reminders", "edits")) + \
        bool(data.get("habit_identified")) + bool(data.get("answer"))


def _problems(data: dict, text: str) -> list[str]:
    """Проверка ответа модели по самому сообщению. Нашлось что-то — переспрашиваем один раз."""
    problems = []
    acts = [a for a in data.get("activities") or [] if isinstance(a, dict)]
    rems = [r for r in data.get("reminders") or [] if isinstance(r, dict)]
    if acts and AGO_RE.search(text) and not any(a.get("ended_ago_min") is not None or a.get("end") or a.get("start") for a in acts):
        problems.append("В сообщении сказано, сколько времени назад это закончилось, — заполни ended_ago_min.")
    if acts and RANGE_RE.search(text) and not any(a.get("start") for a in acts):
        problems.append("В сообщении названо время «с … до …» — заполни start и end (ЧЧ:ММ).")
    elif acts and not rems and CLOCK_RE.search(text) and not any(a.get("start") or a.get("end") for a in acts):
        problems.append("В сообщении названо время («в 23», «до 0:58») — перенеси его в start / end дел (ЧЧ:ММ).")
    if REGULAR_RE.search(text) and rems and not any(str(r.get("repeat", "")).lower() == "daily" for r in rems):
        problems.append("Что просят делать регулярно («каждый день …») — напоминание с repeat \"daily\" (время не сказано — "
                        "time null); разовые оставь разовыми.")
    # «С подругой встретимся в 3–4» модель иногда кладёт в задачи без времени — а время названо
    rem_texts = " ".join(_as_text(r) for r in rems).lower()
    for task in data.get("tasks_to_add") or []:
        t_text = _as_text(task)
        if t_text and any(CLOCK_IN_CLAUSE_RE.search(c) for c in timeline._clauses(text) if timeline._mentions({"title": t_text}, c)) \
                and not timeline._mentions({"title": t_text}, rem_texts):
            problems.append(f"«{t_text}» — в сообщении названо время: это напоминание (kind \"event\", time — "
                            "начало, если сказано «в 3–4» — 15:00), а не задача.")
    # «Скорее всего встретимся в 3–4 сегодня» модель иногда просто выбрасывает — из-за «скорее всего»
    items = [_as_text(x) for k in ("activities", "tasks_to_add", "reminders") for x in data.get(k) or []]
    for clause in timeline._clauses(text):
        if CLOCK_IN_CLAUSE_RE.search(clause) and PLAN_WORDS_RE.search(clause) \
                and not any(timeline._mentions({"title": item}, clause) for item in items if item):
            problems.append(f"В сообщении есть план со временем: «{clause.strip()}» — добавь напоминание (kind \"event\"); "
                            "даже если время неточное («в 3–4», «скорее всего») — поставь начало, например 15:00.")
    if _size(data) == 0 and len(text) > 40:
        problems.append("Ответ пустой, а в сообщении есть дела, планы или задачи — разбери их.")
    if text.strip().endswith("?") and data.get("intent") == "log" and not data.get("answer"):
        problems.append("Это вопрос: intent \"question\", ответ в answer.")
    for a in acts:
        if any(k in a for k in ("start_time", "end_time")) and not any(k in a for k in ("start", "end", "ended_ago_min", "part_of_day")):
            problems.append("Время дела не вычисляй сам: верни поля day / start / end / duration_min / ended_ago_min / part_of_day.")
            break
    return problems


async def parse_voice_text(text: str, known_titles: list | None = None, about: str = "", context: str = "",
                           with_edits: bool = True) -> dict:
    """about — что человек рассказал о себе (/about); context — его день: записи, задачи, напоминания,
    предыдущее сообщение (с номерами, чтобы модель могла отвечать на вопросы и вносить поправки)."""
    tz = pytz.timezone(config.TIMEZONE)
    now = datetime.now(tz)
    today_date = now.strftime("%Y-%m-%d")

    dynamic = DYNAMIC_PROMPT.format(
        now_str=now.strftime("%Y-%m-%d %H:%M"),
        weekday=WEEKDAYS[now.weekday()],
        today=today_date,
        tomorrow=logical_tomorrow(now).strftime("%Y-%m-%d"),
        night=(f"\nСейчас ночь, человек ещё не спал: его «завтра» — это {today_date} (день после сна)."
               if now.hour < NIGHT_END_HOUR else ""),
        # Модель путает, какое число будет в пятницу, — даём готовый календарь на неделю вперёд
        week=", ".join(f"{WEEKDAYS_SHORT[d.weekday()]} {d.strftime('%Y-%m-%d')}"
                       for d in (now + timedelta(days=i) for i in range(1, 8))),
        tz=config.TIMEZONE,
        about=about.strip() or "о себе не рассказывал(а) — обращайся без рода: не «сделала/сделал», а «готово», «записано»",
        known_titles=", ".join(f"«{t}»" for t in known_titles) if known_titles else "пока нет",
        context=context.strip() or "пока ничего нет",
    )
    system = STATIC_PROMPT + ("\n\n" + EDITS_PROMPT if with_edits else "")
    messages = [{"role": "system", "content": system},
                {"role": "system", "content": dynamic},
                {"role": "user", "content": text}]

    raw = await _ask(messages)
    if raw is None:
        print("[ai_parser] Все модели упали, возвращаю заглушку.")
        if LIMIT_HIT["kind"]:
            return {**EMPTY_RESULT, "intent": "chat", "answer": limit_answer(), "rate_limited": LIMIT_HIT["kind"]}
        return {**EMPTY_RESULT, "intent": "chat",
                "answer": "Не получилось разобрать — модель сейчас недоступна. Попробуй ещё раз через минуту."}
    data = _load(raw)

    # Проверка и одна попытка исправить: модель от раза к разу ошибается по-разному
    problems = list(dict.fromkeys(_problems(data, text))) if data is not None else \
        ["Ответ — не JSON. Верни только JSON в нужном формате."]
    if problems:
        print(f"[ai_parser] переспрашиваю: {problems}")
        retry = messages + [{"role": "assistant", "content": raw},
                            {"role": "user", "content": "Проверь свой ответ:\n- " + "\n- ".join(problems) +
                             "\nВерни исправленный JSON целиком, без пояснений."}]
        raw2 = await _ask(retry)
        data2 = _load(raw2) if raw2 else None
        # Исправленный ответ берём, только если он ничего не потерял: 20.10 переспрос вернул пустой JSON
        # («ошибок меньше») — и бот ответил «не нашёл здесь дел» на сообщение из шести дел
        # Чуть меньше элементов — можно (две задачи «встреча» стали одним напоминанием), пусто или сильно меньше — нет
        if data2 is not None and (data is None or (len(set(_problems(data2, text))) < len(problems)
                                                   and _size(data2) > 0 and _size(data2) >= _size(data) - len(problems))):
            # Переспрашиваем про задачи и напоминания — а дела модель при этом иногда теряет: берём их из первого ответа
            if data is not None and len(data2.get("activities") or []) < len(data.get("activities") or []):
                data2["activities"] = data["activities"]
            data = data2

    if data is None:
        return {**EMPTY_RESULT, "intent": "chat",
                "answer": "Сообщение дошло, но разобрать его не вышло. Попробуй ещё раз — иногда нужна пара попыток."}
    return _normalize(data, text, now)


SERVICE_ID_RE = re.compile(r"(?<![\d:.])(?:#\d+:?\s*|\b\d{2,6}\s*—\s*(?=[А-ЯЁA-Z«]))")


def _normalize(data: dict, text: str, now: datetime) -> dict:
    today_date = now.strftime("%Y-%m-%d")
    for key, default in EMPTY_RESULT.items():
        if data.get(key) is None:
            data[key] = [] if isinstance(default, list) else default
    for key in ("activities", "tasks_to_add", "reminders", "edits"):
        if not isinstance(data[key], list):
            data[key] = []

    data["activities"] = [a for a in data["activities"] if isinstance(a, dict)]
    for act in data["activities"]:
        act["category"] = clean_category(act.get("category"))
        timeline.normalize_title(act)          # «Поела» → «Еда», «зал» → «Тренировка»
        timeline.refine_meal(act, text)        # «Еда», а в сообщении «позавтракала» → «Завтрак»
    data["planned"] = timeline.split_plans(data["activities"], text)  # «буду работать» — план, не сделанное
    timeline.mark_ongoing(data["activities"], text)  # «сейчас завтракаю» — до «сейчас», а не «утром»
    timeline.resolve_times(data["activities"], now.replace(tzinfo=None))   # время считает код
    data["activities"].sort(key=lambda a: a["start_time"])

    # Модель иногда присылает задачи/привычку объектами — в БД нужны строки
    tasks = _clean_tasks(data.get("tasks_to_add"), now)
    data["habit_identified"] = _as_text(data.get("habit_identified")) or None
    data["reminders"] = _clean_reminders(data.get("reminders"), now, text)
    # Разовое «напоминание» без времени — это задача на тот день («сегодня решить, что отдать тёте»),
    # если человек сам не просил напомнить
    if not NAG_RE.search(text):
        for rem in [r for r in data["reminders"] if r["date"] and not r["time"]]:
            data["reminders"].remove(rem)
            tasks.append({"text": rem["text"], "due": rem["date"]})
    data["tasks_to_add"], data["choices"] = _collapse_choices(_dedupe_tasks(tasks), text, now)
    _mark_tentative(data["reminders"], text)
    data["edits"] = _clean_edits(data.get("edits"))
    data["intent"] = data["intent"] if data["intent"] in ("log", "question", "correction", "chat") else "log"
    # Номера из ДАННЫХ — служебные: «Сегодня открыты задачи: 24 — Работать» (21.10) человеку ни к чему
    data["answer"] = SERVICE_ID_RE.sub("", _as_text(data.get("answer")).replace("**", ""))
    data["encouragement"] = _as_text(data.get("encouragement")) if data["intent"] == "log" else ""
    return data


NAG_RE = re.compile(r"напомн|напоминай", re.I)
# Еда и подъём — не задачи: «позавтракать (завтра)» только засоряет /todo
ROUTINE_TASK_RE = re.compile(r"^(по)?(завтракать|обедать|ужинать|есть|кушать|перекусить)$|^(пораньше\s+)?встать(\s+пораньше)?$", re.I)


NIGHT_END_HOUR = 5  # до 5 утра человек обычно ещё не спал: его «завтра» — это уже наступивший календарный день


def logical_tomorrow(now: datetime):
    """«Завтра убраться», сказанное в 01:01, — это сегодняшнее число (после сна), а не послезавтра.
    21.10 бот поставил такие задачи на 22.10."""
    return now.date() if now.hour < NIGHT_END_HOUR else now.date() + timedelta(days=1)


def _resolve_date(value, now: datetime):
    """today / tomorrow / thu / ГГГГ-ММ-ДД → ГГГГ-ММ-ДД (дни недели — ближайший такой день, сегодня включительно)."""
    v = str(value or "").strip().lower()
    if v == "today":
        return now.strftime("%Y-%m-%d")
    if v == "tomorrow":
        return logical_tomorrow(now).strftime("%Y-%m-%d")
    weekday = timeline.weekday_date(v, now.date())
    return weekday.strftime("%Y-%m-%d") if weekday else _clean_date(v)


def _clean_tasks(raw, now: datetime) -> list:
    """[{text, due}]: due — ГГГГ-ММ-ДД или None. Модель иногда присылает строки — тоже принимаем."""
    result = []
    for item in raw or []:
        text = _as_text(item)
        if not text or ROUTINE_TASK_RE.match(text.strip().rstrip(".")):
            continue
        due = _resolve_date(item.get("due") if isinstance(item, dict) else None, now)
        result.append({"text": text[0].upper() + text[1:], "due": due})
    return result


def _dedupe_tasks(tasks: list) -> list:
    """«Купить корм либо сегодня, либо завтра, либо в пятницу» модель делает тремя задачами — оставляем одну
    с самым поздним сроком (это и есть крайний срок)."""
    result = {}
    for t in tasks:
        key = t["text"].lower()
        if key not in result or (t["due"] or "") > (result[key]["due"] or ""):
            result[key] = t
    return list(result.values())


CHOICE_RE = re.compile(r"\bлибо\b|\bили\b", re.I)
UNSURE_RE = re.compile(r"не знаю|точно не|скорее всего|наверн|может быть|\bможет\b|возможно|посмотрю|как получится", re.I)
PLAN_WORDS_RE = re.compile(r"\b(завтра|сегодня|в (понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)|"
                           r"встретимся|увидимся|созвонимся|буду|будем|пойду|поеду|надо|нужно)\b", re.I)
CLOCK_IN_CLAUSE_RE =re.compile(r"\b(в|к|около)\s+\d{1,2}([:.]\d{2})?(\s*[-–]\s*\d{1,2})?\b", re.I)
TENTATIVE_RE = re.compile(r"скорее всего|наверн|может быть|возможно|вроде бы|\bлибо\b|\bили\b|не знаю|не уверен|"
                          r"как получится|если получится|пока не ясно|\d{1,2}\s*[-–]\s*\d{1,2}\s*(вечера|утра|дня|ч)", re.I)


def _collapse_choices(tasks: list, text: str, now: datetime):
    """«Буду работать либо над ELGE, либо над работой, точно не знаю» модель делает двумя задачами на сегодня —
    а это выбор, что делать дальше, не два дела. На сегодня — не задачи, а строка «Дальше: … или …»;
    на другой день — одна задача «… или …». Возвращает (задачи, выборы)."""
    choices = []
    today = now.strftime("%Y-%m-%d")
    for clause in timeline._clauses(text):
        if not CHOICE_RE.search(clause):
            continue
        group = [t for t in tasks if timeline._mentions({"title": t["text"]}, clause, first_word=True)]
        # Модель иногда сама склеивает выбор в одну задачу: «Работать над ботом или учёбой»
        if len(group) < 2 and not (group and CHOICE_RE.search(group[0]["text"])):
            continue
        joined = " или ".join([group[0]["text"]] + [t["text"][0].lower() + t["text"][1:] for t in group[1:]])
        tasks = [t for t in tasks if t not in group]
        due = max((t["due"] or "") for t in group) or None
        # «Точно не знаю» на сегодня — это просто «что дальше», а «купить молоко или кефир» — всё же задача
        if (due or today) <= today and UNSURE_RE.search(clause):
            choices.append(joined)
        else:
            tasks.append({"text": joined, "due": due})
    return tasks, choices


def _mark_tentative(reminders: list, text: str):
    """«Скорее всего встретимся в 3–4, либо в пятницу» — время не решено: такое напоминание помечаем
    tentative, бот не ставит его сразу, а позже спрашивает, договорились ли."""
    if NAG_RE.search(text):
        return  # «напомни …» — человек сам попросил, ставим как сказано
    clauses = timeline._clauses(text)
    for rem in reminders:
        if not rem.get("date") or not rem.get("time"):
            continue
        base = TIME_PREFIX_RE.sub("", rem["text"])
        if any(TENTATIVE_RE.search(c) for c in clauses if timeline._mentions({"title": base}, c)):
            rem["tentative"] = True


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


def _clean_reminders(raw, now: datetime, source_text="") -> list:
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
        date = (_resolve_date(item.get("date"), now) or now.strftime("%Y-%m-%d")) if once else None
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