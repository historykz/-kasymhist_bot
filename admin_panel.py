"""
admin_panel.py - панель управления для владельца и администраторов.
- Владелец управляет через ЛС
- Админы тоже получают меню (только свои кнопки)
- Логи всех действий → владельцу
"""

import logging
import re
from datetime import datetime
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update

import database as db
from config import OWNER_ID, MAIN_CHAT_ID
from moderation import (
    restrict_user, unrestrict_user, ban_user_tg, kick_user, unban_user_tg,
    apply_restrict_duration,
)
from utils import get_level_info, parse_duration, format_duration, now_str

logger = logging.getLogger(__name__)

# owner state machine: {user_id: {state, data}}
_states: dict[int, dict] = {}

def set_state(user_id: int, state: str, data: dict = None):
    _states[user_id] = {"state": state, "data": data or {}}

def get_state(user_id: int) -> dict:
    return _states.get(user_id, {"state": None, "data": {}})

def clear_state(user_id: int):
    _states.pop(user_id, None)


# ══════════════════════════════════════════════════════════════════
# MENUS
# ══════════════════════════════════════════════════════════════════

def owner_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Настройки",     callback_data="ap:settings"),
         InlineKeyboardButton("📜 Правила",        callback_data="ap:rules")],
        [InlineKeyboardButton("👋 Приветствие",    callback_data="ap:welcome"),
         InlineKeyboardButton("🛡 Безопасность",   callback_data="ap:security")],
        [InlineKeyboardButton("📢 Канал",          callback_data="ap:channel"),
         InlineKeyboardButton("👑 Управление адм", callback_data="ap:admins")],
        [InlineKeyboardButton("📚 Книги",          callback_data="ap:books"),
         InlineKeyboardButton("🧠 ИИ-история",     callback_data="ap:ai")],
        [InlineKeyboardButton("📊 Статистика",     callback_data="ap:stats"),
         InlineKeyboardButton("🚫 Банлист",        callback_data="ap:banlist")],
        [InlineKeyboardButton("📜 Логи действий",  callback_data="ap:logs"),
         InlineKeyboardButton("⚡ Блиц старт",     callback_data="ap:blitz_start")],
    ])


def admin_menu_keyboard():
    """Menu for non-owner admins."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👮 Нарушения",   callback_data="ap:violations"),
         InlineKeyboardButton("📊 Статистика",  callback_data="ap:stats")],
        [InlineKeyboardButton("🚫 Банлист",     callback_data="ap:banlist"),
         InlineKeyboardButton("⚡ Блиц старт",  callback_data="ap:blitz_start")],
        [InlineKeyboardButton("🛑 Блиц стоп",   callback_data="ap:blitz_stop")],
    ])


async def send_menu(bot: Bot, user_id: int):
    if user_id == OWNER_ID:
        kb   = owner_menu_keyboard()
        text = "👑 <b>Панель владельца - Касым</b>\n\nВыберите раздел:"
    else:
        kb   = admin_menu_keyboard()
        text = "👮 <b>Панель администратора - Касым</b>\n\nВыберите действие:"
    await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)


# ══════════════════════════════════════════════════════════════════
# HANDLE OWNER/ADMIN DM TEXT
# ══════════════════════════════════════════════════════════════════

async def handle_admin_dm(update: Update, bot: Bot) -> bool:
    """Handle text commands in DM from owner or admin. Returns True if handled."""
    msg  = update.message
    if not msg or not msg.text:
        return False

    user_id = msg.from_user.id
    is_owner = (user_id == OWNER_ID)

    # Must be owner or admin
    rank = await db.get_moderator_rank(user_id)
    if not is_owner and rank == 0:
        return False

    text       = msg.text.strip()
    text_lower = text.lower()

    # ── Pending state handling ────────────────────────────────────
    st = get_state(user_id)
    if st["state"] == "await_restrict_duration":
        v_id = st["data"].get("violation_id")
        if v_id:
            result = await apply_restrict_duration(bot, v_id, text)
            await bot.send_message(user_id, result)
            clear_state(user_id)
            return True

    # ── Menu triggers ─────────────────────────────────────────────
    if text_lower in ("меню", "панель", "/start", "/menu", "помощь"):
        await send_menu(bot, user_id)
        return True

    # ── OWNER-ONLY commands ───────────────────────────────────────
    if is_owner:
        handled = await _owner_commands(bot, user_id, text, text_lower)
        if handled:
            return True

    # ── SHARED commands (owner + admins) ──────────────────────────
    return await _shared_commands(bot, user_id, text, text_lower)


async def _owner_commands(bot, user_id, text, text_lower) -> bool:
    """Commands only available to owner."""

    # +Правила
    if text_lower.startswith("+правила"):
        body = text.split("\n", 1)[1].strip() if "\n" in text else ""
        if body:
            await db.set_setting("rules", body)
            await bot.send_message(user_id, "✅ Правила сохранены.")
        else:
            await bot.send_message(user_id, "Напишите правила на новой строке после команды.")
        return True

    if text_lower in ("-правила", "правила удалить"):
        await db.delete_setting("rules")
        await bot.send_message(user_id, "✅ Правила удалены.")
        return True

    # +Приветствие
    if text_lower.startswith("+приветствие"):
        body = text.split("\n", 1)[1].strip() if "\n" in text else ""
        if body:
            await db.set_setting("welcome", body)
            await bot.send_message(user_id, "✅ Приветствие сохранено.")
        else:
            await bot.send_message(user_id, "Напишите приветствие на новой строке.")
        return True

    if text_lower in ("-приветствие", "приветствие удалить"):
        await db.delete_setting("welcome")
        await bot.send_message(user_id, "✅ Приветствие удалено.")
        return True

    # +Канал
    m = re.match(r"\+канал\s+(@\S+)", text_lower)
    if m:
        await db.set_setting("required_channel", m.group(1))
        await bot.send_message(user_id, f"✅ Канал подписки: {m.group(1)}")
        return True
    if text_lower == "-канал":
        await db.delete_setting("required_channel")
        await bot.send_message(user_id, "✅ Канал удалён.")
        return True

    # Проверка подписки
    if text_lower in ("+проверка подписки", "+проверка"):
        await db.set_setting("check_subscription", "1")
        await bot.send_message(user_id, "✅ Проверка подписки включена.")
        return True
    if text_lower in ("-проверка подписки", "-проверка"):
        await db.set_setting("check_subscription", "0")
        await bot.send_message(user_id, "✅ Проверка подписки выключена.")
        return True

    # Режимы
    if text_lower in ("+ручной режим", "+ручной"):
        await db.set_setting("manual_mode", "1")
        await bot.send_message(user_id, "✅ Ручной режим включён.")
        return True
    if text_lower in ("-ручной режим", "-ручной"):
        await db.set_setting("manual_mode", "0")
        await bot.send_message(user_id, "✅ Авторежим включён.")
        return True

    # Авто-блиц
    m = re.match(r"\+автовикторина\s+(.+)", text_lower)
    if m:
        td = parse_duration(m.group(1).strip())
        if td:
            await db.set_setting("auto_blitz_interval", str(int(td.total_seconds())))
            await bot.send_message(user_id, f"✅ Авто-блиц: каждые {format_duration(td)}.")
        return True
    if text_lower == "-автовикторина":
        await db.delete_setting("auto_blitz_interval")
        await bot.send_message(user_id, "✅ Авто-блиц выключен.")
        return True

    # +Админ / +Модер
    m = re.match(r"\+(админ|модер)\s*(\d?)\s*(.*)", text_lower)
    if m:
        rank = int(m.group(2)) if m.group(2) else 1
        target_str = m.group(3).strip()
        uid, uname, fname = await _resolve_user(target_str)
        if uid:
            await db.add_moderator(uid, uname, fname, rank)
            await bot.send_message(user_id,
                f"✅ @{uname or fname} назначен администратором, ранг {rank}.")
        else:
            await bot.send_message(user_id, "Пользователь не найден. Укажите @username или ID.")
        return True

    m = re.match(r"-(админ|модер)\s*(.*)", text_lower)
    if m:
        target_str = m.group(2).strip()
        uid, uname, fname = await _resolve_user(target_str)
        if uid:
            await db.remove_moderator(uid)
            await bot.send_message(user_id, f"✅ @{uname or fname} снят с роли администратора.")
        else:
            await bot.send_message(user_id, "Пользователь не найден.")
        return True

    # Список админов
    if text_lower in ("админы", "модеры", "модераторы"):
        mods = await db.get_all_moderators()
        if not mods:
            await bot.send_message(user_id, "👮 Администраторов нет.")
        else:
            lines = ["👮 <b>Администраторы:</b>\n"]
            for m in mods:
                uname = f"@{m['username']}" if m["username"] else m["full_name"]
                lines.append(f"• {uname} - ранг {m['rank']}")
            await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
        return True

    # Логи
    if text_lower in ("логи", "логи сегодня"):
        today_only = "сегодня" in text_lower
        logs = await db.get_logs(limit=20, today_only=today_only)
        await _send_logs(bot, user_id, logs)
        return True

    m = re.match(r"логи (админа|пользователя)\s+(@?\S+)", text_lower)
    if m:
        target_str = m.group(2)
        uid, _, _ = await _resolve_user(target_str)
        if uid:
            if "админа" in m.group(1):
                logs = await db.get_logs(limit=20, admin_id=uid)
            else:
                logs = await db.get_logs(limit=20, target_id=uid)
            await _send_logs(bot, user_id, logs)
        else:
            await bot.send_message(user_id, "Пользователь не найден.")
        return True

    # Банлист
    if text_lower == "банлист":
        bl = await db.get_banlist()
        if not bl:
            await bot.send_message(user_id, "🚫 Банлист пуст.")
        else:
            lines = ["🚫 <b>Банлист:</b>\n"]
            for b in bl[:20]:
                u = f"@{b['username']}" if b["username"] else f"id{b['user_id']}"
                lines.append(f"• {u} - {b['reason'] or '?'}")
            await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
        return True

    m = re.match(r"банлист добавить\s+(\d+)", text_lower)
    if m:
        uid = int(m.group(1))
        await db.ban_user(uid, "", f"id{uid}", "Добавлен вручную")
        await ban_user_tg(bot, uid)
        await bot.send_message(user_id, f"✅ id{uid} добавлен в банлист.")
        return True

    m = re.match(r"банлист удалить\s+(\d+)", text_lower)
    if m:
        uid = int(m.group(1))
        await db.unban_user(uid)
        await unban_user_tg(bot, uid)
        await bot.send_message(user_id, f"✅ id{uid} удалён из банлиста.")
        return True

    # Варн лимит
    m = re.match(r"варн лимит\s+(\d+)", text_lower)
    if m:
        await db.set_setting("warn_limit", m.group(1))
        await bot.send_message(user_id, f"✅ Лимит предупреждений: {m.group(1)}.")
        return True

    # Книги
    if text_lower == "книги":
        books = await db.get_all_books()
        if not books:
            await bot.send_message(user_id, "📚 Книг нет. Отправьте PDF/DOCX/TXT в ЛС.")
        else:
            icons = {"ready": "✅", "processing": "⏳", "error": "❌", "scanned": "⚠️"}
            lines = ["📚 <b>Книги:</b>\n"]
            for b in books:
                lines.append(f"{icons.get(b['status'],'?')} {b['id']}. {b['title']} - {b['page_count']} стр.")
            await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
        return True

    m = re.match(r"книга удалить\s+(\d+)", text_lower)
    if m:
        await db.delete_book(int(m.group(1)))
        await bot.send_message(user_id, "✅ Книга удалена.")
        return True

    return False


async def _shared_commands(bot, user_id, text, text_lower) -> bool:
    """Commands available to all admins."""
    if text_lower in ("статистика", "стат"):
        from stats import get_chat_stats
        await bot.send_message(user_id, await get_chat_stats(), parse_mode="HTML")
        return True
    return False


async def _send_logs(bot, user_id: int, logs):
    if not logs:
        await bot.send_message(user_id, "📜 Логов нет.")
        return
    lines = ["📜 <b>Журнал действий:</b>\n"]
    for lg in logs:
        admin = f"@{lg['admin_username']}" if lg["admin_username"] else lg["admin_name"]
        target = f"@{lg['target_username']}" if lg["target_username"] else f"id{lg['target_id']}"
        dur = f" [{lg['duration']}]" if lg["duration"] else ""
        dt = lg["created_at"][:16].replace("T", " ")
        lines.append(
            f"<b>{dt}</b> | {admin} → {target}\n"
            f"  <i>{lg['action']}{dur}</i> - {lg['reason']}"
        )
    # Split if too long
    text = "\n\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n\n...(обрезано)"
    await bot.send_message(user_id, text, parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════
# CALLBACK HANDLERS
# ══════════════════════════════════════════════════════════════════

async def handle_admin_callback(update: Update, bot: Bot) -> bool:
    query = update.callback_query
    if not query:
        return False

    user_id  = query.from_user.id
    is_owner = user_id == OWNER_ID
    rank     = await db.get_moderator_rank(user_id)
    if not is_owner and rank == 0:
        return False

    data = query.data
    if not data.startswith("ap:"):
        return False

    await query.answer()
    action = data[3:]

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ap:back")]])

    if action == "back":
        kb = owner_menu_keyboard() if is_owner else admin_menu_keyboard()
        title = "👑 <b>Панель владельца</b>" if is_owner else "👮 <b>Панель администратора</b>"
        await query.edit_message_text(title + "\n\nВыберите раздел:",
                                       parse_mode="HTML", reply_markup=kb)
        return True

    if action == "settings" and is_owner:
        manual   = await db.get_setting("manual_mode", "1")
        sub      = await db.get_setting("check_subscription", "0")
        warn_lim = await db.get_setting("warn_limit", "3")
        await query.edit_message_text(
            f"⚙️ <b>Настройки</b>\n\n"
            f"Ручной режим: {'✅' if manual=='1' else '❌'}\n"
            f"Проверка подписки: {'✅' if sub=='1' else '❌'}\n"
            f"Лимит варнов: {warn_lim}\n\n"
            f"Команды:\n+Ручной режим / -Ручной режим\n"
            f"+Проверка подписки / -Проверка\nВарн лимит 3",
            parse_mode="HTML", reply_markup=back_kb
        )
        return True

    if action == "rules" and is_owner:
        rules = await db.get_setting("rules", "")
        await query.edit_message_text(
            f"📜 <b>Правила:</b>\n\n{rules or '(не установлены)'}\n\n"
            f"Команда: <code>+Правила\nТекст...</code>",
            parse_mode="HTML", reply_markup=back_kb
        )
        return True

    if action == "books" and is_owner:
        books = await db.get_all_books()
        if not books:
            t = "📚 Книг нет. Отправьте PDF/DOCX/TXT в ЛС."
        else:
            icons = {"ready": "✅", "processing": "⏳", "error": "❌", "scanned": "⚠️"}
            lines = ["📚 <b>Книги:</b>\n"]
            for b in books:
                lines.append(f"{icons.get(b['status'],'?')} {b['id']}. {b['title']} - {b['page_count']} стр.")
            lines.append("\nУдалить: <code>Книга удалить ID</code>")
            t = "\n".join(lines)
        await query.edit_message_text(t, parse_mode="HTML", reply_markup=back_kb)
        return True

    if action == "banlist":
        bl = await db.get_banlist()
        if not bl:
            t = "🚫 Банлист пуст."
        else:
            lines = ["🚫 <b>Банлист:</b>\n"]
            for b in bl[:15]:
                u = f"@{b['username']}" if b["username"] else f"id{b['user_id']}"
                lines.append(f"• {u} - {b['reason'] or '?'}")
            t = "\n".join(lines)
        await query.edit_message_text(t, parse_mode="HTML", reply_markup=back_kb)
        return True

    if action == "stats":
        from stats import get_chat_stats
        await query.edit_message_text(
            await get_chat_stats(), parse_mode="HTML", reply_markup=back_kb
        )
        return True

    if action == "admins" and is_owner:
        mods = await db.get_all_moderators()
        if not mods:
            t = "👮 Администраторов нет.\n\nДобавить: <code>+Админ @username</code>"
        else:
            lines = ["👮 <b>Администраторы:</b>\n"]
            for m in mods:
                u = f"@{m['username']}" if m["username"] else m["full_name"]
                lines.append(f"• {u} - ранг {m['rank']}")
            lines.append("\nДобавить: <code>+Админ @username</code>")
            lines.append("Снять: <code>-Админ @username</code>")
            t = "\n".join(lines)
        await query.edit_message_text(t, parse_mode="HTML", reply_markup=back_kb)
        return True

    if action == "logs" and is_owner:
        logs = await db.get_logs(limit=10)
        await query.edit_message_text(
            "📜 Последние 10 действий\n(для полных логов пишите: Логи)",
            parse_mode="HTML", reply_markup=back_kb
        )
        await _send_logs(bot, user_id, logs)
        return True

    if action == "blitz_start":
        from quiz import start_blitz
        await query.edit_message_text("⚡ Запускаю блиц...", parse_mode="HTML")
        await start_blitz(bot)
        return True

    if action == "blitz_stop":
        from quiz import stop_blitz
        await stop_blitz(bot)
        await query.edit_message_text("🛑 Блиц остановлен.", parse_mode="HTML")
        return True

    return False


# ══════════════════════════════════════════════════════════════════
# VIOLATION BUTTON CALLBACKS
# ══════════════════════════════════════════════════════════════════

async def handle_violation_callback(update: Update, bot: Bot) -> bool:
    query = update.callback_query
    if not query or query.from_user.id != OWNER_ID:
        return False

    data = query.data
    if not data.startswith("viol:"):
        return False

    await query.answer()
    parts = data.split(":")
    if len(parts) != 3:
        return False

    _, action, vid_str = parts
    vid = int(vid_str)

    from moderation import process_owner_decision
    result = await process_owner_decision(bot, vid, action)

    if "restrict_apply" in result:
        set_state(OWNER_ID, "await_restrict_duration", {"violation_id": vid})
        await query.edit_message_text(
            "⏱ <b>На сколько ограничить?</b>\n\n"
            "Напишите:\n• 10 минут\n• 2 часа\n• 1 день\n• навсегда",
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
# RESOLVE USER
# ══════════════════════════════════════════════════════════════════

async def _resolve_user(text: str) -> tuple[int | None, str, str]:
    text = text.strip().lstrip("@")
    if text.isdigit():
        uid  = int(text)
        user = await db.get_user(uid)
        if user:
            return uid, user["username"] or "", user["full_name"] or ""
        return uid, "", f"id{uid}"
    user = await db.get_user_by_username(text)
    if user:
        return user["user_id"], user["username"] or "", user["full_name"] or ""
    return None, "", ""
