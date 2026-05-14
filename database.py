import aiosqlite
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
DB_PATH = "kasym.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS chat_settings (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT, full_name TEXT,
            first_seen TEXT, last_seen TEXT,
            message_count INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0, ban_reason TEXT,
            warn_count INTEGER DEFAULT 0,
            xp INTEGER DEFAULT 0,
            correct_answers INTEGER DEFAULT 0,
            wrong_answers INTEGER DEFAULT 0,
            blitz_participations INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER, user_id INTEGER,
            username TEXT, full_name TEXT,
            reason TEXT, message_text TEXT, voice_transcript TEXT,
            created_at TEXT, status TEXT DEFAULT 'pending',
            owner_decision TEXT, decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, reason TEXT,
            created_at TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS banlist (
            user_id INTEGER PRIMARY KEY,
            username TEXT, full_name TEXT,
            reason TEXT, banned_at TEXT
        );
        CREATE TABLE IF NOT EXISTS moderators (
            user_id INTEGER PRIMARY KEY,
            username TEXT, full_name TEXT,
            rank INTEGER DEFAULT 1, added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER, admin_username TEXT, admin_name TEXT,
            target_id INTEGER, target_username TEXT, target_name TEXT,
            chat_id INTEGER, action TEXT, duration TEXT,
            reason TEXT, quote TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, filename TEXT, file_path TEXT,
            page_count INTEGER DEFAULT 0,
            loaded_at TEXT, status TEXT DEFAULT 'processing'
        );
        CREATE TABLE IF NOT EXISTS book_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER, book_title TEXT,
            page_number INTEGER, chunk_text TEXT, embedding TEXT,
            FOREIGN KEY (book_id) REFERENCES books(id)
        );
        CREATE TABLE IF NOT EXISTS blitz_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER, book_title TEXT,
            question TEXT, answer TEXT, topic TEXT,
            page_number INTEGER, explanation TEXT,
            used_count INTEGER DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS blitz_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER, question_text TEXT, correct_answer TEXT,
            started_at TEXT, closed_at TEXT, status TEXT DEFAULT 'active',
            first_winner_id INTEGER, second_winner_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS blitz_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER, user_id INTEGER, username TEXT,
            answer_text TEXT, is_correct INTEGER DEFAULT 0,
            position INTEGER, answered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS flood_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, message_time TEXT
        );
        CREATE TABLE IF NOT EXISTS weak_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, topic TEXT,
            wrong_count INTEGER DEFAULT 1, last_wrong TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, date TEXT,
            messages INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
            xp_earned INTEGER DEFAULT 0
        );
        """)
        await db.commit()
        logger.info("Database initialised")


# âââ SETTINGS ââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM chat_settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        await db.commit()

async def delete_setting(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM chat_settings WHERE key=?", (key,))
        await db.commit()


# âââ USERS âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def upsert_user(user_id: int, username: str, full_name: str):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(user_id, username, full_name, first_seen, last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username, full_name=excluded.full_name, last_seen=excluded.last_seen
        """, (user_id, username, full_name, now, now))
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone()

async def get_user_by_username(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE LOWER(username)=?", (username.lower().lstrip("@"),)
        ) as cur:
            return await cur.fetchone()

async def increment_message_count(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET message_count=message_count+1, last_seen=? WHERE user_id=?",
            (datetime.now().isoformat(), user_id))
        await db.commit()

async def add_xp(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET xp=xp+? WHERE user_id=?", (amount, user_id))
        await db.commit()

async def get_top_users(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE is_banned=0 ORDER BY xp DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


# âââ BANLIST âââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def ban_user(user_id: int, username: str, full_name: str, reason: str):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO banlist(user_id, username, full_name, reason, banned_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, banned_at=excluded.banned_at
        """, (user_id, username, full_name, reason, now))
        await db.execute("UPDATE users SET is_banned=1, ban_reason=? WHERE user_id=?", (reason, user_id))
        await db.commit()

async def unban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM banlist WHERE user_id=?", (user_id,))
        await db.execute("UPDATE users SET is_banned=0, ban_reason=NULL WHERE user_id=?", (user_id,))
        await db.commit()

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM banlist WHERE user_id=?", (user_id,)) as cur:
            return bool(await cur.fetchone())

async def get_banlist():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM banlist ORDER BY banned_at DESC") as cur:
            return await cur.fetchall()


# âââ VIOLATIONS ââââââââââââââââââââââââââââââââââââââââââââââââââ

async def save_violation(chat_id, user_id, username, full_name,
                          reason, message_text="", voice_transcript=""):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO violations(chat_id, user_id, username, full_name, reason,
                message_text, voice_transcript, created_at, status)
            VALUES(?,?,?,?,?,?,?,?,'pending')
        """, (chat_id, user_id, username, full_name, reason, message_text, voice_transcript, now))
        await db.commit()
        return cur.lastrowid

async def get_violation(violation_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM violations WHERE id=?", (violation_id,)) as cur:
            return await cur.fetchone()

async def update_violation_status(violation_id: int, status: str, decision: str = ""):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE violations SET status=?, owner_decision=?, decided_at=? WHERE id=?",
            (status, decision, now, violation_id))
        await db.commit()


# âââ WARNINGS ââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def add_warning(user_id: int, reason: str) -> int:
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO warnings(user_id, reason, created_at) VALUES(?,?,?)",
                         (user_id, reason, now))
        await db.execute("UPDATE users SET warn_count=warn_count+1 WHERE user_id=?", (user_id,))
        await db.commit()
    return await get_warn_count(user_id)

async def get_warn_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM warnings WHERE user_id=? AND active=1", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def clear_warnings(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE warnings SET active=0 WHERE user_id=?", (user_id,))
        await db.execute("UPDATE users SET warn_count=0 WHERE user_id=?", (user_id,))
        await db.commit()


# âââ MODERATORS ââââââââââââââââââââââââââââââââââââââââââââââââââ

async def add_moderator(user_id: int, username: str, full_name: str, rank: int = 1):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO moderators(user_id, username, full_name, rank, added_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET rank=excluded.rank
        """, (user_id, username, full_name, rank, now))
        await db.commit()

async def remove_moderator(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM moderators WHERE user_id=?", (user_id,))
        await db.commit()

async def get_moderator_rank(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT rank FROM moderators WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def get_all_moderators():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM moderators ORDER BY rank DESC") as cur:
            return await cur.fetchall()


# âââ ADMIN ACTIONS LOG âââââââââââââââââââââââââââââââââââââââââââ

async def save_admin_action(admin_id, admin_username, admin_name,
                             target_id, target_username, target_name,
                             chat_id, action, duration, reason, quote):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO admin_actions(
                admin_id, admin_username, admin_name,
                target_id, target_username, target_name,
                chat_id, action, duration, reason, quote, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, (admin_id, admin_username, admin_name,
              target_id, target_username, target_name,
              chat_id, action, duration, reason, quote, now))
        await db.commit()

async def get_logs(limit: int = 30, admin_id: int = None,
                   target_id: int = None, today_only: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        conditions, params = [], []
        if admin_id:
            conditions.append("admin_id=?"); params.append(admin_id)
        if target_id:
            conditions.append("target_id=?"); params.append(target_id)
        if today_only:
            conditions.append("DATE(created_at)=DATE('now')")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        async with db.execute(
            f"SELECT * FROM admin_actions {where} ORDER BY created_at DESC LIMIT ?", params
        ) as cur:
            return await cur.fetchall()


# âââ BOOKS âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def save_book(title, filename, file_path) -> int:
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO books(title, filename, file_path, loaded_at) VALUES(?,?,?,?)",
            (title, filename, file_path, now))
        await db.commit()
        return cur.lastrowid

async def update_book_status(book_id, status, page_count=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE books SET status=?, page_count=? WHERE id=?",
                         (status, page_count, book_id))
        await db.commit()

async def save_book_chunk(book_id, book_title, page_number, chunk_text, embedding=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO book_chunks(book_id, book_title, page_number, chunk_text, embedding) "
            "VALUES(?,?,?,?,?)", (book_id, book_title, page_number, chunk_text, embedding))
        await db.commit()

async def get_all_books():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM books ORDER BY loaded_at DESC") as cur:
            return await cur.fetchall()

async def delete_book(book_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM book_chunks WHERE book_id=?", (book_id,))
        await db.execute("DELETE FROM books WHERE id=?", (book_id,))
        await db.commit()

async def search_chunks_by_text(query: str, limit: int = 5):
    words = query.lower().split()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        results = []
        async with db.execute("SELECT * FROM book_chunks") as cur:
            async for row in cur:
                score = sum(1 for w in words if w in row["chunk_text"].lower())
                if score > 0:
                    results.append((score, dict(row)))
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:limit]]

async def get_random_chunk():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM book_chunks ORDER BY RANDOM() LIMIT 1") as cur:
            return await cur.fetchone()


# âââ BLITZ âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def save_blitz_question(book_id, book_title, question, answer,
                               topic, page_number, explanation) -> int:
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO blitz_questions(book_id, book_title, question, answer,
                topic, page_number, explanation, created_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (book_id, book_title, question, answer, topic, page_number, explanation, now))
        await db.commit()
        return cur.lastrowid

async def start_blitz_session(question_id, question_text, correct_answer) -> int:
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE blitz_sessions SET status='expired', closed_at=? WHERE status='active'", (now,))
        cur = await db.execute("""
            INSERT INTO blitz_sessions(question_id, question_text, correct_answer, started_at, status)
            VALUES(?,?,?,?,'active')
        """, (question_id, question_text, correct_answer, now))
        await db.execute("UPDATE blitz_questions SET used_count=used_count+1 WHERE id=?", (question_id,))
        await db.commit()
        return cur.lastrowid

async def get_active_blitz():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM blitz_sessions WHERE status='active' ORDER BY started_at DESC LIMIT 1"
        ) as cur:
            return await cur.fetchone()

async def close_blitz_session(session_id):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE blitz_sessions SET status='closed', closed_at=? WHERE id=?", (now, session_id))
        await db.commit()

async def record_blitz_answer(session_id, user_id, username, answer_text, is_correct, position):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO blitz_answers(session_id, user_id, username, answer_text,
                is_correct, position, answered_at)
            VALUES(?,?,?,?,?,?,?)
        """, (session_id, user_id, username, answer_text, is_correct, position, now))
        if is_correct:
            await db.execute("UPDATE users SET correct_answers=correct_answers+1 WHERE user_id=?", (user_id,))
        else:
            await db.execute("UPDATE users SET wrong_answers=wrong_answers+1 WHERE user_id=?", (user_id,))
        await db.execute("UPDATE users SET blitz_participations=blitz_participations+1 WHERE user_id=?", (user_id,))
        await db.commit()

async def get_session_correct_count(session_id) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM blitz_answers WHERE session_id=? AND is_correct=1", (session_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def user_already_answered(session_id, user_id) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM blitz_answers WHERE session_id=? AND user_id=?", (session_id, user_id)
        ) as cur:
            return bool(await cur.fetchone())


# âââ FLOOD LOG âââââââââââââââââââââââââââââââââââââââââââââââââââ

async def log_message_time(user_id: int):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO flood_log(user_id, message_time) VALUES(?,?)", (user_id, now))
        await db.execute("DELETE FROM flood_log WHERE message_time < datetime('now', '-60 seconds')")
        await db.commit()

async def get_recent_message_count(user_id: int, seconds: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM flood_log WHERE user_id=? "
            "AND message_time > datetime('now', ? || ' seconds')",
            (user_id, f"-{seconds}")
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# âââ WEAK TOPICS âââââââââââââââââââââââââââââââââââââââââââââââââ

async def record_weak_topic(user_id: int, topic: str):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM weak_topics WHERE user_id=? AND topic=?", (user_id, topic)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE weak_topics SET wrong_count=wrong_count+1, last_wrong=? WHERE user_id=? AND topic=?",
                (now, user_id, topic))
        else:
            await db.execute("INSERT INTO weak_topics(user_id, topic, last_wrong) VALUES(?,?,?)",
                             (user_id, topic, now))
        await db.commit()

async def get_weak_topics(user_id: int, limit: int = 3):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM weak_topics WHERE user_id=? ORDER BY wrong_count DESC LIMIT ?",
            (user_id, limit)
        ) as cur:
            return await cur.fetchall()


# âââ DAILY ACTIVITY ââââââââââââââââââââââââââââââââââââââââââââââ

async def record_daily_activity(user_id: int, xp_earned=0, messages=0, correct=0):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM daily_activity WHERE user_id=? AND date=?", (user_id, today)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE daily_activity SET messages=messages+?, correct=correct+?, "
                "xp_earned=xp_earned+? WHERE user_id=? AND date=?",
                (messages, correct, xp_earned, user_id, today))
        else:
            await db.execute(
                "INSERT INTO daily_activity(user_id, date, messages, correct, xp_earned) VALUES(?,?,?,?,?)",
                (user_id, today, messages, correct, xp_earned))
        await db.commit()
