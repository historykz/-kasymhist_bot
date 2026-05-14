import os
import logging
import tempfile
from telegram import Bot, Message

logger = logging.getLogger(__name__)

try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from config import OPENAI_API_KEY
from utils import contains_bad_words, contains_adult_content


async def transcribe_voice(bot: Bot, message: Message) -> str | None:
    """
    Download voice/video_note and transcribe via OpenAI Whisper.
    Returns transcript text or None on failure.
    """
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        logger.warning("OpenAI not configured - skipping voice transcription")
        return None

    file_obj = message.voice or message.video_note
    if not file_obj:
        return None

    try:
        tg_file = await bot.get_file(file_obj.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        with open(tmp_path, "rb") as audio_file:
            response = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru",
            )
        transcript = response.text
        logger.info(f"Voice transcribed: {transcript[:80]}...")
        return transcript

    except Exception as e:
        logger.error(f"Voice transcription error: {e}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def check_voice_transcript(transcript: str) -> tuple[bool, str]:
    """
    Check transcript for violations.
    Returns (violation_found, reason).
    """
    found_bad, word = contains_bad_words(transcript)
    if found_bad:
        return True, "Ð½ÐµÑÐµÐ½Ð·ÑÑÐ½Ð°Ñ Ð»ÐµÐºÑÐ¸ÐºÐ° Ð² Ð³Ð¾Ð»Ð¾ÑÐ¾Ð²Ð¾Ð¼"

    found_adult, kw = contains_adult_content(transcript)
    if found_adult:
        return True, "ÐºÐ¾Ð½ÑÐµÐ½Ñ 18+ Ð² Ð³Ð¾Ð»Ð¾ÑÐ¾Ð²Ð¾Ð¼"

    return False, ""
