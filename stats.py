import logging
from datetime import datetime, timedelta
from telegram import Bot
import database as db
from utils import get_level_info
from config import MAIN_CHAT_ID

logger = logging.getLogger(__name__)


async def get_top_text(period: str = "all", limit: int = 10) -> str:
    """Generate top users leaderboard text."""
    users = await db.get_top_users(limit)
    if not users:
        return "📊 Статистика пуста - сначала нужно поучаствовать в блице!"

    period_labels = {
        "day": "📅 Топ дня",
        "week": "📅 Топ недели",
        "month": "📅 Топ месяца",
        "all": "🏆 Топ батыров",
    }
    title = period_labels.get(period, "🏆 Топ батыров")

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"<b>{title}:</b>\n"]

    for i, u in enumerate(users, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        uname = f"@{u['username']}" if u["username"] else u["full_name"] or f"id{u['user_id']}"
        level_name, level_emoji, _ = get_level_info(u["xp"])
        xp = u["xp"]
        correct = u["correct_answers"]
        lines.append(
            f"{medal} {uname} - {level_emoji} <b>{level_name}</b>\n"
            f"    XP: <b>{xp}</b> · ✅ Правильных: {correct}"
        )

    lines.append("\nИстория любит активных! 🔥")
    return "\n".join(lines)


async def get_my_stats(user_id: int, username: str) -> str:
    user = await db.get_user(user_id)
    if not user:
        return "Статистика не найдена. Напиши что-нибудь в чат сначала!"

    level_name, level_emoji, next_xp = get_level_info(user["xp"])
    xp = user["xp"]
    progress = ""
    if next_xp:
        pct = int((xp / next_xp) * 100)
        bar_filled = int(pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        progress = f"\nДо следующего уровня: [{bar}] {pct}%"

    weak = await db.get_weak_topics(user_id, 2)
    weak_text = ""
    if weak:
        topics = ", ".join(w["topic"] for w in weak)
        weak_text = f"\n⚠️ Слабые темы: {topics}"

    uname = f"@{username}" if username else f"id{user_id}"

    return (
        f"📊 <b>Статистика: {uname}</b>\n\n"
        f"{level_emoji} Уровень: <b>{level_name}</b>\n"
        f"✨ XP: <b>{xp}</b>{progress}\n\n"
        f"✅ Правильных ответов: <b>{user['correct_answers']}</b>\n"
        f"❌ Неправильных: <b>{user['wrong_answers']}</b>\n"
        f"⚡ Блицев сыграно: <b>{user['blitz_participations']}</b>\n"
        f"💬 Сообщений: <b>{user['message_count']}</b>\n"
        f"⚠️ Предупреждений: <b>{user['warn_count']}</b>"
        f"{weak_text}"
    )


async def get_chat_stats() -> str:
    """Overall chat stats."""
    import aiosqlite
    async with aiosqlite.connect("kasym.db") as d:
        async with d.execute("SELECT COUNT(*) FROM users WHERE is_banned=0") as cur:
            total_users = (await cur.fetchone())[0]
        async with d.execute("SELECT SUM(message_count) FROM users") as cur:
            total_msgs = (await cur.fetchone())[0] or 0
        async with d.execute("SELECT COUNT(*) FROM blitz_sessions") as cur:
            total_blitz = (await cur.fetchone())[0]
        async with d.execute("SELECT COUNT(*) FROM books WHERE status='ready'") as cur:
            total_books = (await cur.fetchone())[0]
        async with d.execute("SELECT COUNT(*) FROM violations") as cur:
            total_violations = (await cur.fetchone())[0]
        async with d.execute("SELECT COUNT(*) FROM banlist") as cur:
            total_banned = (await cur.fetchone())[0]

    return (
        f"📊 <b>Статистика чата</b>\n\n"
        f"👥 Участников: <b>{total_users}</b>\n"
        f"💬 Всего сообщений: <b>{total_msgs}</b>\n"
        f"⚡ Блицев сыграно: <b>{total_blitz}</b>\n"
        f"📚 Книг загружено: <b>{total_books}</b>\n"
        f"⚠️ Нарушений: <b>{total_violations}</b>\n"
        f"🚫 В бане: <b>{total_banned}</b>"
    )
