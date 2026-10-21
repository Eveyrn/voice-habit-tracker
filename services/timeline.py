"""Время и названия дел считает код, а не модель.

Модель только переносит сказанное: «с 8 до 10», «час назад», «полтора часа», «утром». Арифметику
(«сейчас 20:46 − 60 − 90 = 18:16») языковая модель делает по-разному от раза к разу, а этот код — всегда одинаково.
"""
import re
from datetime import datetime, timedelta, date as date_cls

DEFAULT_MIN = 30
PART_OF_DAY = {"morning": (8, 0, 60), "day": (13, 0, 60), "evening": (19, 0, 120), "night": (23, 0, 60)}

# Справочник: как бы человек ни сказал, обычные дела называются одинаково — иначе статистика рассыпается
# («Поела» / «Еда» / «Покушала» стали бы тремя разными делами). (шаблон, название, категория, польза или None)
CANONICAL = [
    (r"завтрак", "Завтрак", "Еда", "routine"),
    (r"обед", "Обед", "Еда", "routine"),
    (r"ужин", "Ужин", "Еда", "routine"),
    (r"^(по)?ел[аи]?\b|^еда$|покушал|^перекус|^приём пищи|^прием пищи", "Еда", "Еда", "routine"),
    (r"^(по)?спал|^сон$|^дневной сон", "Сон", "Сон", "routine"),
    (r"трениров|спортзал|^зал$|^в зал|фитнес|качалк|^заняти[яе] спорт|^спорт$", "Тренировка", "Спорт", "productive"),
    (r"залип|в телефоне|соцсет|тикток|tiktok|инстаграм|instagram|рилс|reels|ленту|шортс", "Залипание в телефоне", "Телефон", "unproductive"),
    (r"сериал", "Сериал", "Отдых", None),
    (r"^игр|^играл|^поиграл", "Игры", "Отдых", None),
    (r"прогулк|^гулял|^погулял", "Прогулка", "Отдых", None),
    (r"^дорога|^ехал|^поездк|такси|автобус|метро", "Дорога", "Дорога", "routine"),
    (r"^душ|гигиен|^умыл", "Гигиена", "Быт", "routine"),
    (r"^уборк|^убирал|^прибрал", "Уборка", "Быт", "routine"),
    (r"^готовил|^готовк|^приготовил", "Готовка", "Быт", "routine"),
]
CANONICAL_HINT = "Еда (или Завтрак / Обед / Ужин), Сон, Тренировка, Прогулка, Залипание в телефоне, Сериал, Игры, Дорога, Гигиена, Уборка, Готовка"


def normalize_title(act: dict):
    # Подробности в скобках («Завтрак (гречка)») дробят статистику на разные дела — убираем
    title = re.sub(r"\s*\([^)]*\)\s*$", "", (act.get("title") or "").strip()) or (act.get("title") or "").strip()
    low = title.lower()
    for pattern, name, category, utility in CANONICAL:
        if re.search(pattern, low):
            act["title"], act["category"] = name, category
            if utility:
                act["utility"] = utility
            return
    if title:
        act["title"] = title[0].upper() + title[1:]


MEALS = [(r"завтрак", "Завтрак"), (r"обед", "Обед"), (r"ужин", "Ужин")]


def refine_meal(act: dict, text: str):
    """Модель часто пишет просто «Еда». Если в сообщении назван ровно один приём пищи — берём его."""
    if act.get("title") != "Еда":
        return
    found = [name for pattern, name in MEALS if re.search(pattern, text or "", re.I)]
    if len(found) == 1:
        act["title"] = found[0]


def _hm(value):
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(value).strip()) if value else None
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return int(m.group(1)), int(m.group(2))
    if m and int(m.group(1)) == 24 and int(m.group(2)) == 0:
        return 0, 0
    return None


def _int(value):
    try:
        v = int(round(float(value)))
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def weekday_date(value, today: date_cls, forward: bool = True) -> date_cls | None:
    """«thu» / «thursday» → ближайший четверг: вперёд (для планов) или назад (для сделанного), сегодня включительно.
    Модель путает, какое число будет в четверг, — поэтому она называет день недели, а число считает код."""
    v = str(value or "").strip().lower()
    key = v[:3] if v in WEEKDAYS or v in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday") else None
    if not key:
        return None
    target = WEEKDAYS.index(key)
    if forward:
        return today + timedelta(days=(target - today.weekday()) % 7)
    return today - timedelta(days=(today.weekday() - target) % 7)


def _day(value, now: datetime) -> date_cls:
    v = str(value or "").strip().lower()
    if v == "yesterday":
        return now.date() - timedelta(days=1)
    weekday = weekday_date(v, now.date(), forward=False)
    if weekday:
        return weekday
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return now.date()


def _rolled_back(s: datetime, e: datetime, now: datetime):
    """«В 23 начала играть», сказанное в 00:58, — это вчерашние 23:00, а не сегодняшние (будущее).
    Дела — всегда прошлое, поэтому время, ушедшее в будущее, переносим на сутки назад, если так выходит
    недавнее прошлое (до 12 ч). «В 18 пойду работать», сказанное в 15:00, так не переносится — это план."""
    if s > now + timedelta(minutes=5) and now - (s - timedelta(days=1)) <= timedelta(hours=12):
        return s - timedelta(days=1), e - timedelta(days=1)
    return s, e


def _at(day: date_cls, hm) -> datetime:
    return datetime(day.year, day.month, day.day, hm[0], hm[1])


def _legacy(act):
    """Старый формат (модель сама посчитала start_time/end_time) — на случай, если она его вернёт."""
    try:
        return (datetime.fromisoformat(str(act["start_time"]).replace("Z", "")).replace(tzinfo=None),
                datetime.fromisoformat(str(act["end_time"]).replace("Z", "")).replace(tzinfo=None))
    except (KeyError, TypeError, ValueError):
        return None


def resolve_times(activities: list, now: datetime) -> None:
    """Проставляет каждому делу start_time / end_time / duration_minutes / time_exact по словам модели.
    now — местное время без часового пояса."""
    slots = []  # (start, end, exact) или None — «время не сказано»
    loose_start = []  # сказан только конец, а начала и длительности нет — начало можно взять у предыдущего дела
    start_named = []  # начало сказано — его не двигаем (в будущем — значит, это план, а не сделанное)
    for act in activities:
        day = _day(act.get("day"), now)
        day_named = str(act.get("day") or "").strip().lower() not in ("", "today", "none", "null")
        start, end = _hm(act.get("start")), _hm(act.get("end"))
        dur, ago = _int(act.get("duration_min")), _int(act.get("ended_ago_min"))
        part = str(act.get("part_of_day") or "").lower()
        loose_start.append(not start and dur is None and bool(end or ago is not None))
        start_named.append(bool(start))

        slot = None
        if start and end:
            s, e = _at(day, start), _at(day, end)
            if e <= s:                      # «с 23 до 7» — через полночь
                e += timedelta(days=1)
                if e > now + timedelta(minutes=5):
                    s, e = s - timedelta(days=1), e - timedelta(days=1)
            slot = (s, e, True)
        elif start:
            s = _at(day, start)
            slot = (s, s + timedelta(minutes=dur or DEFAULT_MIN), dur is not None)
        elif end:
            e = _at(day, end)
            slot = (e - timedelta(minutes=dur or DEFAULT_MIN), e, dur is not None)
        elif ago is not None:
            e = now - timedelta(minutes=ago)
            slot = (e - timedelta(minutes=dur or DEFAULT_MIN), e, dur is not None)
        elif part in PART_OF_DAY:
            h, m, default = PART_OF_DAY[part]
            s = _at(day, (h, m))
            slot = (s, s + timedelta(minutes=dur or default), False)
        elif _legacy(act) and not act.get("duration_min"):
            s, e = _legacy(act)
            slots.append((s, e, False))
            continue
        else:
            slots.append(None)
            act["_dur"] = dur or DEFAULT_MIN
            act["_day"] = day
            continue
        if not day_named and ago is None:
            slot = (*_rolled_back(slot[0], slot[1], now), slot[2])
        slots.append(slot)

    # Дела без времени — подряд: после предыдущего известного, а если его нет — до следующего известного
    # или (для сегодня) до «сейчас»
    n = len(activities)
    for i in range(n):
        if slots[i] is not None:
            continue
        j = i
        while j < n and slots[j] is None:
            j += 1
        block = list(range(i, j))
        if i > 0 and slots[i - 1] is not None:
            cursor = slots[i - 1][1]
            for k in block:
                slots[k] = (cursor, cursor + timedelta(minutes=activities[k]["_dur"]), False)
                cursor = slots[k][1]
        else:
            day = activities[i]["_day"]
            anchor = slots[j][0] if j < n else (now if day == now.date() else _at(day, (22, 0)))
            cursor = anchor
            for k in reversed(block):
                slots[k] = (cursor - timedelta(minutes=activities[k]["_dur"]), cursor, False)
                cursor = slots[k][0]

    # Прикинутое время не может уехать в будущее: сдвигаем так, чтобы закончилось «сейчас»
    for i, (s, e, exact) in enumerate(slots):
        if not exact and e > now and not start_named[i]:
            slots[i] = (s - (e - now), now, exact)

    # Дела из одного сообщения идут одно за другим: «час играла, после этого до 0:58 сидела в TikTok» —
    # у TikTok сказан только конец, начало — там, где кончились игры
    for i in range(1, n):
        prev_end = slots[i - 1][1]
        s, e, exact = slots[i]
        if loose_start[i] and prev_end < e and e - prev_end <= timedelta(hours=6):
            slots[i] = (prev_end, e, exact)
    # …и не налезают друг на друга: прикинутое дело заканчивается там, где начинается следующее
    for i in range(n - 2, -1, -1):
        s, e, exact = slots[i]
        next_start = slots[i + 1][0]
        if not exact and s < next_start < e:
            slots[i] = (s - (e - next_start), next_start, exact)

    for act, (s, e, exact) in zip(activities, slots):
        act["start_time"], act["end_time"] = s.isoformat(timespec="seconds"), e.isoformat(timespec="seconds")
        act["duration_minutes"] = max(1, int((e - s).total_seconds() // 60))
        act["time_exact"] = exact
        act.pop("_dur", None)
        act.pop("_day", None)
