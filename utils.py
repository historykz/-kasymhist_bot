import re
import emoji
from datetime import datetime, timedelta
from config import LEVELS, ALL_BAD_WORDS, ADULT_KEYWORDS


def get_user_mention(user) -> str:
    """Return @username or full name."""
    if user.username:
        return f"@{user.username}"
    return user.full_name or f"id{user.id}"


def get_full_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip() or f"id{user.id}"


def get_level_info(xp: int) -> tuple:
    """Return (level_name, emoji, next_level_xp)."""
    current = LEVELS[0]
    for lvl in LEVELS:
        if xp >= lvl[0]:
            current = lvl
        else:
            break
    # find next level
    idx = LEVELS.index(current)
    if idx + 1 < len(LEVELS):
        next_xp = LEVELS[idx + 1][0]
    else:
        next_xp = None
    return current[1], current[2], next_xp


def parse_duration(text: str) -> timedelta | None:
    """
    Parse human-readable duration.
    Examples: '10 минут', '2 часа', '1 день', '50 дней', 'навсегда'
    Returns timedelta or None for 'навсегда'.
    """
    text = text.lower().strip()
    if text in ("навсегда", "бессрочно", "forever", "permanent"):
        return None  # None = permanent

    patterns = [
        (r"(\d+)\s*сек", lambda m: timedelta(seconds=int(m.group(1)))),
        (r"(\d+)\s*мин", lambda m: timedelta(minutes=int(m.group(1)))),
        (r"(\d+)\s*час", lambda m: timedelta(hours=int(m.group(1)))),
        (r"(\d+)\s*д(ень|ня|ней|ней|н[ьея])", lambda m: timedelta(days=int(m.group(1)))),
        (r"(\d+)\s*нед", lambda m: timedelta(weeks=int(m.group(1)))),
        (r"(\d+)\s*month", lambda m: timedelta(days=int(m.group(1)) * 30)),
    ]
    for pattern, handler in patterns:
        m = re.search(pattern, text)
        if m:
            return handler(m)
    return timedelta(minutes=5)  # fallback


def format_duration(td: timedelta | None) -> str:
    if td is None:
        return "навсегда"
    total = int(td.total_seconds())
    if total < 60:
        return f"{total} сек"
    elif total < 3600:
        return f"{total // 60} мин"
    elif total < 86400:
        return f"{total // 3600} ч"
    else:
        return f"{total // 86400} дн"


def contains_bad_words(text: str) -> tuple[bool, str]:
    """Check for profanity. Returns (found, matched_word)."""
    text_lower = text.lower()
    # Remove spaces between letters to catch "х у й" style
    text_nospace = re.sub(r"\s+", "", text_lower)
    # Also check with common leet replacements
    text_leet = (text_nospace
                 .replace("0", "о").replace("3", "е").replace("4", "а")
                 .replace("@", "а").replace("$", "с").replace("1", "и")
                 .replace("!", "и"))

    for word in ALL_BAD_WORDS:
        word_clean = word.lower()
        if word_clean in text_lower or word_clean in text_nospace or word_clean in text_leet:
            return True, word
        # partial match for root forms (e.g. "ебан" matches "ебаный")
        if len(word_clean) >= 4 and word_clean[:4] in text_leet:
            return True, word
    return False, ""


def contains_adult_content(text: str) -> tuple[bool, str]:
    """Check for 18+ content."""
    text_lower = text.lower()
    for kw in ADULT_KEYWORDS:
        if kw in text_lower:
            return True, kw
    return False, ""


def count_emojis(text: str) -> int:
    return len([c for c in text if c in emoji.EMOJI_DATA])


def is_caps_spam(text: str) -> bool:
    """Check if message is mostly caps."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 10:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) > 0.70


def clean_text(text: str) -> str:
    """Remove excessive whitespace."""
    return re.sub(r"\s+", " ", text).strip()


def now_str() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def escape_md(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))
