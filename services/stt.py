import os
import asyncio
from groq import Groq
import config

client = Groq(api_key=config.GROQ_API_KEY)

# Подсказка Whisper: слова, которые часто звучат в голосовых и легко искажаются
# («бот» → «бат», «ELGE» → «элге»). Пишется как образец текста, не как инструкция.
WHISPER_HINT = (
    "Голосовой дневник дня. Работала над ботом, писала код, программировала, "
    "проект ELGE, Telegram-бот, нейросеть, дизайн, Figma. "
    "Залипала в телефоне, сериал, гречка, витамины, зарядка, прогулка."
)


def _transcribe_sync(file_path: str) -> str:
    with open(file_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            file=(os.path.basename(file_path), audio_file.read()),
            model="whisper-large-v3",
            language="ru",
            prompt=WHISPER_HINT,
            response_format="text"
        )
    return transcription.strip()


async def transcribe_audio(file_path: str) -> str:
    """Расшифровка голосового через Groq Whisper (в отдельном потоке, чтобы не блокировать бота)."""
    return await asyncio.to_thread(_transcribe_sync, file_path)
