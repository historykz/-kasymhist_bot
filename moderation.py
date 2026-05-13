import logging
import random
from datetime import datetime, timedelta
from telegram import Bot, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatPermissions
from telegram.error import TelegramError

import database as db
from config import (
    OWNER_ID, MAIN_CHAT_ID,
    FLOOD_MAX_MESSAGES, FLOOD_TIME_WINDOW, FLOOD_MUTE_DURATION,
    SPAM_IDENTICAL_COUNT, SPAM_TIME_WINDOW,
    EMOJI_SPAM_COUNT,
)
from utils import (
    get_user_mention, get_full_name, now_str,
    contains_bad_words, contains_adult_content,
    count_emojis, is_caps_spam,
)

logger = logging.getLogger(__name__)

# ─── Per-user in-memory caches (lightweight) ─────────────────────────────────
# {user_id: [datetime, ...]}
_message_times: dict[int, list] = {}
# {user_id: [text, ...]}
_recent_texts: dict[int, list] = {}
# {user_id: int} sticker count
_sticker_times: dict[int, list] = {}


# ══════════════════════════════════════════════════════════════════
# FLOOD & SPAM DETECTION
# ══════════════════════════════════════════════════════════════════

def _track_message(user_id: int, text: str = "") -> dict:
    """
    Track messages per user, return analysis result:
    {flood: bool, spam_identical: bool, emoji_spam: bool, caps: bool}
    """
    now = datetime.now()
    result = {"flood": False, "spam_identical": False, "emoji_spam": False, "caps": False}

    # --- Flood: too many messages in time window ---
    times = _message_times.get(user_id, [])
    times = [t for t in times if (now - t).total_seconds() < FLOOD_TIME_WINDOW]
    times.append(now)
    _message_times[user_id] = times
    if len(times) >= FLOOD_MAX_MESSAGES:
        result["flood"] = True

    # --- Spam identical messages ---
    if text:
        recent = _recent_texts.get(user_id, [])
        recent = recent[-SPAM_IDENTICAL_COUNT:]
        recent.append(text.strip().lower())
        _recent_texts[user_id] = recent
        if len(recent) >= SPAM_IDENTICAL_COUNT and len(set(recent)) == 1:
            result["spam_identical"] = True

    # --- Emoji spam ---
    if text and count_emojis(text) >= EMOJI_SPAM_COUNT:
        result["emoji_spam"] = True

    # --- Caps spam ---
    if text and is_caps_spam(text):
        result["caps"] = True

    return result


def _track_sticker(user_id: int) -> bool:
    """Returns True if sticker spam detected."""
    now = datetime.now()
    times = _sticker_times.get(user_id, [])
    times = [t for t in times if (now - t).total_seconds() < 15]
    times.append(now)
    _sticker_times[user_id] = times
    return len(times) >= 4


# ══════════════════════════════════════════════════════════════════
# RESTRICT / MUTE HELPERS
# ══════════════════════════════════════════════════════════════════

NO_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
)

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def restrict_user(bot: Bot, user_id: int, until: datetime | None = None):
    """Restrict user in the main chat."""
    until_ts = int(until.timestamp()) if until else 0
    try:
        await bot.restrict_chat_member(
            chat_id=MAIN_CHAT_ID,
            user_id=user_id,
            permissions=NO_PERMISSIONS,
            until_date=until_ts if until_ts else None,
        )
        await db.upsert_user(user_id, "", "")
    except TelegramError as e:
        logger.warning(f"restrict_user error: {e}")


async def unrestrict_user(bot: Bot, user_id: int):
    """Restore full permissions."""
    try:
        await bot.restrict_chat_member(
            chat_id=MAIN_CHAT_ID,
            user_id=user_id,
            permissions=FULL_PERMISSIONS,
        )
    except TelegramError as e:
        logger.warning(f"unrestrict_user error: {e}")


async def kick_user(bot: Bot, user_id: int):
    try:
        await bot.ban_chat_member(MAIN_CHAT_ID, user_id,
                                   until_date=datetime.now() + timedelta(seconds=35))
    except TelegramError as e:
        logger.warning(f"kick_user error: {e}")


async def ban_user_tg(bot: Bot, user_id: int):
    try:
        await bot.ban_chat_member(MAIN_CHAT_ID, user_id)
    except TelegramError as e:
        logger.warning(f"ban_user_tg error: {e}")


# ══════════════════════════════════════════════════════════════════
# VIOLATION CARD → OWNER DM
# ══════════════════════════════════════════════════════════════════

async def send_violation_card(bot: Bot, violation_id: int,
                               user_id: int, username: str, full_name: str,
                               reason: str, message_text: str = "",
                               voice_transcript: str = ""):
    """Send formatted violation card to owner with action buttons."""
    is_voice = bool(voice_transcript)

    text = (
        f"🚨 <b>Нарушение в учебном чате</b>\n\n"
        f"👤 <b>Пользователь:</b>\n"
        f"  Имя: {full_name}\n"
        f"  Username: @{username or '—'}\n"
        f"  ID: <code>{user_id}</code>\n\n"
        f"📌 <b>Причина:</b> {reason}\n"
    )
    if message_text:
        preview = message_text[:200] + ("…" if len(message_text) > 200 else "")
        text += f"\n💬 <b>Сообщение:</b>\n«{preview}»\n"
    if is_voice:
        preview = voice_transcript[:200] + ("…" if len(voice_transcript) > 200 else "")
        text += f"\n🎙 <b>Расшифровка голосового:</b>\n«{preview}»\n"

    text += f"\n⏰ <b>Время:</b> {now_str()}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"viol:delete:{violation_id}"),
            InlineKeyboardButton("⚠️ Варн",    callback_data=f"viol:warn:{violation_id}"),
        ],
        [
            InlineKeyboardButton("🔇 Ограничить", callback_data=f"viol:restrict:{violation_id}"),
            InlineKeyboardButton("😇 Простить",   callback_data=f"viol:forgive:{violation_id}"),
        ],
    ])

    try:
        await bot.send_message(OWNER_ID, text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramError as e:
        logger.error(f"send_violation_card error: {e}")


# ══════════════════════════════════════════════════════════════════
# HUMAN-LIKE RESPONSES
# ══════════════════════════════════════════════════════════════════

FLOOD_WARNINGS = [
    "⚡ {mention}, эй-эй, полегче! Ты пишешь слишком быстро. Сделай паузу — чат не телетайп 😄",
    "🌊 {mention}, вижу настоящий поток сознания! Давай помедленнее, хорошо? Я всё равно читаю каждое слово.",
    "📜 {mention}, летопись чата не успевает за тобой! Пиши чуть медленнее, батыр.",
    "🛑 {mention}, стоп-стоп-стоп. Одно сообщение зараз, договорились? Иначе придётся доложить администрации 👀",
    "😅 {mention}, у тебя всё хорошо? Столько сообщений за секунду — это рекорд! Чуть помедленнее, пожалуйста.",
]

SPAM_IDENTICAL_WARNINGS = [
    "🔁 {mention}, ты уже написал это. Я не глухой — всё слышу с первого раза 😄 Не надо повторять.",
    "📋 {mention}, скопировал сам себя? Одного раза достаточно, поверь.",
    "👂 {mention}, слышу тебя! Одно и то же сообщение несколько раз — это уже спам. Остановись.",
    "🤔 {mention}, ты точно помнишь, что уже написал это? Напоминаю: да, написал 😄",
]

CAPS_WARNINGS = [
    "📢 {mention}, зачем так кричать? Я слышу тебя и без капслока 😄",
    "🔊 {mention}, CAPS LOCK — это не аргумент, это просто громко. Пиши нормально, пожалуйста.",
    "😬 {mention}, ого, СТОЛЬКО ЗАГЛАВНЫХ! Включи строчные, договорились?",
]

EMOJI_SPAM_WARNINGS = [
    "🎭 {mention}, ты прислал целый зоопарк эмодзи 😅 Чуть поменьше, хорошо?",
    "🌈 {mention}, я насчитал много эмодзи в одном сообщении. Нам хватит и пары штук 😄",
]

STICKER_SPAM_WARNINGS = [
    "🎪 {mention}, стикеры — это весело, но не 4 подряд за 15 секунд 😄 Притормози.",
    "📦 {mention}, стикеры кончатся — а репутация останется. Не спамь стикерами, пожалуйста.",
]

FLOOD_MUTE_MESSAGES = [
    (
        "🔇 {mention}, я предупреждал! Флуд продолжился — временно ограничиваю тебя на {duration}.\n"
        "Используй время с умом — повтори тему Казахского ханства 📚\n\n"
        "<i>Я уже сообщил администрации. Жди решения.</i>"
    ),
    (
        "⏱ {mention}, поток сообщений зашкалил. Делаю паузу на {duration} — так будет лучше для всех.\n"
        "Администрация уведомлена 👀"
    ),
]

BAD_WORD_MESSAGES = [
    "⚠️ {mention}, ваше сообщение нарушило правила чата.\n"
    "Причина: нецензурная лексика\n\n"
    "До решения администрации ваши возможности в чате временно ограничены.\n"
    "Пожалуйста, дождитесь решения.",

    "⚠️ {mention}, в нашем ханстве за мат не хвалят 😄\n"
    "Причина: нецензурная лексика\n\n"
    "Возможности в чате временно ограничены. Ждите решения администрации.",
]

ADULT_MESSAGES = [
    "🔞 {mention}, такой контент здесь неуместен.\n"
    "Причина: контент 18+\n\n"
    "Возможности в чате временно ограничены до решения администрации.",
]

VOICE_VIOLATION_MESSAGES = [
    "👂 {mention}, я всё услышал. Даже голосовое не спрячется от летописи чата.\n"
    "Причина: {reason}\n\n"
    "До решения администрации ваши возможности временно ограничены.",
]

FORGIVE_MESSAGES = [
    "😇 {mention}, администрация решила вас простить.\nСегодня вам повезло, но летопись всё запомнила 📜",
    "🕊 {mention}, вас помиловали. Пишите дальше, но без фокусов 😄",
    "✌️ {mention}, на этот раз всё. Администрация прощает — чат ждёт нормальных сообщений.",
]

DELETE_MESSAGES = [
    "🚫 Пользователь удалён за нарушение правил.",
    "⛔ Администрация удалила участника за систематическое нарушение правил.",
]

WARN_IN_CHAT = [
    "⚠️ {mention}, вам выдано предупреждение.\nПричина: {reason}\nПредупреждений: {count}/{limit}",
]


def _pick(lst: list, **kwargs) -> str:
    return random.choice(lst).format(**kwargs)


# ══════════════════════════════════════════════════════════════════
# MAIN MESSAGE CHECK
# ══════════════════════════════════════════════════════════════════

async def check_message(update: Update, bot: Bot) -> bool:
    """
    Full moderation check for a message.
    Returns True if message was removed (handler should stop processing).
    """
    msg = update.message
    if not msg or not msg.from_user:
        return False

    user = msg.from_user
    user_id = user.id
    mention = get_user_mention(user)
    full_name = get_full_name(user)
    username = user.username or ""
    text = msg.text or msg.caption or ""

    # Track message time in DB (for stats)
    await db.log_message_time(user_id)
    await db.increment_message_count(user_id)

    # ── 1. Check banlist ─────────────────────────────────────────
    if await db.is_banned(user_id):
        try:
            await msg.delete()
            await ban_user_tg(bot, user_id)
        except Exception:
            pass
        return True

    # ── 2. Sticker spam ─────────────────────────────────────────
    if msg.sticker:
        if _track_sticker(user_id):
            try:
                await msg.delete()
            except Exception:
                pass
            await bot.send_message(
                MAIN_CHAT_ID,
                _pick(STICKER_SPAM_WARNINGS, mention=mention),
                parse_mode="HTML"
            )
        return False  # stickers OK otherwise

    # ── 3. Flood & spam analysis ─────────────────────────────────
    analysis = _track_message(user_id, text)

    if analysis["flood"]:
        await _handle_flood(bot, msg, user_id, mention, full_name, username)
        return True

    if analysis["spam_identical"] and text:
        try:
            await msg.delete()
        except Exception:
            pass
        await bot.send_message(
            MAIN_CHAT_ID,
            _pick(SPAM_IDENTICAL_WARNINGS, mention=mention),
            parse_mode="HTML"
        )
        return True

    if analysis["caps"]:
        await bot.send_message(
            MAIN_CHAT_ID,
            _pick(CAPS_WARNINGS, mention=mention),
            parse_mode="HTML"
        )
        # don't delete, just warn

    if analysis["emoji_spam"]:
        try:
            await msg.delete()
        except Exception:
            pass
        await bot.send_message(
            MAIN_CHAT_ID,
            _pick(EMOJI_SPAM_WARNINGS, mention=mention),
            parse_mode="HTML"
        )
        return True

    # ── 4. Bad words ─────────────────────────────────────────────
    if text:
        found, word = contains_bad_words(text)
        if found:
            try:
                await msg.delete()
            except Exception:
                pass
            await _handle_content_violation(
                bot, user_id, username, full_name, mention,
                reason="нецензурная лексика",
                message_text=text,
                chat_msg=_pick(BAD_WORD_MESSAGES, mention=mention),
            )
            return True

        # ── 5. 18+ content ───────────────────────────────────────
        found_adult, kw = contains_adult_content(text)
        if found_adult:
            try:
                await msg.delete()
            except Exception:
                pass
            await _handle_content_violation(
                bot, user_id, username, full_name, mention,
                reason="контент 18+",
                message_text=text,
                chat_msg=_pick(ADULT_MESSAGES, mention=mention),
            )
            return True

    return False


async def _handle_flood(bot: Bot, msg, user_id: int,
                         mention: str, full_name: str, username: str):
    """Delete message, soft-mute, warn user, notify owner."""
    try:
        await msg.delete()
    except Exception:
        pass

    # Check if already warned about flood recently
    recent_db = await db.get_recent_message_count(user_id, FLOOD_TIME_WINDOW)

    if recent_db >= FLOOD_MAX_MESSAGES + 2:
        # escalate: actual temporary restrict
        until = datetime.now() + timedelta(seconds=FLOOD_MUTE_DURATION)
        await restrict_user(bot, user_id, until=until)
        duration_str = f"{FLOOD_MUTE_DURATION // 60} минут"

        await bot.send_message(
            MAIN_CHAT_ID,
            _pick(FLOOD_MUTE_MESSAGES, mention=mention, duration=duration_str),
            parse_mode="HTML"
        )
        # save violation & notify owner
        v_id = await db.save_violation(
            MAIN_CHAT_ID, user_id, username, full_name,
            reason="Флуд / спам сообщениями",
            message_text=f"[{recent_db} сообщений за {FLOOD_TIME_WINDOW} сек]"
        )
        await send_violation_card(
            bot, v_id, user_id, username, full_name,
            reason="Флуд / спам сообщениями",
            message_text=f"[{recent_db} сообщений за {FLOOD_TIME_WINDOW} сек]"
        )
    else:
        # first warning: no mute yet
        await bot.send_message(
            MAIN_CHAT_ID,
            _pick(FLOOD_WARNINGS, mention=mention),
            parse_mode="HTML"
        )


async def _handle_content_violation(bot: Bot, user_id: int, username: str,
                                     full_name: str, mention: str,
                                     reason: str, message_text: str,
                                     chat_msg: str,
                                     voice_transcript: str = ""):
    """Restrict user, notify chat, send card to owner."""
    # Temporary restrict (pending owner decision)
    await restrict_user(bot, user_id)

    # Notify chat
    await bot.send_message(MAIN_CHAT_ID, chat_msg, parse_mode="HTML")

    # Save to DB
    v_id = await db.save_violation(
        MAIN_CHAT_ID, user_id, username, full_name,
        reason=reason,
        message_text=message_text,
        voice_transcript=voice_transcript,
    )

    # Send card to owner
    await send_violation_card(
        bot, v_id, user_id, username, full_name,
        reason=reason,
        message_text=message_text,
        voice_transcript=voice_transcript,
    )


# ══════════════════════════════════════════════════════════════════
# VOICE MODERATION
# ══════════════════════════════════════════════════════════════════

async def handle_voice_violation(bot: Bot, user_id: int, username: str,
                                  full_name: str, transcript: str, reason: str):
    mention = f"@{username}" if username else full_name
    chat_msg = _pick(VOICE_VIOLATION_MESSAGES, mention=mention, reason=reason)
    await _handle_content_violation(
        bot, user_id, username, full_name, mention,
        reason=reason, message_text="",
        chat_msg=chat_msg,
        voice_transcript=transcript,
    )


# ══════════════════════════════════════════════════════════════════
# OWNER DECISION CALLBACKS
# ══════════════════════════════════════════════════════════════════

async def process_owner_decision(bot: Bot, violation_id: int, action: str):
    """Handle owner button press: delete / warn / restrict / forgive."""
    violation = await db.get_violation(violation_id)
    if not violation:
        return "Нарушение не найдено."

    if violation["status"] != "pending":
        return "Решение по этому нарушению уже принято."

    user_id  = violation["user_id"]
    username = violation["username"] or ""
    full_name = violation["full_name"] or ""
    reason   = violation["reason"]
    mention  = f"@{username}" if username else full_name

    if action == "forgive":
        await unrestrict_user(bot, user_id)
        await db.update_violation_status(violation_id, "forgiven", "Прощён владельцем")
        msg = _pick(FORGIVE_MESSAGES, mention=mention)
        await bot.send_message(MAIN_CHAT_ID, msg, parse_mode="HTML")
        return "✅ Пользователь прощён, ограничение снято."

    elif action == "warn":
        warn_limit = int(await db.get_setting("warn_limit", "3"))
        count = await db.add_warning(user_id, reason)
        await unrestrict_user(bot, user_id)
        await db.update_violation_status(violation_id, "warned", f"Варн #{count}")
        msg = _pick(WARN_IN_CHAT, mention=mention, reason=reason,
                    count=count, limit=warn_limit)
        await bot.send_message(MAIN_CHAT_ID, msg, parse_mode="HTML")

        if count >= warn_limit:
            # Auto-action on warn limit
            auto = await db.get_setting("warn_limit_action", "restrict")
            if auto == "ban":
                await ban_user_tg(bot, user_id)
                await db.ban_user(user_id, username, full_name, f"Достигнут лимит предупреждений: {reason}")
                await bot.send_message(MAIN_CHAT_ID,
                    f"🚫 {mention}, лимит предупреждений исчерпан. "
                    f"Администрация вынуждена применить бан.", parse_mode="HTML")
            elif auto == "kick":
                await kick_user(bot, user_id)
                await bot.send_message(MAIN_CHAT_ID,
                    f"👢 {mention}, лимит предупреждений исчерпан. "
                    f"Администрация временно исключила участника.", parse_mode="HTML")
            else:
                # permanent restrict
                await restrict_user(bot, user_id)
                await bot.send_message(MAIN_CHAT_ID,
                    f"🔇 {mention}, лимит предупреждений исчерпан. "
                    f"Возможности в чате ограничены до решения администрации.", parse_mode="HTML")
        return f"⚠️ Предупреждение #{count} выдано."

    elif action == "delete":
        await ban_user_tg(bot, user_id)
        await db.ban_user(user_id, username, full_name, reason)
        await db.update_violation_status(violation_id, "banned", "Удалён владельцем")
        msg = random.choice(DELETE_MESSAGES)
        await bot.send_message(MAIN_CHAT_ID, msg, parse_mode="HTML")
        return "🗑 Пользователь удалён и добавлен в банлист."

    elif action == "restrict":
        # Owner will send duration next — store pending state
        await db.update_violation_status(violation_id, "restrict_pending", "Ожидает времени ограничения")
        return (
            "⏱ На сколько ограничить пользователя?\n"
            "Напишите время в любом формате:\n"
            "  10 минут\n  2 часа\n  1 день\n  50 дней\n  навсегда\n\n"
            f"<code>restrict_apply:{violation_id}</code>"
        )

    return "Неизвестное действие."


async def apply_restrict_duration(bot: Bot, violation_id: int, duration_text: str):
    """Apply restriction with given duration text."""
    from utils import parse_duration, format_duration
    violation = await db.get_violation(violation_id)
    if not violation:
        return "Нарушение не найдено."

    user_id  = violation["user_id"]
    username = violation["username"] or ""
    full_name = violation["full_name"] or ""
    reason   = violation["reason"]
    mention  = f"@{username}" if username else full_name

    td = parse_duration(duration_text)
    if td is None:
        until = None
        dur_str = "навсегда"
    else:
        until = datetime.now() + td
        from utils import format_duration
        dur_str = format_duration(td)

    await restrict_user(bot, user_id, until=until)
    await db.update_violation_status(violation_id, "restricted",
                                      f"Ограничен на {dur_str}")
    await bot.send_message(
        MAIN_CHAT_ID,
        f"🔇 {mention}, ваши возможности в чате ограничены.\n"
        f"Срок: <b>{dur_str}</b>\n"
        f"Причина: {reason}",
        parse_mode="HTML"
    )
    return f"✅ Ограничение применено: {dur_str}."
