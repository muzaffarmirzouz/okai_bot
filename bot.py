"""
AkoAI — matnni o'zbekcha ovozga aylantiruvchi Telegram bot.
ElevenLabs Text-to-Speech API orqali ishlaydi.

ENV VARIABLES (Railway -> Variables):
  BOT_TOKEN            — @BotFather bergan token
  ELEVENLABS_API_KEY   — ElevenLabs'dan olingan API kalit
  ELEVENLABS_VOICE_ID  — ishlatiladigan ovozning ID'si (Voice Library'dan olinadi)
"""
import asyncio
import logging
import os
import tempfile

import urllib.request
import urllib.error
import json

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, BotCommand, FSInputFile
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("akoai-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]

# Eleven v3 modelining bitta so'rovdagi belgi chegarasi ~3000. Xavfsizlik
# uchun biroz kamroq chegara qo'yamiz, aks holda API xato qaytaradi.
MAX_CHARS = 2800

router = Router()


def _generate_speech_sync(text: str) -> bytes:
    """ElevenLabs API'ga so'rov yuborib, audio baytlarini qaytaradi
    (bloklaydigan/sinxron — alohida threadda ishga tushiriladi)."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    payload = {
        "text": text,
        "model_id": "eleven_v3",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


@router.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "\U0001F916 AkoAI — matnni o'zbekcha ovozga aylantiruvchi bot\n\n"
        "Menga istalgan matnni yozing, men uni ovozli xabar qilib qaytaraman.\n\n"
        f"Bir martada {MAX_CHARS} belgigacha matn qabul qilaman."
    )


@router.message(F.text.startswith("/"))
async def ignore_unknown_commands(message: Message):
    # Noma'lum buyruqlarni matn sifatida ovozga aylantirmaslik uchun
    return


@router.message(F.text)
async def handle_text(message: Message):
    text = message.text.strip()
    if not text:
        return

    if len(text) > MAX_CHARS:
        await message.answer(
            f"\u26A0\uFE0F Matn juda uzun ({len(text)} belgi). "
            f"Iltimos, {MAX_CHARS} belgidan qisqaroq matn yuboring."
        )
        return

    status = await message.answer("\U0001F3A7 Ovoz yaratilmoqda...")

    try:
        audio_bytes = await asyncio.wait_for(
            asyncio.to_thread(_generate_speech_sync, text), timeout=60
        )
    except asyncio.TimeoutError:
        await status.edit_text("\u274C Vaqt tugadi, birozdan keyin qayta urinib ko'ring.")
        return
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.warning(f"ElevenLabs xato: {e.code} {body}")
        await status.edit_text("\u274C Ovoz yaratib bo'lmadi. Birozdan keyin qayta urinib ko'ring.")
        return
    except Exception as e:
        log.error(f"Kutilmagan xato: {e}")
        await status.edit_text("\u274C Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.")
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        await message.answer_voice(voice=FSInputFile(tmp_path))
        await status.delete()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Botni ishga tushirish / yordam"),
    ])

    log.info("AkoAI bot ishga tushmoqda...")
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
