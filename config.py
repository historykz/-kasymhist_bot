import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///kasym.db")

# Bot personality name
BOT_NAME = "Касым"

# Flood control settings
FLOOD_MAX_MESSAGES = 5        # messages
FLOOD_TIME_WINDOW = 6         # seconds
FLOOD_MUTE_DURATION = 300     # 5 minutes mute for flood

# Spam: identical messages
SPAM_IDENTICAL_COUNT = 3
SPAM_TIME_WINDOW = 10

# Caps lock: if >70% of message is uppercase and len > 10
CAPS_THRESHOLD = 0.70
CAPS_MIN_LEN = 10

# Sticker spam
STICKER_SPAM_COUNT = 4
STICKER_TIME_WINDOW = 15

# Emoji spam
EMOJI_SPAM_COUNT = 10  # more than 10 emojis in one message

# Warning limits before auto-action
DEFAULT_WARN_LIMIT = 3

# XP rewards
XP_FIRST_CORRECT = 5
XP_SECOND_CORRECT = 3
XP_PARTICIPATE = 1

# Levels
LEVELS = [
    (0,   "Новичок аула",  "🏕"),
    (30,  "Жас батыр",     "⚔️"),
    (100, "Знаток степи",  "🌾"),
    (200, "Би знаний",     "⚖️"),
    (400, "Хан истории",   "👑"),
]

# Blitz quiz timeout (seconds)
BLITZ_TIMEOUT = 60

# Auto-blitz interval options (stored as seconds)
AUTO_BLITZ_OPTIONS = {
    "30 минут": 1800,
    "1 час":    3600,
    "2 часа":   7200,
    "1 день":   86400,
}

# Daily fact time (hour, minute) in UTC
DAILY_FACT_TIME = (7, 0)

# Bad words (base list — extend via DB)
BAD_WORDS_RU = [
    "блять", "бля", "блядь", "блядина",
    "ёбаный", "ёб", "еб", "ебать", "ебал", "ебать",
    "пизда", "пиздец", "пиздит", "пиздёж",
    "хуй", "хуйня", "хуйло", "хуета",
    "залупа", "мудак", "мудила", "мудозвон",
    "пидор", "пидар", "пидорас",
    "сука", "суки", "сучка",
    "блин",  # mild — optional
    "нахуй", "нахер", "нахрен",
    "ёпта", "епта",
    "ёпт",
    "курва", "шлюха", "шалава",
    "дрочить", "дрочил",
]

BAD_WORDS_KZ = [
    "сиктир", "сикт", "боқ", "бок",
    "пышқ", "жезөкше",
    "аупырмай",  # mild
]

ALL_BAD_WORDS = BAD_WORDS_RU + BAD_WORDS_KZ

# 18+ keywords (basic)
ADULT_KEYWORDS = [
    "секс", "порно", "эротика", "интим", "оргазм",
    "голая", "голый", "нагой", "раздетый",
    "sex", "porn", "xxx", "nude", "naked", "erotic",
    "мастурбац", "дрочить", "мастурби",
]
