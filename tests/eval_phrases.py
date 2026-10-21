"""Проверочные фразы: гоняют НАСТОЯЩУЮ модель (нужен GROQ_API_KEY в .env) и проверяют то, что пользователь
увидит. Запускать перед каждым обновлением бота:

    python tests/eval_phrases.py            # каждую фразу по разу
    python tests/eval_phrases.py --runs 3   # по 3 раза — видно, стабильна ли модель
    python tests/eval_phrases.py --only тикток   # только фразы с этим словом (бережёт квоту)

Модель от раза к разу отвечает по-разному, поэтому одна удачная проверка ничего не гарантирует —
смотрите на «N из N».
"""
import os
import re
import sys
import asyncio
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz  # noqa: E402
import config  # noqa: E402
from services import ai_parser  # noqa: E402
from services.ai_parser import parse_voice_text, LAST_USAGE  # noqa: E402
from bot.handlers import CORRECTION_RE, QUESTION_RE  # noqa: E402


def now_local():
    return datetime.now(pytz.timezone(config.TIMEZONE)).replace(tzinfo=None)


def dt(value):
    return datetime.fromisoformat(value)


def near(a: datetime, b: datetime, minutes=3):
    return abs((a - b).total_seconds()) <= minutes * 60


def acts(r):
    return r["activities"]


def one(r, title):
    found = [a for a in acts(r) if a["title"] == title]
    return found[0] if found else None


CONTEXT_HABIT = "Напоминания:\n- напоминание #5: каждый день в 10:00 — Выпить витамины"
CONTEXT_TASKS = "Открытые задачи:\n- задача #24: Работать (сегодня)\n- задача #23: Работать над ботом (сегодня)"
# Как в настоящем боте: модель видит уже известные дела пользователя
KNOWN_TITLES = ["Работа над ботом", "Залипание в телефоне", "Еда", "Завтрак", "Сон", "Тренировка", "Сериал"]

# (фраза, контекст, проверка → None если всё хорошо, иначе текст ошибки)
CASES = [
    ("Час назад закончила тренировку, она шла полтора часа", "",
     lambda r, now: None if (a := one(r, "Тренировка")) and a["duration_minutes"] == 90
     and near(dt(a["end_time"]), now - timedelta(minutes=60)) and a["time_exact"]
     else f"ждали Тренировка 90 мин, конец ≈ сейчас−60: {acts(r)}"),

    ("С 8 до 10 сидела в телефоне", "",
     lambda r, now: None if (a := one(r, "Залипание в телефоне")) and a["start_time"].endswith("08:00:00")
     and a["end_time"].endswith("10:00:00") and a["utility"] == "unproductive"
     else f"ждали Залипание 08:00–10:00: {acts(r)}"),

    ("Утром ходила в зал", "",
     lambda r, now: None if (a := one(r, "Тренировка")) and not a["time_exact"] and dt(a["start_time"]).hour < 12
     else f"ждали Тренировку утром с ≈: {acts(r)}"),

    ("Вчера весь вечер смотрела сериал", "",
     lambda r, now: None if (a := one(r, "Сериал")) and dt(a["start_time"]).date() == now.date() - timedelta(days=1)
     else f"ждали Сериал вчерашним числом: {acts(r)}"),

    ("Поела, а потом пойду работать над ботом", "",
     lambda r, now: None if one(r, "Еда") and not any(dt(a["start_time"]) > now for a in acts(r))
     else f"ждали Еда и ничего в будущем: {acts(r)}"),

    ("Позавтракала, потом 2 часа работала над ботом", "",
     lambda r, now: None if (a := one(r, "Работа над ботом")) and a["duration_minutes"] == 120
     and near(dt(a["end_time"]), now, 5) and one(r, "Завтрак")
     else f"ждали Завтрак + Работа над ботом 120 мин до сейчас: {acts(r)}"),

    ("Поспала с часу ночи до девяти", "",
     lambda r, now: None if (a := one(r, "Сон")) and a["duration_minutes"] == 480
     else f"ждали Сон 8 ч: {acts(r)}"),

    ("Напоминай мне каждый вечер в 10 читать", "",
     lambda r, now: None if any(x["time"] == "22:00" and x["date"] is None for x in r["reminders"])
     else f"ждали ежедневное 22:00: {r['reminders']}"),

    ("Завтра в 9 к врачу", "",
     lambda r, now: None if any(x["time"] == "08:30" and x["date"] == (now + timedelta(days=1)).strftime("%Y-%m-%d")
                                and "09:00" in x["text"] for x in r["reminders"])
     else f"ждали напоминание завтра 08:30 «В 09:00 …»: {r['reminders']}"),

    ("Выпила витамины", CONTEXT_HABIT,
     lambda r, now: None if r["habit_identified"] == "Выпить витамины" and not acts(r)
     else f"ждали привычку «Выпить витамины» без дела: habit={r['habit_identified']} acts={acts(r)}"),

    ("Завтра купить корм коту, сегодня позвонить в банк, и когда-нибудь помыть машину", "",
     lambda r, now: None if len(r["tasks_to_add"]) == 3
     and {t["due"] for t in r["tasks_to_add"]} == {(now + timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"), None}
     else f"ждали 3 задачи: завтра / сегодня / без срока: {r['tasks_to_add']}"),

    ("Сегодня что у нас?", CONTEXT_HABIT,
     lambda r, now: None if r["intent"] == "question" and r["answer"] and not acts(r)
     else f"ждали вопрос с ответом: intent={r['intent']} answer={r['answer']!r}"),

    # 21.10: в 00:58 «в 23» стало будущим («Игры ещё впереди»), а TikTok наложился на игры
    ("Где-то в 23 начала играть с друзьями, где-то час поиграла. После этого до 0:58 сидела в тиктоке", "",
     lambda r, now: None if (g := one(r, "Игры")) and (p := one(r, "Залипание в телефоне"))
     and dt(g["start_time"]).date() == now.date() - timedelta(days=1) and 22 <= dt(g["start_time"]).hour <= 23
     and near(dt(p["end_time"]), now, 5) and dt(p["start_time"]) >= dt(g["end_time"]) - timedelta(minutes=5)
     else f"ждали Игры вчера ~23:00 и телефон после них до 00:58: {acts(r)}", "2026-10-21 00:58"),

    # 21.10: на такой список бот ответил «не нашёл дел»; модель путала, какое число в четверг
    ("надо в четверг в 14 к стоматологу\nкупить корм коту либо сегодня, либо завтра, либо в пятницу\n"
     "решить, что подарить брату (сегодня)\nпостирать шторы в субботу\n\nИ каждый день заходить в игру за наградами", "",
     lambda r, now: None if any(x["date"] == "2026-10-22" and "14:00" in x["text"] for x in r["reminders"])
     and len([t for t in r["tasks_to_add"] if "корм" in t["text"].lower()]) == 1
     and any("штор" in t["text"].lower() and t["due"] == "2026-10-24" for t in r["tasks_to_add"])
     else f"ждали стоматолог чт 22.10 14:00, одну задачу про корм, шторы сб 24.10: "
          f"tasks={r['tasks_to_add']} rem={r['reminders']}", "2026-10-21 10:00"),

    # 21.10: «сейчас завтракаю» в 12:55 бот записал как 08:00–09:00 (модель: завтрак — значит утро);
    # «либо … либо, точно не знаю» стало двумя задачами; «скорее всего в 3–4, либо в пятницу» — точным напоминанием
    ("Привет, я сейчас уже завтракаю гречкой и яйцом. Дальше я буду работать либо над ботом, либо над учёбой, "
     "точно не знаю. А с подругой скорее всего встретимся где-то в 3-4 вечера сегодня, либо же в пятницу. "
     "Она сказала, что через час напишет.", "",
     lambda r, now: None if (a := one(r, "Завтрак")) and near(dt(a["end_time"]), now, 5) and len(acts(r)) == 1
     and not r["tasks_to_add"] and r["reminders"] and all(x.get("tentative") for x in r["reminders"])
     else f"ждали Завтрак до 12:55, без задач, встреча неточная: acts={acts(r)} tasks={r['tasks_to_add']} "
          f"rem={r['reminders']} choices={r.get('choices')}", "2026-10-21 12:55"),

    # 21.10: сказано в 01:01 — «завтра» это 21.10 (после сна), а бот поставил задачи на 22.10
    ("Завтра я должна убраться во всей квартире, пол вымыть. И ещё надо поработать, точно не знаю сколько.", "",
     lambda r, now: None if any("убра" in t["text"].lower() and t["due"] == "2026-10-21" for t in r["tasks_to_add"])
     else f"ждали уборку на 21.10 (ночное «завтра»): {r['tasks_to_add']}", "2026-10-21 01:01"),

    # 21.10: в ответе на вопрос торчали служебные номера: «24 — Работать; 23 — …»
    ("Покажи, что у меня сегодня по задачам", CONTEXT_TASKS,
     lambda r, now: None if r["intent"] == "question" and "Работать над ботом" in r["answer"]
     and not re.search(r"#\d|\b2[34]\b", r["answer"])
     else f"ждали список задач без номеров: {r['answer']!r}"),

    ("Завтра надо убраться в квартире, поработать, пораньше встать, позавтракать и пообедать", "",
     lambda r, now: None if any("убра" in t["text"].lower() for t in r["tasks_to_add"])
     and not any(re.search(r"завтрак|обед|встать", t["text"].lower()) for t in r["tasks_to_add"])
     else f"ждали задачи без еды и подъёма: {r['tasks_to_add']}"),
]


def freeze(at: str | None):
    """Подменяет «сейчас» для модели и кода — для фраз, где важно время суток (после полуночи и т.п.)."""
    if not at:
        ai_parser.datetime = datetime
        return now_local()
    fixed = datetime.strptime(at, "%Y-%m-%d %H:%M")

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return tz.localize(fixed) if tz else fixed
    ai_parser.datetime = Frozen
    return fixed


async def main(runs: int, only: str | None):
    total_ok, tokens = 0, []
    cases = [c for c in CASES if not only or only.lower() in c[0].lower()]
    for phrase, context, check, *at in cases:
        results = []
        # Как в боте: правила поправок в подсказке — только для вопросов и поправок
        with_edits = bool(CORRECTION_RE.search(phrase) or QUESTION_RE.search(phrase))
        for _ in range(runs):
            LAST_USAGE.clear()
            now = freeze(at[0] if at else None)
            r = await parse_voice_text(phrase, KNOWN_TITLES, "", context, with_edits=with_edits)
            results.append(check(r, now))
            if LAST_USAGE:
                tokens.append(LAST_USAGE["total"])
        ok = sum(1 for e in results if e is None)
        total_ok += ok
        mark = "✅" if ok == runs else ("⚠️ " if ok else "❌")
        used = f"  [{LAST_USAGE['total']} ток., {LAST_USAGE['model']}]" if LAST_USAGE else ""
        print(f"{mark} {ok}/{runs}  {phrase}{used}")
        for e in {e for e in results if e}:
            print(f"      {e}")
    print(f"\nИтого: {total_ok}/{len(cases) * runs}")
    if tokens:
        print(f"Токенов на сообщение (вход + ответ, последний запрос): в среднем {sum(tokens) // len(tokens)}, "
              f"от {min(tokens)} до {max(tokens)}")


if __name__ == "__main__":
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 1
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    asyncio.run(main(runs, only))
