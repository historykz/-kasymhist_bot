import logging
import random
import re
from config import OPENAI_API_KEY, BOT_NAME
import database as db

logger = logging.getLogger(__name__)

try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ── Personality prompts ───────────────────────────────────────────────────────

KASYM_SYSTEM_PROMPT = """Ты — Касым, умный и чуть саркастичный учебный бот по Истории Казахстана.

Твой характер:
- Строгий, но с юмором. Никогда не грубишь, но умеешь подколоть по-доброму.
- Говоришь как живой человек, не как робот. Варьируй стиль.
- Используешь исторические отсылки: «Совет биев одобряет», «Абылай хан бы оценил», «летопись чата».
- Иногда добавляешь 😄 🔥 📜 ⚖️ — но не перебарщиваешь.
- Адаптируешься под собеседника: с молодым — легко, с серьёзным — по делу.
- Никогда не пишешь мат, 18+, не оскорбляешь людей.
- Отвечаешь кратко и по существу. Без воды.

Если вопрос НЕ по истории Казахстана:
- Отвечаешь коротко и кратко — как умный человек, а не как энциклопедия.
- Если вопрос явно плохой/неуместный — мягко останавливаешь.

Если вопрос по Истории Казахстана:
- Отвечаешь строго по предоставленным фрагментам книг.
- Указываешь книгу и страницу.
- Если данных нет — честно говоришь об этом."""

ADAPTIVE_STYLES = {
    "formal": "Отвечай академично и чётко, без шуток.",
    "casual": "Отвечай легко, с юмором, как будто разговариваешь с другом.",
    "motivate": "Мотивируй пользователя изучать историю.",
    "strict": "Будь строгим и требовательным, как учитель.",
}

# ── User style memory (in-memory, per session) ───────────────────────────────
# {user_id: {"style": str, "msg_count": int, "topics": [str]}}
_user_profiles: dict[int, dict] = {}


def _get_or_create_profile(user_id: int) -> dict:
    if user_id not in _user_profiles:
        _user_profiles[user_id] = {
            "style": "casual",
            "msg_count": 0,
            "last_topics": [],
            "formality": 0,  # -2..+2: negative=casual, positive=formal
        }
    return _user_profiles[user_id]


def _update_profile(user_id: int, text: str):
    """Adapt bot style based on user's writing style."""
    p = _get_or_create_profile(user_id)
    p["msg_count"] += 1
    text_lower = text.lower()

    # Detect formality signals
    formal_words = ["пожалуйста", "будьте добры", "не могли бы", "хотелось бы", "уважаемый"]
    casual_words = ["привет", "хай", "чё", "норм", "ок", "окей", "лол", "хаха", "😄", "😂"]

    for w in formal_words:
        if w in text_lower:
            p["formality"] = min(2, p["formality"] + 1)
    for w in casual_words:
        if w in text_lower:
            p["formality"] = max(-2, p["formality"] - 1)

    # Assign style
    if p["formality"] >= 1:
        p["style"] = "formal"
    else:
        p["style"] = "casual"


def _build_system_prompt(user_id: int, context_chunks: list[dict] = None) -> str:
    p = _get_or_create_profile(user_id)
    style_note = ADAPTIVE_STYLES.get(p["style"], "")
    prompt = KASYM_SYSTEM_PROMPT
    if style_note:
        prompt += f"\n\nСтиль для этого пользователя: {style_note}"

    if context_chunks:
        prompt += "\n\n📚 ФРАГМЕНТЫ ИЗ КНИГ (используй ТОЛЬКО эти данные для ответа по истории):\n"
        for i, chunk in enumerate(context_chunks, 1):
            prompt += (
                f"\n[{i}] Книга: «{chunk.get('book_title','?')}», "
                f"стр. {chunk.get('page_number','?')}\n"
                f"{chunk.get('chunk_text','')[:600]}\n"
            )
    return prompt


# ── AI call ───────────────────────────────────────────────────────────────────

async def ask_kasym(user_id: int, question: str,
                     history: list[dict] = None) -> str:
    """
    Main AI answer function.
    history: list of {"role": "user"/"assistant", "content": str}
    """
    _update_profile(user_id, question)

    # Search book chunks for history questions
    context_chunks = []
    if _is_history_question(question):
        context_chunks = await db.search_chunks_by_text(question, limit=4)

    system = _build_system_prompt(user_id, context_chunks)

    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return _fallback_answer(question, context_chunks)

    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-6:])  # last 3 exchanges
    messages.append({"role": "user", "content": question})

    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=600,
            temperature=0.75,
        )
        answer = response.choices[0].message.content.strip()

        # Append source reference if we used book chunks
        if context_chunks:
            chunk = context_chunks[0]
            answer += (
                f"\n\n📚 <b>Источник:</b>\n"
                f"Книга: {chunk.get('book_title','?')}\n"
                f"Страница: {chunk.get('page_number','?')}"
            )
        return answer

    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return _fallback_answer(question, context_chunks)


def _is_history_question(text: str) -> bool:
    """Quick check if question is about Kazakhstan history."""
    kw = [
        "казах", "ханств", "хан ", "батыр", "жуз", "степ", "история",
        "год", "войн", "восстан", "бий", "абылай", "кенесары", "аблай",
        "тауке", "жанибек", "керей", "чингис", "монгол", "орда",
        "совет", "советск", "независим", "тоқаев", "назарбаев",
        "ssr", "қазақ", "дешт", "кипчак", "сак", "скиф", "usun",
    ]
    text_lower = text.lower()
    return any(k in text_lower for k in kw)


def _fallback_answer(question: str, chunks: list[dict]) -> str:
    """Answer without OpenAI using book chunks directly."""
    if chunks:
        chunk = chunks[0]
        text = chunk.get("chunk_text", "")[:400]
        return (
            f"📘 <b>Ответ (по книге):</b>\n{text}\n\n"
            f"📚 <b>Источник:</b>\n"
            f"Книга: {chunk.get('book_title','?')}\n"
            f"Страница: {chunk.get('page_number','?')}"
        )
    fallbacks = [
        "🤔 Хм, на этот вопрос у меня нет данных в загруженных книгах. "
        "Загрузите ещё материал или уточните вопрос.",
        "📚 В загруженных материалах я не нашёл точного ответа. "
        "Лучше уточнить по учебнику или загрузить ещё источник.",
    ]
    return random.choice(fallbacks)


# ── Generate blitz question from chunk ───────────────────────────────────────

async def generate_blitz_question(book_id: int = None) -> dict | None:
    """
    Generate a blitz question from a random book chunk.
    Returns dict with question/answer/topic/explanation/page/book_title
    or None if failed.
    """
    chunk = await db.get_random_chunk()
    if not chunk:
        return None

    chunk_text = chunk["chunk_text"]
    book_title = chunk["book_title"]
    page = chunk["page_number"]

    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return _fallback_question(chunk)

    prompt = (
        f"Ты — составитель вопросов по Истории Казахстана.\n\n"
        f"Вот фрагмент из книги «{book_title}», страница {page}:\n"
        f"{chunk_text[:800]}\n\n"
        f"Составь ОДИН чёткий вопрос с конкретным кратким ответом (1–3 слова или год).\n"
        f"Ответ должен однозначно следовать из текста.\n\n"
        f"Верни ТОЛЬКО JSON без markdown в таком формате:\n"
        f'{{"question": "...", "answer": "...", "topic": "...", "explanation": "..."}}'
    )

    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.5,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL).strip()

        import json
        data = json.loads(raw)
        data["book_title"] = book_title
        data["page_number"] = page
        data["book_id"] = chunk["book_id"]
        return data

    except Exception as e:
        logger.error(f"generate_blitz_question error: {e}")
        return _fallback_question(chunk)


def _fallback_question(chunk: dict) -> dict:
    """Simple fallback question when AI is unavailable."""
    text = chunk["chunk_text"]
    # Try to extract a year
    years = re.findall(r"\b(1[0-9]{3}|20[0-2][0-9])\b", text)
    if years:
        year = years[0]
        # Find context around year
        idx = text.find(year)
        context = text[max(0, idx-60):idx+60].strip()
        return {
            "question": f"Какой год упоминается в контексте: «{context[:100]}»?",
            "answer": year,
            "topic": "Хронология",
            "explanation": context,
            "book_title": chunk["book_title"],
            "page_number": chunk["page_number"],
            "book_id": chunk["book_id"],
        }
    return None


# ── Check if question is inappropriate ───────────────────────────────────────

INAPPROPRIATE_RESPONSES = [
    "Так, юный батыр, такие вопросы в нашем ханстве не обсуждают 😄 Держим уровень.",
    "Совет биев не одобряет этот вопрос 😄 Давай по теме — история Казахстана ждёт.",
    "Летопись чата такое не фиксирует 📜 Задай нормальный вопрос.",
    "Абылай хан бы поморщился от такого вопроса 😄 Давай серьёзнее.",
]


def get_inappropriate_response() -> str:
    return random.choice(INAPPROPRIATE_RESPONSES)


# ── Daily historical fact ─────────────────────────────────────────────────────

async def get_daily_fact() -> str | None:
    """Get a random historical fact from books."""
    chunk = await db.get_random_chunk()
    if not chunk:
        return None

    text = chunk["chunk_text"][:300]
    book = chunk["book_title"]
    page = chunk["page_number"]

    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return (
            f"📜 <b>Исторический факт дня:</b>\n\n"
            f"{text}\n\n"
            f"📚 {book}, стр. {page}"
        )

    prompt = (
        f"Из этого фрагмента книги «{book}» выдели один интересный исторический факт "
        f"про Казахстан. Напиши его красиво, 2–3 предложения, без воды.\n\n{text}"
    )
    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.7,
        )
        fact = resp.choices[0].message.content.strip()
        return (
            f"📜 <b>Исторический факт дня:</b>\n\n"
            f"{fact}\n\n"
            f"📚 {book}, стр. {page}"
        )
    except Exception as e:
        logger.error(f"get_daily_fact error: {e}")
        return None


# ── Check answer correctness ─────────────────────────────────────────────────

async def check_answer_ai(user_answer: str, correct_answer: str) -> bool:
    """
    Use AI to check if user's answer matches correct answer semantically.
    Falls back to simple string matching.
    """
    # Simple match first
    u = user_answer.lower().strip()
    c = correct_answer.lower().strip()
    if u == c or c in u or u in c:
        return True

    # Remove common particles
    for particle in ["год", "г.", "хан", "бий", "батыр", "хана"]:
        u = u.replace(particle, "").strip()
        c = c.replace(particle, "").strip()
    if u == c or c in u:
        return True

    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return False

    try:
        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"Правильный ответ: «{correct_answer}»\n"
                    f"Ответ пользователя: «{user_answer}»\n\n"
                    f"Означают ли они одно и то же? Ответь ТОЛЬКО: да или нет"
                )
            }],
            max_tokens=5,
            temperature=0,
        )
        result = resp.choices[0].message.content.strip().lower()
        return result.startswith("да")
    except Exception:
        return False
