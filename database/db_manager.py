import aiosqlite
import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "tracker.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                category TEXT,
                utility TEXT,
                duration_min INTEGER,
                start_time TEXT,
                end_time TEXT,
                calendar_event_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                status TEXT DEFAULT 'pending',
                due_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                streak INTEGER DEFAULT 0,
                last_completed TEXT,
                UNIQUE(user_id, name)
            )
        ''')
        # Старые базы: name был уникален на ВСЕХ пользователей — у второго человека
        # с привычкой «Выпить витамины» вставка падала. Пересобираем таблицу.
        async with db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'habits'") as cursor:
            row = await cursor.fetchone()
        if row and "name TEXT UNIQUE" in row[0]:
            await db.executescript('''
                CREATE TABLE habits_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT,
                    streak INTEGER DEFAULT 0,
                    last_completed TEXT,
                    UNIQUE(user_id, name)
                );
                INSERT INTO habits_new (id, user_id, name, streak, last_completed)
                    SELECT id, user_id, name, streak, last_completed FROM habits;
                DROP TABLE habits;
                ALTER TABLE habits_new RENAME TO habits;
            ''')
        # Таблица профиля и фазы наблюдения
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id INTEGER PRIMARY KEY,
                observation_start_date TEXT,
                target_days INTEGER DEFAULT 21,
                phase TEXT DEFAULT 'observation'
            )
        ''')
        # Таблица паттернов повторяющихся активностей
        await db.execute('''
            CREATE TABLE IF NOT EXISTS patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                utility TEXT,
                count_logs INTEGER DEFAULT 1,
                last_performed TEXT,
                avg_interval_days REAL DEFAULT 0,
                UNIQUE(user_id, title)
            )
        ''')
        # Ежедневные напоминания (время — ЧЧ:ММ в config.TIMEZONE)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                time TEXT,
                last_sent TEXT,
                UNIQUE(user_id, text)
            )
        ''')
        # snooze_until — отложено до (ISO-время в config.TIMEZONE);
        # date — для разовых напоминаний (ГГГГ-ММ-ДД), NULL — каждый день
        for column in ("snooze_until TEXT", "date TEXT"):
            try:
                await db.execute(f'ALTER TABLE reminders ADD COLUMN {column}')
            except aiosqlite.OperationalError:
                pass  # колонка уже есть
        # Пользователи: кто попросил доступ и кого пустили (админы — из ALLOWED_USER_IDS, им запись не нужна)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                username TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Google Календарь каждого пользователя (его собственный аккаунт): ключ и id календаря «Трекер дня».
        # В бэкап НЕ попадает (см. backup_bytes) — после восстановления люди просто переподключатся.
        await db.execute('''
            CREATE TABLE IF NOT EXISTS google_calendars (
                user_id INTEGER PRIMARY KEY,
                token TEXT,
                calendar_id TEXT,
                connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # «👎 Бот ошибся»: сообщение и ответ бота целиком + комментарий — из них пополняется tests/eval_phrases.py
        await db.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                bot_text TEXT,
                comment TEXT,
                prompt_message_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Записи (одно сообщение = одна запись): что из него создано — для «Отменить» и для поправок
        await db.execute('''
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                data TEXT,
                created_at TEXT
            )
        ''')
        # Настройки и «уже предупреждала сегодня»
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER,
                key TEXT,
                value TEXT,
                PRIMARY KEY (user_id, key)
            )
        ''')
        await db.commit()

async def get_or_create_profile(user_id: int, today_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT observation_start_date, target_days, phase FROM user_profile WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            await db.execute(
                'INSERT INTO user_profile (user_id, observation_start_date, target_days, phase) VALUES (?, ?, ?, ?)',
                (user_id, today_str, 21, 'observation')
            )
            await db.commit()
            return {'start_date': today_str, 'target_days': 21, 'phase': 'observation', 'days_passed': 1}
        
        start_date, target_days, phase = row
        d_start = datetime.strptime(start_date, "%Y-%m-%d")
        d_today = datetime.strptime(today_str, "%Y-%m-%d")
        days_passed = max(1, (d_today - d_start).days + 1)
        
        if days_passed > target_days and phase == 'observation':
            phase = 'coaching'
            await db.execute('UPDATE user_profile SET phase = "coaching" WHERE user_id = ?', (user_id,))
            await db.commit()
            
        return {'start_date': start_date, 'target_days': target_days, 'phase': phase, 'days_passed': days_passed}

def _pattern_title(title: str) -> str:
    # Только первая буква заглавная: capitalize() портил «ELGE» → «Elge»
    title = (title or "").strip()
    return title[:1].upper() + title[1:]

async def record_pattern(user_id: int, title: str, utility: str, date_str: str):
    title_clean = _pattern_title(title)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, count_logs, last_performed, avg_interval_days FROM patterns WHERE user_id = ? AND title = ?', (user_id, title_clean)) as cursor:
            row = await cursor.fetchone()
        
        if row:
            p_id, count, last_date, avg_interval = row
            if last_date != date_str:
                d_last = datetime.strptime(last_date, "%Y-%m-%d")
                d_curr = datetime.strptime(date_str, "%Y-%m-%d")
                interval = max(1, (d_curr - d_last).days)
                new_avg = interval if count == 1 else (avg_interval * (count - 1) + interval) / count
                count += 1
                await db.execute('''
                    UPDATE patterns 
                    SET count_logs = ?, last_performed = ?, avg_interval_days = ?
                    WHERE id = ?
                ''', (count, date_str, round(new_avg, 1), p_id))
        else:
            await db.execute('''
                INSERT INTO patterns (user_id, title, utility, count_logs, last_performed, avg_interval_days)
                VALUES (?, ?, ?, 1, ?, 0)
            ''', (user_id, title_clean, utility, date_str))
        await db.commit()

async def get_top_patterns(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT title, utility, count_logs, last_performed, avg_interval_days
            FROM patterns
            WHERE user_id = ?
            ORDER BY count_logs DESC
        ''', (user_id,)) as cursor:
            return await cursor.fetchall()

async def add_activity(user_id, title, category, utility, duration_min, start_time, end_time, calendar_event_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('''
            INSERT INTO activities (user_id, title, category, utility, duration_min, start_time, end_time, calendar_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, title, category, utility, duration_min, start_time, end_time, calendar_event_id))
        await db.commit()
        return cursor.lastrowid

async def add_task(user_id, title, due_date=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('''
            INSERT INTO tasks (user_id, title, due_date)
            VALUES (?, ?, ?)
        ''', (user_id, title, due_date))
        await db.commit()
        return cursor.lastrowid

async def get_pending_tasks(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, title, due_date FROM tasks WHERE user_id = ? AND status = "pending" ORDER BY id DESC', (user_id,)) as cursor:
            return await cursor.fetchall()

async def mark_task_done(task_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE tasks SET status = "done" WHERE id = ? AND user_id = ?', (task_id, user_id))
        await db.commit()

async def update_habit(user_id, habit_name, today_date):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, streak, last_completed FROM habits WHERE user_id = ? AND name = ?', (user_id, habit_name)) as cursor:
            row = await cursor.fetchone()
        
        if row:
            habit_id, streak, last_completed = row
            if last_completed == today_date:
                return streak
            # Серия продолжается, только если вчера тоже было сделано
            d_today = datetime.strptime(today_date, "%Y-%m-%d")
            try:
                gap = (d_today - datetime.strptime(last_completed, "%Y-%m-%d")).days
            except (TypeError, ValueError):
                gap = None
            streak = streak + 1 if gap == 1 else 1
            await db.execute('UPDATE habits SET streak = ?, last_completed = ? WHERE id = ?', (streak, today_date, habit_id))
        else:
            streak = 1
            await db.execute('INSERT INTO habits (user_id, name, streak, last_completed) VALUES (?, ?, ?, ?)', (user_id, habit_name, streak, today_date))
        await db.commit()
        return streak

async def get_habits(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT name, streak, last_completed FROM habits WHERE user_id = ?', (user_id,)) as cursor:
            return await cursor.fetchall()

def _last_sent_for(time, date, now_time, today_date):
    """Чтобы напоминание не сработало сразу: ежедневное с уже прошедшим временем — с завтра."""
    if date is None and time <= now_time:
        return today_date
    return None

async def add_reminder(user_id, text, time, now_time, today_date, date=None):
    """Создаёт напоминание или меняет существующее с тем же текстом. Возвращает id.
    date=None — каждый день; date='ГГГГ-ММ-ДД' — один раз. time='meal' — после еды."""
    last_sent = _last_sent_for(time, date, now_time, today_date)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO reminders (user_id, text, time, last_sent, date) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, text) DO UPDATE SET
                time = excluded.time, last_sent = excluded.last_sent, date = excluded.date, snooze_until = NULL
        ''', (user_id, text, time, last_sent, date))
        await db.commit()
        async with db.execute('SELECT id FROM reminders WHERE user_id = ? AND text = ?', (user_id, text)) as cursor:
            return (await cursor.fetchone())[0]

async def update_reminder(reminder_id, user_id, now_time, today_date, **fields):
    """Меняет time и/или date (date=None — сделать ежедневным). Пересчитывает, когда сработает."""
    row = await get_reminder(reminder_id, user_id)
    if not row:
        return None
    _, text, time, date = row
    time = fields.get("time", time)
    date = fields["date"] if "date" in fields else date
    last_sent = _last_sent_for(time, date, now_time, today_date)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE reminders SET time = ?, date = ?, last_sent = ?, snooze_until = NULL WHERE id = ? AND user_id = ?',
                         (time, date, last_sent, reminder_id, user_id))
        await db.commit()
    return (reminder_id, text, time, date)

async def get_reminders(user_id, from_date=None):
    """(id, text, time, date). Разовые — только начиная с from_date (по умолчанию все)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT id, text, time, date FROM reminders
            WHERE user_id = ? AND (date IS NULL OR ? IS NULL OR date >= ?)
            ORDER BY date IS NOT NULL, date, time
        ''', (user_id, from_date, from_date)) as cursor:
            return await cursor.fetchall()

async def get_reminder(reminder_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, text, time, date FROM reminders WHERE id = ? AND user_id = ?', (reminder_id, user_id)) as cursor:
            return await cursor.fetchone()

async def cleanup_old_reminders(today_date):
    """Разовые напоминания за прошедшие дни больше не нужны."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM reminders WHERE date IS NOT NULL AND date < ?', (today_date,))
        await db.commit()

async def delete_reminder(reminder_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM reminders WHERE id = ? AND user_id = ?', (reminder_id, user_id))
        await db.commit()

async def get_due_reminders(time_str, today_date):
    """Напоминания на это время, которые сегодня ещё не отправлялись (ежедневные и разовые на сегодня).
    'meal' сюда не попадает: 'meal' > '23:59' при сравнении строк."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT id, user_id, text FROM reminders
            WHERE time <= ? AND (date IS NULL OR date = ?) AND (last_sent IS NULL OR last_sent != ?)
        ''', (time_str, today_date, today_date)) as cursor:
            return await cursor.fetchall()

async def mark_reminder_sent(reminder_id, today_date):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE reminders SET last_sent = ? WHERE id = ?', (today_date, reminder_id))
        await db.commit()

async def get_activity_stats(user_id, since_date):
    """Сводка дел с даты since_date: по каждому названию — сколько дней, минут всего, во сколько обычно начинается."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT title, utility, category, duration_min, start_time FROM activities
            WHERE user_id = ? AND substr(start_time, 1, 10) >= ?
        ''', (user_id, since_date)) as cursor:
            rows = await cursor.fetchall()

    stats = {}
    for title, utility, category, duration, start in rows:
        s = stats.setdefault(_pattern_title(title), {"utility": utility, "category": category, "days": set(), "minutes": 0, "hours": []})
        s["minutes"] += int(duration or 0)
        if start and len(start) >= 13:
            s["days"].add(start[:10])
            try:
                s["hours"].append(int(start[11:13]) + int(start[14:16]) / 60)
            except ValueError:
                pass
    all_days = {d for s in stats.values() for d in s["days"]}
    return stats, len(all_days)

async def snooze_reminder(reminder_id, user_id, until_iso):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE reminders SET snooze_until = ? WHERE id = ? AND user_id = ?', (until_iso, reminder_id, user_id))
        await db.commit()

async def pop_snoozed_reminders(now_iso):
    """Отложенные напоминания, время которых пришло. Сразу снимает отметку, чтобы не прислать дважды."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT id, user_id, text FROM reminders WHERE snooze_until IS NOT NULL AND snooze_until <= ?', (now_iso,)
        ) as cursor:
            rows = await cursor.fetchall()
        if rows:
            await db.execute('UPDATE reminders SET snooze_until = NULL WHERE snooze_until IS NOT NULL AND snooze_until <= ?', (now_iso,))
            await db.commit()
    return rows

async def get_setting(user_id, key, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT value FROM settings WHERE user_id = ? AND key = ?', (user_id, key)) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else default

async def set_setting(user_id, key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) '
            'ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value', (user_id, key, str(value)))
        await db.commit()

async def get_unproductive_minutes(user_id, date_str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(duration_min), 0) FROM activities WHERE user_id = ? AND utility = 'unproductive' AND start_time LIKE ?",
            (user_id, f"{date_str}%")
        ) as cursor:
            return int((await cursor.fetchone())[0] or 0)

def backup_bytes() -> bytes:
    """Целостная копия базы (через sqlite backup API, даже если бот как раз пишет)."""
    tmp_path = DB_PATH + ".backup"
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(tmp_path)
    try:
        src.backup(dst)
        # Ключи доступа к чужим Google-аккаунтам в файл, который пересылается в Telegram, не кладём
        dst.execute("DELETE FROM google_calendars")
        dst.commit()
        dst.execute("VACUUM")
    finally:
        dst.close()
        src.close()
    with open(tmp_path, "rb") as f:
        data = f.read()
    os.remove(tmp_path)
    return data

async def _recount_patterns(db, user_id, titles):
    """Пересчитывает ритмы по оставшимся делам — после удаления или переименования.
    Иначе удалённое дело осталось бы в анализе (или пропало, хотя в другой записи за тот же день оно есть)."""
    affected = {_pattern_title(t) for t in titles if t}
    if not affected:
        return
    async with db.execute(
        'SELECT title, utility, COALESCE(substr(start_time, 1, 10), substr(created_at, 1, 10)) FROM activities WHERE user_id = ?',
        (user_id,)
    ) as cursor:
        remaining = await cursor.fetchall()
    for t in affected:
        matches = [(u, day) for title, u, day in remaining if _pattern_title(title) == t and day]
        days = sorted({day for _, day in matches})
        if days:
            await db.execute('''
                INSERT INTO patterns (user_id, title, utility, count_logs, last_performed, avg_interval_days)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(user_id, title) DO UPDATE SET count_logs = excluded.count_logs, last_performed = excluded.last_performed
            ''', (user_id, t, matches[0][0], len(days), days[-1]))
        else:
            await db.execute('DELETE FROM patterns WHERE user_id = ? AND title = ?', (user_id, t))

async def delete_items(user_id, activity_ids=(), task_ids=(), reminder_ids=()):
    """Удаляет дела/задачи/напоминания пользователя по id.
    Возвращает список calendar_event_id удалённых дел (длина = сколько дел удалено), чтобы убрать их и из Google."""
    activity_ids, task_ids, reminder_ids = list(activity_ids), list(task_ids), list(reminder_ids)
    async with aiosqlite.connect(DB_PATH) as db:
        titles, event_ids = [], []
        if activity_ids:
            marks = ",".join("?" * len(activity_ids))
            async with db.execute(f'SELECT title, calendar_event_id FROM activities WHERE user_id = ? AND id IN ({marks})', (user_id, *activity_ids)) as cursor:
                rows = await cursor.fetchall()
            titles, event_ids = [r[0] for r in rows], [r[1] for r in rows]
            await db.execute(f'DELETE FROM activities WHERE user_id = ? AND id IN ({marks})', (user_id, *activity_ids))
        if task_ids:
            await db.execute(f'DELETE FROM tasks WHERE user_id = ? AND id IN ({",".join("?" * len(task_ids))})', (user_id, *task_ids))
        if reminder_ids:
            await db.execute(f'DELETE FROM reminders WHERE user_id = ? AND id IN ({",".join("?" * len(reminder_ids))})', (user_id, *reminder_ids))
        await _recount_patterns(db, user_id, titles)
        await db.commit()
    return event_ids

async def undo_entry(user_id, act_from, act_to, task_from, task_to):
    """Старые кнопки «Отменить» (диапазоны id). Возвращает True, если что-то ушло в календарь."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id FROM activities WHERE user_id = ? AND id BETWEEN ? AND ?', (user_id, act_from, act_to)) as cursor:
            acts = [r[0] for r in await cursor.fetchall()]
        async with db.execute('SELECT id FROM tasks WHERE user_id = ? AND id BETWEEN ? AND ?', (user_id, task_from, task_to)) as cursor:
            tasks = [r[0] for r in await cursor.fetchall()]
    deleted = await delete_items(user_id, acts, tasks)
    return bool(deleted) and await _phase(user_id) == 'coaching'

async def get_activities_for_day(user_id, date_str):
    """(id, title, utility, category, duration_min, start_time, end_time) за день, по времени."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT id, title, utility, category, duration_min, start_time, end_time FROM activities
            WHERE user_id = ? AND start_time LIKE ? ORDER BY start_time
        ''', (user_id, f"{date_str}%")) as cursor:
            return await cursor.fetchall()

async def update_activity(activity_id, user_id, **fields):
    """Поправка дела: title / utility / category / start_time / end_time. Длительность пересчитывается по времени."""
    allowed = {k: v for k, v in fields.items() if k in ("title", "utility", "category", "start_time", "end_time") and v}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT title, start_time, end_time FROM activities WHERE id = ? AND user_id = ?', (activity_id, user_id)) as cursor:
            row = await cursor.fetchone()
        if not row or not allowed:
            return None
        old_title, start, end = row
        start, end = allowed.get("start_time", start), allowed.get("end_time", end)
        try:
            minutes = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() // 60)
            if minutes > 0:
                allowed["duration_min"] = minutes
        except (TypeError, ValueError):
            pass
        sets = ", ".join(f"{k} = ?" for k in allowed)
        await db.execute(f'UPDATE activities SET {sets} WHERE id = ? AND user_id = ?', (*allowed.values(), activity_id, user_id))
        await _recount_patterns(db, user_id, [old_title, allowed.get("title")])
        await db.commit()
        async with db.execute('SELECT id, title, utility, category, duration_min, start_time, end_time, calendar_event_id FROM activities WHERE id = ?', (activity_id,)) as cursor:
            return await cursor.fetchone()

# ---------- Google Календарь пользователя ----------

async def get_google(user_id):
    """{"token": {...}, "calendar_id": ...} или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT token, calendar_id FROM google_calendars WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
    return {"token": json.loads(row[0]), "calendar_id": row[1]} if row else None

async def save_google(user_id, token: dict, calendar_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO google_calendars (user_id, token, calendar_id) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET token = excluded.token, calendar_id = excluded.calendar_id,
                connected_at = CURRENT_TIMESTAMP
        ''', (user_id, json.dumps(token), calendar_id))
        await db.commit()

async def delete_google(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM google_calendars WHERE user_id = ?', (user_id,))
        await db.commit()

async def get_daily_totals(user_id, since_date):
    """[(дата, категория, минут)] по дням начиная с since_date — для ответов на «сколько за неделю»."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT substr(start_time, 1, 10) AS day, category, SUM(duration_min) FROM activities
            WHERE user_id = ? AND substr(start_time, 1, 10) >= ?
            GROUP BY day, category ORDER BY day
        ''', (user_id, since_date)) as cursor:
            return await cursor.fetchall()

async def get_task(task_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, title FROM tasks WHERE id = ? AND user_id = ?', (task_id, user_id)) as cursor:
            return await cursor.fetchone()

# ---------- Записи (для «Отменить» и поправок) ----------

async def add_entry(user_id, text, data: dict, created_at):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('INSERT INTO entries (user_id, text, data, created_at) VALUES (?, ?, ?, ?)',
                                  (user_id, text, json.dumps(data, ensure_ascii=False), created_at))
        await db.commit()
        return cursor.lastrowid

async def get_entry(entry_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, text, data, created_at FROM entries WHERE id = ? AND user_id = ?', (entry_id, user_id)) as cursor:
            row = await cursor.fetchone()
    return (row[0], row[1], json.loads(row[2] or "{}"), row[3]) if row else None

async def get_last_entry(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id, text, data, created_at FROM entries WHERE user_id = ? ORDER BY id DESC LIMIT 1', (user_id,)) as cursor:
            row = await cursor.fetchone()
    return (row[0], row[1], json.loads(row[2] or "{}"), row[3]) if row else None

# ---------- «Бот ошибся» ----------

async def add_feedback(user_id, bot_text):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('INSERT INTO feedback (user_id, bot_text) VALUES (?, ?)', (user_id, bot_text))
        await db.commit()
        return cursor.lastrowid

async def set_feedback_prompt(feedback_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE feedback SET prompt_message_id = ? WHERE id = ?', (message_id, feedback_id))
        await db.commit()

async def add_feedback_comment(user_id, prompt_message_id, comment):
    """Комментарий — ответ (reply) на сообщение «что не так?». Возвращает id отзыва или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT id FROM feedback WHERE user_id = ? AND prompt_message_id = ?', (user_id, prompt_message_id)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        await db.execute('UPDATE feedback SET comment = ? WHERE id = ?', (comment, row[0]))
        await db.commit()
        return row[0]

async def list_feedback(limit=50):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT f.id, f.user_id, COALESCE(u.name, ''), f.bot_text, f.comment, f.created_at
            FROM feedback f LEFT JOIN users u ON u.user_id = f.user_id
            ORDER BY f.id DESC LIMIT ?
        ''', (limit,)) as cursor:
            return await cursor.fetchall()

# ---------- Пользователи ----------

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, name, username, status FROM users WHERE user_id = ?', (user_id,)) as cursor:
            return await cursor.fetchone()

async def add_user_request(user_id, name, username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR IGNORE INTO users (user_id, name, username, status) VALUES (?, ?, ?, "pending")',
                         (user_id, name, username))
        await db.commit()

async def set_user_status(user_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET status = ? WHERE user_id = ?', (status, user_id))
        await db.commit()

async def list_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id, name, username, status FROM users ORDER BY created_at') as cursor:
            return await cursor.fetchall()

async def active_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT user_id FROM users WHERE status = "active"') as cursor:
            return [r[0] for r in await cursor.fetchall()]

async def delete_user_data(user_id):
    """Стирает все записи человека (сам доступ остаётся)."""
    async with aiosqlite.connect(DB_PATH) as db:
        for table in ("activities", "tasks", "habits", "patterns", "reminders", "entries", "settings", "user_profile", "google_calendars"):
            await db.execute(f'DELETE FROM {table} WHERE user_id = ?', (user_id,))
        await db.commit()

async def bump_usage(user_id, date_str) -> int:
    """+1 к счётчику записей за день, возвращает новое значение."""
    key = f"usage:{date_str}"
    count = int(await get_setting(user_id, key, 0)) + 1
    await set_setting(user_id, key, count)
    return count

async def _phase(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('SELECT phase FROM user_profile WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None

async def get_today_summary(user_id, date_str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT utility, category, SUM(duration_min)
            FROM activities
            WHERE user_id = ? AND start_time LIKE ?
            GROUP BY utility, category
        ''', (user_id, f"{date_str}%")) as cursor:
            return await cursor.fetchall()