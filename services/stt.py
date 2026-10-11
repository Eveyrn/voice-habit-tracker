import os
from groq import Groq
import config

client = Groq(api_key=config.GROQ_API_KEY)

async def transcribe_audio(file_path: str) -> str:
    """?????????????? ????????? ????? Groq Whisper API ?? ???? ???????."""
    with open(file_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            file=(os.path.basename(file_path), audio_file.read()),
            model="whisper-large-v3",
            language="ru",
            response_format="text"
        )
    return transcription.strip()
