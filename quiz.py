import asyncio
import logging
import random
from datetime import datetime
from telegram import Bot

import database as db
from config import (
    MAIN_CHAT_ID, XP_FIRST_CORRECT, XP_SECOND_CORRECT,
    XP_PARTICIPATE, BLITZ_TIMEOUT
)
from history_ai import generate_blitz_question, check_answer_ai
from utils import get_level_info

logger = logging.getLogger(__name__)

# Active blitz timeout task {session_id: asyncio.Task}
_blitz_timers: dict[int, asyncio.Task] = {}


# ══════════════════════════════════════════════════════════════════
# START BLITZ
# ══════════════════════════════════════════════════════════════════

async def start_blitz(bot: Bot) -> bool:
    """
    Generate and post a blitz question.
    Returns True if started, False if no questions available.
    """
    # Check if already active
    active = await db.get_active_blitz()
    if active:
        await bot.send_message(
            MAIN_CHAT_ID,
            "⚡ Блиц уже идёт! Отвечай на текущий вопрос 😄"
        )
        return False

    # Generate question
    q_data = await generate_blitz_question()
    if not q_data:
        await bot.send_message(
            MAIN_CHAT_ID,
            "📚 Хм, вопросов пока нет. Сначала загрузите книги — и летопись откроется 😄"
        )
        return False

    # Save question to DB
    q_id = await db.save_blitz_question(
        book_id=q_data.get("book_id", 0),
        book_title=q_data.get("book_title", "?"),
        question=q_data.get("question", ""),
        answer=q_data.get("answer", ""),
        topic=q_data.get("topic", ""),
        page_number=q_data.get("page_number", 0),
        explanation=q_data.get("explanation", ""),
    )

    # Start session
    session_id = await db.start_blitz_session(
        q_id,
        q_data["question"],
        q_data["answer"],
    )

    # Send to chat
    intros = [
        "⚡ <b>Блиц-вопрос по Истории Казахстана:</b>",
        "📜 <b>Летопись задаёт вопрос:</b>",
        "⚖️ <b>Совет биев проверяет знания:</b>",
        "🔥 <b>Батыры, готовьтесь:</b>",
    ]
    msg_text = (
        f"{random.choice(intros)}\n\n"
        f"<b>{q_data['question']}</b>\n\n"
        f"Отвечаем быстро! Засчитываются только первые два правильных ответа 👇"
    )
    await bot.send_message(MAIN_CHAT_ID, msg_text, parse_mode="HTML")

    # Start timeout timer
    task = asyncio.create_task(_blitz_timeout(bot, session_id, q_data, BLITZ_TIMEOUT))
    _blitz_timers[session_id] = task

    return True


async def _blitz_timeout(bot: Bot, session_id: int, q_data: dict, timeout: int):
    """Close blitz after timeout if not enough correct answers."""
    await asyncio.sleep(timeout)
    active = await db.get_active_blitz()
    if not active or active["id"] != session_id:
        return

    count = await db.get_session_correct_count(session_id)
    await db.close_blitz_session(session_id)

    if count == 0:
        msg = (
            f"⏳ <b>Время вышло!</b>\n\n"
            f"Правильный ответ: <b>{q_data['answer']}</b>\n"
            f"Тема: {q_data.get('topic', '?')}\n"
            f"📚 {q_data.get('book_title','?')}, стр. {q_data.get('page_number','?')}\n\n"
            f"Повторяем тему и идём дальше 😄"
        )
    else:
        msg = (
            f"⏳ <b>Блиц завершён по времени.</b>\n\n"
            f"Правильный ответ: <b>{q_data['answer']}</b>\n"
            f"Тема: {q_data.get('topic', '?')}\n"
            f"📚 {q_data.get('book_title','?')}, стр. {q_data.get('page_number','?')}"
        )
    await bot.send_message(MAIN_CHAT_ID, msg, parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════
# PROCESS ANSWER
# ══════════════════════════════════════════════════════════════════

async def process_blitz_answer(bot: Bot, user_id: int, username: str,
                                full_name: str, text: str) -> bool:
    """
    Check if text is an answer to active blitz.
    Returns True if processed.
    """
    active = await db.get_active_blitz()
    if not active:
        return False

    session_id = active["id"]
    correct_answer = active["correct_answer"]

    # Check if already answered
    if await db.user_already_answered(session_id, user_id):
        return False

    mention = f"@{username}" if username else full_name
    is_correct = await check_answer_ai(text, correct_answer)
    correct_count = await db.get_session_correct_count(session_id)
    position = correct_count + 1 if is_correct else 0

    await db.record_blitz_answer(session_id, user_id, username, text, is_correct, position)

    if is_correct:
        if correct_count == 0:
            # First correct
            xp = XP_FIRST_CORRECT
            await db.add_xp(user_id, xp)
            await db.record_daily_activity(user_id, xp_earned=xp, correct=1)
            responses = [
                f"🔥 {mention}, первый правильный ответ! Батыр знаний на месте! <b>+{xp} XP</b>",
                f"⚡ {mention}, первый! Абылай хан бы кивнул с уважением 🔥 <b>+{xp} XP</b>",
                f"🏆 {mention}, летопись запомнила первого батыра! <b>+{xp} XP</b>",
            ]
            await bot.send_message(MAIN_CHAT_ID, random.choice(responses), parse_mode="HTML")

            # Get q info for potential close
            q = await _get_question_for_session(session_id)
            if q:
                await bot.send_message(
                    MAIN_CHAT_ID,
                    f"🥇 Первый занят! Кто заберёт второе место? Быстрее! ⚡"
                )

        elif correct_count == 1:
            # Second correct
            xp = XP_SECOND_CORRECT
            await db.add_xp(user_id, xp)
            await db.record_daily_activity(user_id, xp_earned=xp, correct=1)
            responses = [
                f"✅ {mention}, второй правильный ответ засчитан! Хорошо держишь темп. <b>+{xp} XP</b>",
                f"👏 {mention}, второй батыр найден! <b>+{xp} XP</b>",
                f"⚖️ {mention}, Совет биев фиксирует второй правильный ответ. <b>+{xp} XP</b>",
            ]
            await bot.send_message(MAIN_CHAT_ID, random.choice(responses), parse_mode="HTML")

            # Close blitz — 2 correct answers reached
            await _close_blitz_successfully(bot, session_id)

        else:
            # Third+ correct — too late
            await db.add_xp(user_id, XP_PARTICIPATE)
            responses = [
                f"{mention}, верно, но зачёт уже закрыт 😄 Первые два батыра уже в летописи.",
                f"{mention}, правильно! Но опоздал — два героя уже забрали баллы 📜",
            ]
            await bot.send_message(MAIN_CHAT_ID, random.choice(responses), parse_mode="HTML")

        # Check level up
        user = await db.get_user(user_id)
        if user:
            level_name, level_emoji, next_xp = get_level_info(user["xp"])
            # Announce if just reached a new level threshold
            for threshold, name, emoji in [
                (30, "Жас батыр", "⚔️"),
                (100, "Знаток степи", "🌾"),
                (200, "Би знаний", "⚖️"),
                (400, "Хан истории", "👑"),
            ]:
                if user["xp"] - XP_FIRST_CORRECT < threshold <= user["xp"]:
                    await bot.send_message(
                        MAIN_CHAT_ID,
                        f"🎉 {mention} получает новый титул: <b>{emoji} {name}</b>!\n"
                        f"Летопись пополнена 📜",
                        parse_mode="HTML"
                    )

    else:
        # Wrong answer
        await db.add_xp(user_id, 0)
        await db.record_daily_activity(user_id, xp_earned=0)

        # Save weak topic
        q = await _get_question_for_session(session_id)
        topic = q["topic"] if q else "История Казахстана"
        if topic:
            await db.record_weak_topic(user_id, topic)

        responses = [
            f"Ай-ай-ай, {mention} 😄 Ответ не тот. Повтори тему: <b>{topic}</b>. История любит точность!",
            f"{mention}, не совсем 😄 Летопись плачет. Тема: <b>{topic}</b> — повторяй!",
            f"Хм, {mention}, подумай ещё 🤔 Тема: <b>{topic}</b>",
        ]
        await bot.send_message(MAIN_CHAT_ID, random.choice(responses), parse_mode="HTML")

    return True


async def _close_blitz_successfully(bot: Bot, session_id: int):
    """Close blitz after 2 correct answers."""
    active = await db.get_active_blitz()
    if not active:
        return

    # Cancel timer
    timer = _blitz_timers.pop(session_id, None)
    if timer:
        timer.cancel()

    await db.close_blitz_session(session_id)

    # Get question info
    q = await _get_question_for_session(session_id)
    if not q:
        return

    msg = (
        f"✅ <b>Блиц закрыт!</b>\n\n"
        f"Правильный ответ: <b>{q['answer']}</b>\n"
        f"Тема: {q.get('topic', '?')}\n"
        f"📚 {q.get('book_title', '?')}, стр. {q.get('page_number', '?')}\n\n"
        f"Кто хочет следующий вопрос? Напишите: <b>Блиц ⚡</b>"
    )
    await bot.send_message(MAIN_CHAT_ID, msg, parse_mode="HTML")


async def _get_question_for_session(session_id: int) -> dict | None:
    active = await db.get_active_blitz()
    if active:
        q_id = active["question_id"]
    else:
        # session closed, fetch directly
        import aiosqlite
        async with aiosqlite.connect("kasym.db") as d:
            d.row_factory = aiosqlite.Row
            async with d.execute(
                "SELECT question_id FROM blitz_sessions WHERE id=?", (session_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                q_id = row["question_id"]

    import aiosqlite
    async with aiosqlite.connect("kasym.db") as d:
        d.row_factory = aiosqlite.Row
        async with d.execute(
            "SELECT * FROM blitz_questions WHERE id=?", (q_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def stop_blitz(bot: Bot):
    """Manually stop active blitz."""
    active = await db.get_active_blitz()
    if not active:
        return False
    session_id = active["id"]
    timer = _blitz_timers.pop(session_id, None)
    if timer:
        timer.cancel()
    await db.close_blitz_session(session_id)
    await bot.send_message(MAIN_CHAT_ID,
        "🛑 Блиц остановлен администрацией.", parse_mode="HTML")
    return True
