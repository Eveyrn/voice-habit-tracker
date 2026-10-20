"""Проверочные фразы: гоняют НАСТОЯЩУЮ модель (нужен GROQ_API_KEY в .env) и проверяют то, что пользователь
увидит. Запускать перед каждым обновлением бота:

    python tests/eval_phrases.py            # каждую фразу по разу
    python tests/eval_phrases.py --runs 3   # по 3 раза — видно, стабильна ли модель

Модель от раза к разу отвечает по-разному, поэтому одна удачная проверка ничего не гарантирует —
смотрите на «N из N».
"""
import os
import sys
import asyncio
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz  # noqa: E402
import config  # noqa: E402
from services.ai_parser import parse_voice_text  # noqa: E402


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
]


async def main(runs: int):
    total_ok = 0
    for phrase, context, check in CASES:
        results = []
        for _ in range(runs):
            r = await parse_voice_text(phrase, KNOWN_TITLES, "", context)
            results.append(check(r, now_local()))
        ok = sum(1 for e in results if e is None)
        total_ok += ok
        mark = "✅" if ok == runs else ("⚠️ " if ok else "❌")
        print(f"{mark} {ok}/{runs}  {phrase}")
        for e in {e for e in results if e}:
            print(f"      {e}")
    print(f"\nИтого: {total_ok}/{len(CASES) * runs}")


if __name__ == "__main__":
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 1
    asyncio.run(main(runs))
