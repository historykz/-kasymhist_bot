""
Касым-бот — учебный помощник по Истории Казахстана.
Работает ТОЛЬКО в одном чате: MAIN_CHAT_ID.
"""

import asyncio
import logging
import re
import os
from datetime import datetime, timedelta

from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.error import TelegramError

# ── project modules ──────────────────────────────────────────────
import database as db
from config import (
    BOT_TOKEN, OWNER_ID, MAIN_CHAT_ID, REQUIRED_CHANNEL,
    BOT_NAME, DAILY_FACT_TIME,
)
from moderation import (
    check_message, send_violation_card,
    restrict_user, unrestrict_user, ban_user_tg,
    process_owner_decision,
)
from voice import transcribe_voice, check_voice_transcript
from history_ai import (
    ask_kasym, get_inappropriate_response, get_daily_fact,
    _is_history_question,
)
from book_loader import load_book, download_and_save_book
from quiz import start_blitz, stop_blitz, process_blitz_answer
from stats import get_top_text, get_my_stats
from admin_panel import (
    handle_owner_dm, handle_admin_callback, handle_violation_callback,
    send_main_menu, get_owner_state, clear_owner_state, set_owner_state,
)
from utils import get_user_mention, get_full_name, get_level_info

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Per-user conversation history for AI (in-memory) ─────────────
# {user_id: [{"role": ..., "content": ...}]}
_ai_history: dict[int, list] = {}

# Track users awaiting subscription check {user_id: message_id}
_awaiting_subscription: dict[int, int] = {}


# ══════════════════════════════════════════════════════════════════
# GUARD: only respond to MAIN_CHAT_ID
# ══════════════════════════════════════════════════════════════════

def is_main_chat(update: Update) -> bool:
    if update.effective_chat:
        return update.effective_chat.id == MAIN_CHAT_ID
    return False


def is_private(update: Update) -> bool:
    if update.effective_chat:
        return update.effective_chat.type == "private"
    return False


def is_owner(update: Update) -> bool:
    if update.effective_user:
        return update.effective_user.id == OWNER_ID
    return False


# ══════════════════════════════════════════════════════════════════
# SUBSCRIPTION CHECK
# ══════════════════════════════════════════════════════════════════

async def check_subscription(bot: Bot, user_id: int) -> bool:
    channel = await db.get_setting("required_channel", REQUIRED_CHANNEL)
    if not channel:
        return True
    check_enabled = await db.get_setting("check_subscription", "0")
    if check_enabled != "1":
        return True
    try:
        member = await bot.get_chat_member(channel, user_id)
        return member.status not in ("left", "kicked", "restricted")
    except TelegramError:
        return True  # if can't check — allow


# ══════════════════════════════════════════════════════════════════
# NEW MEMBER
# ══════════════════════════════════════════════════════════════════

async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_main_chat(update):
        return
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    for user in msg.new_chat_members:
        if user.is_bot:
            continue

        user_id = user.id
        username = user.username or ""
        full_name = get_full_name(user)

        await db.upsert_user(user_id, username, full_name)

        # Check banlist
        if await db.is_banned(user_id):
            await ban_user_tg(context.bot, user_id)
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ Пользователь из банлиста попытался войти в чат.\n"
                f"ID: {user_id} | @{username} | {full_name}"
            )
            return

        # Check subscription
        sub_enabled = await db.get_setting("check_subscription", "0")
        if sub_enabled == "1":
            is_sub = await check_subscription(context.bot, user_id)
            if not is_sub:
                channel = await db.get_setting("required_channel", REQUIRED_CHANNEL)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ Я подписался",
                        callback_data=f"sub_check:{user_id}"
                    )
                ]])
                await restrict_user(context.bot, user_id)
                sent = await context.bot.send_message(
                    MAIN_CHAT_ID,
                    f"👋 {get_user_mention(user)}, добро пожаловать!\n\n"
                    f"Чтобы писать в чате, подпишитесь на канал: {channel}\n"
                    f"После подписки нажмите кнопку 👇",
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                _awaiting_subscription[user_id] = sent.message_id
                return

        # Welcome message
        welcome_tpl = await db.get_setting("welcome", "")
        chat = await context.bot.get_chat(MAIN_CHAT_ID)
        invite_link = chat.invite_link or ""

        if welcome_tpl:
            welcome = (welcome_tpl
                       .replace("{имя}", full_name)
                       .replace("{ссылка}", invite_link))
            await context.bot.send_message(MAIN_CHAT_ID, welcome, parse_mode="HTML")
        else:
            greetings = [
                f"👋 {get_user_mention(user)}, добро пожаловать в учебный чат по Истории Казахстана! "
                f"Изучай, отвечай на блиц-вопросы и становись Ханом истории 👑",
                f"⚔️ Новый батыр в чате — {get_user_mention(user)}! "
                f"Летопись пополнена. Добро пожаловать 📜",
                f"🌾 {get_user_mention(user)}, степь приветствует тебя! "
                f"Задавай вопросы боту: начни со слова «Бот,» 😄",
            ]
            import random
            await context.bot.send_message(
                MAIN_CHAT_ID, random.choice(greetings), parse_mode="HTML"
            )


# ══════════════════════════════════════════════════════════════════
# SUBSCRIPTION CALLBACK
# ══════════════════════════════════════════════════════════════════

async def on_subscription_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data
    if not data.startswith("sub_check:"):
        return

    user_id = int(data.split(":")[1])

    # Only the user themselves can click
    if query.from_user.id != user_id:
        await query.answer("Эта кнопка не для вас 😄", show_alert=True)
        return

    is_sub = await check_subscription(context.bot, user_id)
    if is_sub:
        await unrestrict_user(context.bot, user_id)
        await query.answer("✅ Подписка подтверждена! Добро пожаловать.")
        try:
            await query.delete_message()
        except Exception:
            pass
        # Send welcome
        user = query.from_user
        await context.bot.send_message(
            MAIN_CHAT_ID,
            f"✅ {get_user_mention(user)}, подписка подтверждена. "
            f"Добро пожаловать в чат! Пиши, учись, задавай вопросы боту 😄",
            parse_mode="HTML"
        )
    else:
        channel = await db.get_setting("required_channel", REQUIRED_CHANNEL)
        await query.answer(
            f"Пока подписка не найдена. Подпишитесь на {channel} и нажмите ещё раз.",
            show_alert=True
        )


# ══════════════════════════════════════════════════════════════════
# MAIN MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return

    user = msg.from_user
    user_id = user.id
    chat_id = update.effective_chat.id if update.effective_chat else 0

    # ── Owner DM ──────────────────────────────────────────────────
    if is_private(update) and is_owner(update):
        # Handle file uploads (books)
        if msg.document:
            await handle_book_upload(update, context)
            return
        handled = await handle_owner_dm(update, context.bot)
        if not handled:
            await send_main_menu(context.bot, OWNER_ID)
        return

    # ── Wrong chat — stay silent ──────────────────────────────────
    if not is_main_chat(update):
        if is_private(update):
            await msg.reply_text(
                "Этот бот работает только в своём учебном чате по Истории Казахстана."
            )
        return

    # ── Register user ─────────────────────────────────────────────
    username = user.username or ""
    full_name = get_full_name(user)
    await db.upsert_user(user_id, username, full_name)

    # ── Subscription check ────────────────────────────────────────
    sub_enabled = await db.get_setting("check_subscription", "0")
    if sub_enabled == "1" and user_id != OWNER_ID:
        is_sub = await check_subscription(context.bot, user_id)
        if not is_sub:
            try:
                await msg.delete()
            except Exception:
                pass
            return

    text = msg.text or msg.caption or ""
    mention = get_user_mention(user)

    # ── Voice message ─────────────────────────────────────────────
    if msg.voice or msg.video_note:
        await handle_voice_message(update, context)
        return

    # ── Moderation check ──────────────────────────────────────────
    removed = await check_message(update, context.bot)
    if removed:
        return

    # ── Commands / triggers in chat ───────────────────────────────
    text_lower = text.lower().strip()

    # Show rules
    if text_lower in ("правила", "/правила"):
        rules = await db.get_setting("rules", "")
        if rules:
            chat = await context.bot.get_chat(MAIN_CHAT_ID)
            invite_link = chat.invite_link or ""
            rules = rules.replace("{ссылка}", invite_link)
            await msg.reply_text(f"📜 <b>Правила чата:</b>\n\n{rules}", parse_mode="HTML")
        else:
            await msg.reply_text("Правила ещё не установлены.")
        return

    # Stats commands
    if text_lower in ("статистика", "/статистика", "стат"):
        from stats import get_chat_stats
        t = await get_chat_stats()
        await msg.reply_text(t, parse_mode="HTML")
        return

    if text_lower in ("топ", "топ дня", "/топ", "топ недели", "топ месяца"):
        period = "week" if "недели" in text_lower else ("month" if "месяца" in text_lower else "all")
        t = await get_top_text(period)
        await msg.reply_text(t, parse_mode="HTML")
        return

    if text_lower in ("моя статистика", "мой уровень", "/мойуровень", "/моястатистика"):
        t = await get_my_stats(user_id, username)
        await msg.reply_text(t, parse_mode="HTML")
        return

    # Blitz commands
    if text_lower in ("блиц", "блиц старт", "вопрос", "бот, задай вопрос",
                       "бот задай вопрос", "/блиц", "/вопрос"):
        await start_blitz(context.bot)
        return

    if text_lower in ("блиц стоп", "стоп блиц"):
        if is_owner(update) or await db.get_moderator_rank(user_id) >= 3:
            await stop_blitz(context.bot)
        return

    if text_lower in ("топ блица", "топ блиц"):
        t = await get_top_text("all")
        await msg.reply_text(t, parse_mode="HTML")
        return

    if text_lower == "ответ":
        active = await db.get_active_blitz()
        if active:
            await msg.reply_text(
                f"⏳ Блиц ещё идёт! Напиши ответ прямо в чат.\n"
                f"Вопрос: <b>{active['question_text']}</b>",
                parse_mode="HTML"
            )
        return

    # ── Check if reply to active blitz ───────────────────────────
    active = await db.get_active_blitz()
    if active and text and len(text) < 200:
        processed = await process_blitz_answer(
            context.bot, user_id, username, full_name, text
        )
        if processed:
            return

    # ── AI question (starts with "Бот," or "Kasym,") ─────────────
    bot_triggers = ["бот,", "касым,", "kasym,", "bot,", "бот ", "касым "]
    is_ai_question = any(text_lower.startswith(t) for t in bot_triggers)

    if is_ai_question:
        # Strip trigger prefix
        question = text
        for t in bot_triggers:
            if text_lower.startswith(t):
                question = text[len(t):].strip()
                break

        if not question:
            await msg.reply_text("Спрашивай! Я слушаю 😄", parse_mode="HTML")
            return

        # Check if question is inappropriate
        from utils import contains_bad_words, contains_adult_content
        found_bad, _ = contains_bad_words(question)
        found_adult, _ = contains_adult_content(question)
        if found_bad or found_adult:
            await msg.reply_text(get_inappropriate_response(), parse_mode="HTML")
            return

        # Get/update AI history for this user
        history = _ai_history.get(user_id, [])
        history.append({"role": "user", "content": question})

        # Show typing indicator
        await context.bot.send_chat_action(MAIN_CHAT_ID, "typing")

        answer = await ask_kasym(user_id, question, history)
        history.append({"role": "assistant", "content": answer})
        _ai_history[user_id] = history[-12:]  # keep last 6 exchanges

        await msg.reply_text(answer, parse_mode="HTML")
        return

    # ── Passive XP for activity ───────────────────────────────────
    if text and len(text) > 5:
        import random
        if random.random() < 0.05:  # 5% chance to praise
            praises = [
                f"📜 {mention}, летопись фиксирует твою активность!",
                f"🔥 {mention}, красавчик — пишешь, участвуешь!",
            ]
            await context.bot.send_message(
                MAIN_CHAT_ID, random.choice(praises), parse_mode="HTML"
            )


# ══════════════════════════════════════════════════════════════════
# VOICE HANDLER
# ══════════════════════════════════════════════════════════════════

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    user_id = user.id
    username = user.username or ""
    full_name = get_full_name(user)

    await context.bot.send_chat_action(MAIN_CHAT_ID, "typing")

    transcript = await transcribe_voice(context.bot, msg)

    if transcript:
        violation_found, reason = check_voice_transcript(transcript)
        if violation_found:
            try:
                await msg.delete()
            except Exception:
                pass
            from moderation import handle_voice_violation
            await handle_voice_violation(
                context.bot, user_id, username, full_name, transcript, reason
            )
            return

        # No violation — optionally show transcript
        show_transcript = await db.get_setting("show_voice_transcript", "0")
        if show_transcript == "1":
            mention = get_user_mention(user)
            await msg.reply_text(
                f"🎙 <i>Расшифровка голосового {mention}:</i>\n{transcript[:300]}",
                parse_mode="HTML"
            )


# ══════════════════════════════════════════════════════════════════
# BOOK UPLOAD (owner DM)
# ══════════════════════════════════════════════════════════════════

async def handle_book_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    if not doc:
        return

    filename = doc.file_name or "book"
    ext = os.path.splitext(filename)[1].lower()

    if ext not in (".pdf", ".docx", ".txt"):
        await msg.reply_text(
            f"❌ Формат {ext} не поддерживается.\n"
            f"Поддерживаются: PDF, DOCX, TXT"
        )
        return

    await msg.reply_text(f"📥 Загружаю «{filename}»... Это может занять пару минут.")

    try:
        file_path = await download_and_save_book(context.bot, doc.file_id, filename)
        title = os.path.splitext(filename)[0]
        book_id = await db.save_book(title, filename, file_path)

        result = await load_book(file_path, filename, book_id)

        if result["status"] == "ok":
            ocr_note = " (использован OCR)" if result["ocr_used"] else ""
            await msg.reply_text(
                f"✅ <b>Книга загружена{ocr_note}!</b>\n\n"
                f"📚 Название: {title}\n"
                f"📄 Страниц: {result['page_count']}\n"
                f"🔖 Фрагментов: {result['chunk_count']}\n\n"
                f"Теперь бот может отвечать по этой книге 🎓",
                parse_mode="HTML"
            )
        elif result["status"] == "scanned_no_ocr":
            await msg.reply_text(
                f"⚠️ <b>Книга загружена, но текст не распознан.</b>\n\n"
                f"{result['message']}\n\n"
                f"Попробуйте загрузить текстовую версию PDF или DOCX.",
                parse_mode="HTML"
            )
        else:
            await msg.reply_text(
                f"❌ Ошибка при загрузке: {result['message']}"
            )

    except Exception as e:
        logger.error(f"Book upload error: {e}")
        await msg.reply_text(f"❌ Ошибка загрузки книги: {e}")


# ══════════════════════════════════════════════════════════════════
# CALLBACKS
# ══════════════════════════════════════════════════════════════════

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""

    # Subscription check
    if data.startswith("sub_check:"):
        await on_subscription_check(update, context)
        return

    # Violation callbacks (owner only)
    if data.startswith("viol:") and is_owner(update):
        handled = await handle_violation_callback(update, context.bot)
        if handled:
            return

    # Admin panel callbacks (owner only)
    if data.startswith("admin:") and is_owner(update):
        await handle_admin_callback(update, context.bot)
        return

    await query.answer()


# ══════════════════════════════════════════════════════════════════
# /START
# ══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(update) and is_private(update):
        await send_main_menu(context.bot, OWNER_ID)
        return

    if is_main_chat(update):
        await update.message.reply_text(
            f"👋 Привет! Я <b>Касым</b> — учебный бот по Истории Казахстана.\n\n"
            f"Чтобы задать вопрос, напиши: <b>Бот, [вопрос]</b>\n"
            f"Блиц-вопрос: <b>Блиц</b>\n"
            f"Мой уровень: <b>Мой уровень</b>\n"
            f"Правила: <b>Правила</b>",
            parse_mode="HTML"
        )


# ══════════════════════════════════════════════════════════════════
# SCHEDULED TASKS
# ══════════════════════════════════════════════════════════════════

async def scheduled_daily_fact(bot: Bot):
    """Send daily historical fact."""
    fact = await get_daily_fact()
    if fact:
        await bot.send_message(MAIN_CHAT_ID, fact, parse_mode="HTML")


async def scheduled_auto_blitz(bot: Bot):
    """Auto-start blitz if configured."""
    interval_str = await db.get_setting("auto_blitz_interval")
    if not interval_str:
        return
    # Check if enough time has passed since last blitz
    import aiosqlite
    async with aiosqlite.connect("kasym.db") as d:
        d.row_factory = aiosqlite.Row
        async with d.execute(
            "SELECT closed_at FROM blitz_sessions "
            "WHERE status='closed' ORDER BY closed_at DESC LIMIT 1"
        ) as cur:
            last = await cur.fetchone()

    interval = int(interval_str)
    if last and last["closed_at"]:
        try:
            last_time = datetime.fromisoformat(last["closed_at"])
            if (datetime.now() - last_time).total_seconds() < interval:
                return
        except Exception:
            pass

    await start_blitz(bot)


async def run_scheduler(app: Application):
    """Background task runner."""
    import random
    while True:
        now = datetime.now()

        # Daily fact at configured time
        if now.hour == DAILY_FACT_TIME[0] and now.minute == DAILY_FACT_TIME[1]:
            try:
                await scheduled_daily_fact(app.bot)
            except Exception as e:
                logger.error(f"Daily fact error: {e}")
            await asyncio.sleep(61)  # avoid double-fire

        # Auto blitz check every 5 minutes
        try:
            await scheduled_auto_blitz(app.bot)
        except Exception as e:
            logger.error(f"Auto blitz error: {e}")

        await asyncio.sleep(300)  # 5 minutes


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

async def post_init(app: Application):
    """Called after bot starts."""
    await db.init_db()
    logger.info(f"Database initialised")
    logger.info(f"Bot started. Main chat: {MAIN_CHAT_ID}, Owner: {OWNER_ID}")

    # Start scheduler
    asyncio.create_task(run_scheduler(app))

    # Notify owner
    try:
        await app.bot.send_message(
            OWNER_ID,
            f"✅ <b>Касым-бот запущен!</b>\n\n"
            f"Чат: <code>{MAIN_CHAT_ID}</code>\n"
            f"Напишите <b>Меню</b> для настройки.",
            parse_mode="HTML"
        )
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in .env")
    if not OWNER_ID:
        raise ValueError("OWNER_ID not set in .env")
    if not MAIN_CHAT_ID:
        raise ValueError("MAIN_CHAT_ID not set in .env")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Handlers ─────────────────────────────────────────────────

    # /start
    app.add_handler(CommandHandler("start", cmd_start))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # New members
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member
    ))

    # All messages (text + media + voice)
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION | filters.VOICE |
         filters.VIDEO_NOTE | filters.Document.ALL | filters.STICKER) &
        ~filters.COMMAND,
        on_message
    ))

    logger.info("Starting Kasym Bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
