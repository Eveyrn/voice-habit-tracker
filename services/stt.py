import os
import asyncio
from groq import Groq
import config

client = Groq(api_key=config.GROQ_API_KEY)

# Подсказка Whisper: слова, которые часто звучат в голосовых дневниках и легко искажаются.
# Пишется как образец текста, не как инструкция. К ней добавляется «о себе» человека (/about) —
# там его собственные слова: названия проектов, имена и т. п. («бот» → «бат», «ELGE» → «элге»).
WHISPER_HINT = (
    "Голосовой дневник дня. Работа, учёба, встреча, тренировка, прогулка, залипание в телефоне, сериал, "
    "завтрак, обед, ужин, витамины, таблетки."
)


def _transcribe_sync(file_path: str, hint: str) -> str:
    with open(file_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            file=(os.path.basename(file_path), audio_file.read()),
            model="whisper-large-v3",
            language="ru",
            prompt=hint,
            response_format="text"
        )
    return transcription.strip()


async def transcribe_audio(file_path: str, about: str = "") -> str:
    """Расшифровка голосового через Groq Whisper (в отдельном потоке, чтобы не блокировать бота)."""
    hint = f"{WHISPER_HINT} {about.strip()[:300]}".strip()
    return await asyncio.to_thread(_transcribe_sync, file_path, hint)
