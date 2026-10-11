import aiosqlite
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
                name TEXT UNIQUE,
                streak INTEGER DEFAULT 0,
                last_completed TEXT
            )
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

async def record_pattern(user_id: int, title: str, utility: str, date_str: str):
    title_clean = title.strip().capitalize()
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
            streak += 1
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

async def get_today_summary(user_id, date_str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute('''
            SELECT utility, category, SUM(duration_min)
            FROM activities
            WHERE user_id = ? AND start_time LIKE ?
            GROUP BY utility, category
        ''', (user_id, f"{date_str}%")) as cursor:
            return await cursor.fetchall()