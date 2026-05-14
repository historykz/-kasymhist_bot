import logging
import re
from datetime import datetime, timedelta
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database as db
from config import OWNER_ID, MAIN_CHAT_ID
from moderation import (
    restrict_user, unrestrict_user, ban_user_tg, kick_user,
    apply_restrict_duration
)
from utils import get_level_info, parse_duration, format_duration

logger = logging.getLogger(__name__)

# Track owner's pending state: {owner_id: {"state": str, "data": dict}}
_owner_state: dict[int, dict] = {}


def set_owner_state(state: str, data: dict = None):
    _owner_state[OWNER_ID] = {"state": state, "data": data or {}}


def get_owner_state() -> dict:
    return _owner_state.get(OWNER_ID, {"state": None, "data": {}})


def clear_owner_state():
    _owner_state.pop(OWNER_ID, None)


# ══════════════════════════════════════════════════════════════════
# MAIN MENU
# ══════════════════════════════════════════════════════════════════

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Настройки",   callback_data="admin:settings"),
            InlineKeyboardButton("📜 Правила",      callback_data="admin:rules"),
        ],
        [
            InlineKeyboardButton("👋 Приветствие",  callback_data="admin:welcome"),
            InlineKeyboardButton("🛡 Безопасность", callback_data="admin:security"),
        ],
        [
            InlineKeyboardButton("📢 Канал",        callback_data="admin:channel"),
            InlineKeyboardButton("👮 Модераторы",   callback_data="admin:mods"),
        ],
        [
            InlineKeyboardButton("📚 Книги",        callback_data="admin:books"),
            InlineKeyboardButton("🧠 ИИ-история",   callback_data="admin:ai"),
        ],
        [
            InlineKeyboardButton("📊 Статистика",   callback_data="admin:stats"),
            InlineKeyboardButton("🚫 Банлист",      callback_data="admin:banlist"),
        ],
        [
            InlineKeyboardButton("⚡ Блиц старт",   callback_data="admin:blitz_start"),
            InlineKeyboardButton("🛑 Блиц стоп",    callback_data="admin:blitz_stop"),
        ],
    ])


async def send_main_menu(bot: Bot, chat_id: int):
    await bot.send_message(
        chat_id,
        "⚙️ <b>Панель управления Касым-ботом</b>\n\n"
        "Выберите раздел для настройки:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )


# ══════════════════════════════════════════════════════════════════
# HANDLE OWNER TEXT IN DM
# ══════════════════════════════════════════════════════════════════

async def handle_owner_dm(update: Update, bot: Bot) -> bool:
    """
    Handle owner's DM commands.
    Returns True if handled.
    """
    msg = update.message
    if not msg or not msg.text:
        return False

    text = msg.text.strip()
    state_info = get_owner_state()
    state = state_info["state"]
    data  = state_info["data"]

    # ── Pending: restrict duration input ─────────────────────────
    if state == "await_restrict_duration":
        v_id = data.get("violation_id")
        if v_id:
            result = await apply_restrict_duration(bot, v_id, text)
            await bot.send_message(OWNER_ID, result)
            clear_owner_state()
            return True

    # ── Pending: manual restrict on user ─────────────────────────
    if state == "await_manual_restrict":
        uid = data.get("user_id")
        uname = data.get("username", "")
        if uid:
            td = parse_duration(text)
            until = datetime.now() + td if td else None
            await restrict_user(bot, uid, until=until)
            dur = format_duration(td)
            await bot.send_message(OWNER_ID, f"✅ Пользователь ограничен на {dur}.")
            mention = f"@{uname}" if uname else f"id{uid}"
            await bot.send_message(
                MAIN_CHAT_ID,
                f"🔇 {mention}, возможности в чате ограничены.\nСрок: <b>{dur}</b>",
                parse_mode="HTML"
            )
            clear_owner_state()
            return True

    # ── Commands ─────────────────────────────────────────────────
    text_lower = text.lower()

    # Show menu
    if text_lower in ("меню", "панель", "помощь", "/start", "/menu"):
        await send_main_menu(bot, OWNER_ID)
        return True

    # +Правила
    if text.startswith("+Правила") or text.startswith("+правила"):
        rules = text.split("\n", 1)[1].strip() if "\n" in text else ""
        if rules:
            await db.set_setting("rules", rules)
            await bot.send_message(OWNER_ID, "✅ Правила сохранены.")
        else:
            await bot.send_message(OWNER_ID, "Напишите правила после команды, на новой строке.")
        return True

    # -Правила / Правила удалить
    if text_lower in ("-правила", "правила удалить"):
        await db.delete_setting("rules")
        await bot.send_message(OWNER_ID, "✅ Правила удалены.")
        return True

    # +Приветствие
    if text.startswith("+Приветствие") or text.startswith("+приветствие"):
        welcome = text.split("\n", 1)[1].strip() if "\n" in text else ""
        if welcome:
            await db.set_setting("welcome", welcome)
            await bot.send_message(OWNER_ID, "✅ Приветствие сохранено.")
        else:
            await bot.send_message(OWNER_ID, "Напишите приветствие после команды, на новой строке.")
        return True

    # -Приветствие
    if text_lower in ("-приветствие", "приветствие удалить"):
        await db.delete_setting("welcome")
        await bot.send_message(OWNER_ID, "✅ Приветствие удалено.")
        return True

    # +Канал @channel
    m = re.match(r"\+канал\s+(@\S+)", text_lower)
    if m:
        ch = m.group(1)
        await db.set_setting("required_channel", ch)
        await bot.send_message(OWNER_ID, f"✅ Канал подписки: {ch}")
        return True

    # -Канал
    if text_lower == "-канал":
        await db.delete_setting("required_channel")
        await bot.send_message(OWNER_ID, "✅ Канал подписки удалён.")
        return True

    # +Контакт @username
    m = re.match(r"\+контакт\s+(@\S+)", text_lower)
    if m:
        contact = m.group(1)
        await db.set_setting("contact", contact)
        await bot.send_message(OWNER_ID, f"✅ Контакт установлен: {contact}")
        return True

    # +Проверка подписки / -Проверка подписки
    if text_lower in ("+проверка подписки", "+проверка"):
        await db.set_setting("check_subscription", "1")
        await bot.send_message(OWNER_ID, "✅ Проверка подписки включена.")
        return True
    if text_lower in ("-проверка подписки", "-проверка"):
        await db.set_setting("check_subscription", "0")
        await bot.send_message(OWNER_ID, "✅ Проверка подписки выключена.")
        return True

    # +Ручной режим / -Ручной режим
    if text_lower in ("+ручной режим", "+ручной"):
        await db.set_setting("manual_mode", "1")
        await bot.send_message(OWNER_ID, "✅ Ручной режим включён.")
        return True
    if text_lower in ("-ручной режим", "-ручной"):
        await db.set_setting("manual_mode", "0")
        await bot.send_message(OWNER_ID, "✅ Ручной режим выключён (авто).")
        return True

    # +Автовикторина / -Автовикторина
    m = re.match(r"\+автовикторина\s+(.+)", text_lower)
    if m:
        interval_text = m.group(1).strip()
        td = parse_duration(interval_text)
        if td:
            await db.set_setting("auto_blitz_interval", str(int(td.total_seconds())))
            await bot.send_message(OWNER_ID,
                f"✅ Авто-блиц включён: каждые {format_duration(td)}.")
        return True
    if text_lower == "-автовикторина":
        await db.delete_setting("auto_blitz_interval")
        await bot.send_message(OWNER_ID, "✅ Авто-блиц выключен.")
        return True

    # Банлист
    if text_lower == "банлист":
        banlist = await db.get_banlist()
        if not banlist:
            await bot.send_message(OWNER_ID, "🚫 Банлист пуст.")
        else:
            lines = ["🚫 <b>Банлист:</b>\n"]
            for b in banlist[:20]:
                uname = f"@{b['username']}" if b["username"] else b["full_name"] or f"id{b['user_id']}"
                lines.append(f"• {uname} — {b['reason'] or '?'}")
            await bot.send_message(OWNER_ID, "\n".join(lines), parse_mode="HTML")
        return True

    m = re.match(r"банлист добавить\s+(\d+)", text_lower)
    if m:
        uid = int(m.group(1))
        await db.ban_user(uid, "", f"id{uid}", "Добавлен вручную")
        await ban_user_tg(bot, uid)
        await bot.send_message(OWNER_ID, f"✅ Пользователь {uid} добавлен в банлист.")
        return True

    m = re.match(r"банлист удалить\s+(\d+)", text_lower)
    if m:
        uid = int(m.group(1))
        await db.unban_user(uid)
        await bot.send_message(OWNER_ID, f"✅ Пользователь {uid} удалён из банлиста.")
        return True

    if text_lower == "банлист очистить":
        import aiosqlite
        async with aiosqlite.connect("kasym.db") as d:
            await d.execute("DELETE FROM banlist")
            await d.execute("UPDATE users SET is_banned=0, ban_reason=NULL")
            await d.commit()
        await bot.send_message(OWNER_ID, "✅ Банлист очищен.")
        return True

    # +Модер / +Модер 2..5
    m = re.match(r"\+(модер|админ)\s*(\d?)\s+(.+)", text_lower)
    if m:
        rank = int(m.group(2)) if m.group(2) else 1
        target_text = m.group(3).strip()
        uid, uname, fname = await _resolve_user(target_text)
        if uid:
            await db.add_moderator(uid, uname, fname, rank)
            await bot.send_message(OWNER_ID,
                f"✅ {uname or fname} назначен модератором, ранг {rank}.")
        else:
            await bot.send_message(OWNER_ID, "Пользователь не найден. Укажите ID или @username.")
        return True

    m = re.match(r"-(модер|админ)\s+(.+)", text_lower)
    if m:
        target_text = m.group(2).strip()
        uid, uname, fname = await _resolve_user(target_text)
        if uid:
            await db.remove_moderator(uid)
            await bot.send_message(OWNER_ID, f"✅ {uname or fname} снят с роли модератора.")
        else:
            await bot.send_message(OWNER_ID, "Пользователь не найден.")
        return True

    # Warn limit
    m = re.match(r"варн лимит\s+(\d+)", text_lower)
    if m:
        await db.set_setting("warn_limit", m.group(1))
        await bot.send_message(OWNER_ID, f"✅ Лимит предупреждений: {m.group(1)}.")
        return True

    # Книги — list
    if text_lower == "книги":
        books = await db.get_all_books()
        if not books:
            await bot.send_message(OWNER_ID, "📚 Книг нет. Отправьте PDF, DOCX или TXT в ЛС.")
        else:
            lines = ["📚 <b>Загруженные книги:</b>\n"]
            for b in books:
                status_icon = {"ready": "✅", "processing": "⏳",
                                "error": "❌", "scanned": "⚠️"}.get(b["status"], "?")
                lines.append(f"{status_icon} {b['id']}. {b['title']} — {b['page_count']} стр.")
            await bot.send_message(OWNER_ID, "\n".join(lines), parse_mode="HTML")
        return True

    m = re.match(r"книга удалить\s+(\d+)", text_lower)
    if m:
        bid = int(m.group(1))
        await db.delete_book(bid)
        await bot.send_message(OWNER_ID, f"✅ Книга #{bid} удалена.")
        return True

    if text_lower == "книги очистить":
        import aiosqlite
        async with aiosqlite.connect("kasym.db") as d:
            await d.execute("DELETE FROM book_chunks")
            await d.execute("DELETE FROM books")
            await d.execute("DELETE FROM blitz_questions")
            await d.commit()
        await bot.send_message(OWNER_ID, "✅ Все книги удалены.")
        return True

    # Статистика
    if text_lower in ("статистика", "стат"):
        from stats import get_chat_stats
        text_stat = await get_chat_stats()
        await bot.send_message(OWNER_ID, text_stat, parse_mode="HTML")
        return True

    return False


# ══════════════════════════════════════════════════════════════════
# CALLBACK HANDLERS
# ══════════════════════════════════════════════════════════════════

async def handle_admin_callback(update: Update, bot: Bot) -> bool:
    """Handle admin panel inline button callbacks."""
    query = update.callback_query
    if not query or query.from_user.id != OWNER_ID:
        return False

    data = query.data
    await query.answer()

    if data == "admin:settings":
        manual = await db.get_setting("manual_mode", "1")
        check_sub = await db.get_setting("check_subscription", "0")
        warn_limit = await db.get_setting("warn_limit", "3")
        text = (
            f"⚙️ <b>Настройки чата</b>\n\n"
            f"Ручной режим: {'✅ вкл' if manual=='1' else '❌ выкл'}\n"
            f"Проверка подписки: {'✅ вкл' if check_sub=='1' else '❌ выкл'}\n"
            f"Лимит варнов: {warn_limit}\n\n"
            f"Команды:\n"
            f"+Ручной режим / -Ручной режим\n"
            f"+Проверка подписки / -Проверка подписки\n"
            f"Варн лимит 3"
        )
        await query.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=_back_keyboard())
        return True

    if data == "admin:rules":
        rules = await db.get_setting("rules", "")
        text = (
            f"📜 <b>Текущие правила:</b>\n\n{rules or '(не установлены)'}\n\n"
            f"Чтобы изменить, отправьте:\n<code>+Правила\nТекст правил...</code>"
        )
        await query.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=_back_keyboard())
        return True

    if data == "admin:welcome":
        welcome = await db.get_setting("welcome", "")
        text = (
            f"👋 <b>Приветствие:</b>\n\n{welcome or '(не установлено)'}\n\n"
            f"Переменные: {{имя}}, {{ссылка}}\n"
            f"Команда: <code>+Приветствие\nТекст...</code>"
        )
        await query.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=_back_keyboard())
        return True

    if data == "admin:books":
        books = await db.get_all_books()
        if not books:
            text = "📚 Книг нет.\n\nОтправьте мне PDF, DOCX или TXT файл в этот чат."
        else:
            lines = ["📚 <b>Книги:</b>\n"]
            for b in books:
                icon = {"ready": "✅", "processing": "⏳",
                        "error": "❌", "scanned": "⚠️"}.get(b["status"], "?")
                lines.append(f"{icon} {b['id']}. {b['title']} — {b['page_count']} стр.")
            lines.append("\nУдалить: <code>Книга удалить ID</code>")
            text = "\n".join(lines)
        await query.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=_back_keyboard())
        return True

    if data == "admin:banlist":
        banlist = await db.get_banlist()
        if not banlist:
            text = "🚫 Банлист пуст."
        else:
            lines = ["🚫 <b>Банлист:</b>\n"]
            for b in banlist[:15]:
                uname = f"@{b['username']}" if b["username"] else f"id{b['user_id']}"
                lines.append(f"• {uname} — {b['reason'] or '?'}")
            text = "\n".join(lines)
        await query.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=_back_keyboard())
        return True

    if data == "admin:stats":
        from stats import get_chat_stats
        text = await get_chat_stats()
        await query.edit_message_text(text, parse_mode="HTML",
                                       reply_markup=_back_keyboard())
        return True

    if data == "admin:blitz_start":
        from quiz import start_blitz
        await query.edit_message_text("⚡ Запускаю блиц...", parse_mode="HTML")
        await start_blitz(bot)
        return True

    if data == "admin:blitz_stop":
        from quiz import stop_blitz
        await stop_blitz(bot)
        await query.edit_message_text("🛑 Блиц остановлен.", parse_mode="HTML")
        return True

    if data == "admin:back":
        await query.edit_message_text(
            "⚙️ <b>Панель управления Касым-ботом</b>\n\nВыберите раздел:",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
        return True

    return False


def _back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data="admin:back")]
    ])


# ══════════════════════════════════════════════════════════════════
# VIOLATION CALLBACKS
# ══════════════════════════════════════════════════════════════════

async def handle_violation_callback(update: Update, bot: Bot) -> bool:
    """Handle viol:action:id callbacks from owner."""
    query = update.callback_query
    if not query or query.from_user.id != OWNER_ID:
        return False

    cb = query.data
    if not cb.startswith("viol:"):
        return False

    await query.answer()
    parts = cb.split(":")
    if len(parts) != 3:
        return False

    _, action, violation_id_str = parts
    violation_id = int(violation_id_str)

    from moderation import process_owner_decision
    result = await process_owner_decision(bot, violation_id, action)

    if action == "restrict" and "restrict_apply" in result:
        # Need duration input
        set_owner_state("await_restrict_duration", {"violation_id": violation_id})
        await query.edit_message_text(
            "⏱ <b>На сколько ограничить?</b>\n\n"
            "Напишите время:\n• 10 минут\n• 2 часа\n• 1 день\n• 50 дней\n• навсегда",
            parse_mode="HTML"
        )
    else:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await bot.send_message(OWNER_ID, result)

    return True


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

async def _resolve_user(text: str) -> tuple[int | None, str, str]:
    """Try to find user by @username or ID string."""
    text = text.strip().lstrip("@")
    if text.isdigit():
        uid = int(text)
        user = await db.get_user(uid)
        if user:
            return uid, user["username"] or "", user["full_name"] or ""
        return uid, "", f"id{uid}"

    # search by username
    import aiosqlite
    async with aiosqlite.connect("kasym.db") as d:
        d.row_factory = aiosqlite.Row
        async with d.execute(
            "SELECT * FROM users WHERE LOWER(username)=?", (text.lower(),)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row["user_id"], row["username"] or "", row["full_name"] or ""
    return None, "", ""
