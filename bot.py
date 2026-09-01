"""
AkoAI — matnni o'zbekcha ovozga aylantiruvchi Telegram bot.
ElevenLabs Text-to-Speech + Voice Cloning API orqali ishlaydi.

Botdan foydalanish uchun foydalanuvchi @Namanganliklar_uz kanaliga obuna
bo'lgan bo'lishi shart — aks holda bot ishlamaydi.

BUYRUQLAR:
  /start   — botni ishga tushirish / yordam
  /clone   — o'z ovozingizni klonlash (ovozli xabar namunasi orqali)
  /default — standart ovozga qaytish (klonlangan ovozdan voz kechish)

ENV VARIABLES (Railway -> Variables):
  BOT_TOKEN            — @BotFather bergan token
  ELEVENLABS_API_KEY   — ElevenLabs'dan olingan API kalit
  ELEVENLABS_VOICE_ID  — standart ovozning ID'si (Voice Library'dan olinadi)

MUHIM: Botni @Namanganliklar_uz kanaliga ADMIN qilib qo'shish kerak,
aks holda obunani tekshirish ishlamaydi (Telegram talabi shunday).

ESLATMA: Klonlangan ovozlar user_voices.json fayliga saqlanadi. Railway'ning
standart (persistent bo'lmagan) diskida bu fayl har yangi deploy'da o'chib
ketishi mumkin — doimiy saqlash kerak bo'lsa, Railway Volume ulash tavsiya etiladi.
"""
import asyncio
import logging
import os
import tempfile
import uuid

import urllib.request
import urllib.error
import json

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    BotCommand,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("akoai-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]

# Majburiy obuna kanali
CHANNEL_USERNAME = "@Namanganliklar_uz"
CHANNEL_URL = "https://t.me/Namanganliklar_uz"

# Eleven v3 modelining bitta so'rovdagi belgi chegarasi ~3000. Xavfsizlik
# uchun biroz kamroq chegara qo'yamiz, aks holda API xato qaytaradi.
MAX_CHARS = 2800

# Klonlangan ovozlar: {"<user_id>": "<voice_id>"} ko'rinishida saqlanadi
USER_VOICES_FILE = "user_voices.json"

router = Router()

# /clone buyrug'idan keyin ovoz namunasi kutilayotgan foydalanuvchilar
pending_clone: set[int] = set()


def load_user_voices() -> dict:
    if os.path.exists(USER_VOICES_FILE):
        try:
            with open(USER_VOICES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_user_voices(data: dict) -> None:
    with open(USER_VOICES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


user_voices = load_user_voices()


def _generate_speech_sync(text: str, voice_id: str) -> bytes:
    """ElevenLabs API'ga so'rov yuborib, audio baytlarini qaytaradi
    (bloklaydigan/sinxron — alohida threadda ishga tushiriladi)."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
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


def _clone_voice_sync(name: str, audio_bytes: bytes, filename: str) -> str:
    """ElevenLabs'ga ovoz namunasini yuborib, yangi klonlangan ovoz yaratadi
    va uning voice_id'sini qaytaradi (bloklaydigan/sinxron)."""
    boundary = uuid.uuid4().hex
    body = bytearray()

    def add_field(field_name: str, value: str):
        body.extend(
            (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'
                f'{value}\r\n'
            ).encode("utf-8")
        )

    def add_file(field_name: str, fname: str, content: bytes, content_type: str):
        body.extend(
            (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{field_name}"; filename="{fname}"\r\n'
                f'Content-Type: {content_type}\r\n\r\n'
            ).encode("utf-8")
        )
        body.extend(content)
        body.extend(b"\r\n")

    add_field("name", name)
    add_file("files", filename, audio_bytes, "application/octet-stream")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices/add",
        data=bytes(body),
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["voice_id"]


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    """Foydalanuvchi CHANNEL_USERNAME kanaliga obuna bo'lganini tekshiradi.
    Bot shu kanalda ADMIN bo'lishi shart, aks holda Telegram xato qaytaradi."""
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status not in ("left", "kicked")
    except TelegramBadRequest as e:
        log.error(
            f"Obunani tekshirib bo'lmadi (bot kanalga admin qilib qo'shilganmi?): {e}"
        )
        return False


def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Kanalga obuna bo'lish", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")],
        ]
    )


SUBSCRIBE_TEXT = (
    "\U0001F512 Botdan foydalanish uchun avval quyidagi kanalga obuna bo'ling:\n\n"
    f"{CHANNEL_USERNAME}\n\n"
    "Obuna bo'lgach, pastdagi \"✅ Tekshirish\" tugmasini bosing."
)

WELCOME_TEXT = (
    "\U0001F916 AkoAI — matnni ovozga aylantiruvchi bot\n\n"
    "Menga istalgan matnni yozing, men uni mp3 audio fayl qilib qaytaraman.\n\n"
    f"Bir martada {MAX_CHARS} belgigacha matn qabul qilaman.\n\n"
    "\U0001F3A4 O'z ovozingizda gapirtirishni xohlasangiz — /clone buyrug'ini yuboring."
)


@router.message(F.text == "/start")
async def cmd_start(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return
    await message.answer(WELCOME_TEXT)


@router.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, callback.from_user.id):
        await callback.message.edit_text(WELCOME_TEXT)
        await callback.answer("✅ Obuna tasdiqlandi!")
    else:
        await callback.answer("❌ Siz hali kanalga obuna bo'lmagansiz.", show_alert=True)


@router.message(F.text == "/clone")
async def cmd_clone(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return

    pending_clone.add(message.from_user.id)
    await message.answer(
        "\U0001F3A4 O'z ovozingizni klonlash uchun menga 20-60 soniyalik ovozli xabar "
        "(yoki audio fayl) yuboring.\n\n"
        "Aniq, sokin joyda, tabiiy ohangda gapiring — sifat shunga qarab yaxshi bo'ladi."
    )


@router.message(F.text == "/default")
async def cmd_default(message: Message):
    user_voices.pop(str(message.from_user.id), None)
    save_user_voices(user_voices)
    await message.answer("\U0001F501 Standart ovozga qaytdingiz.")


@router.message(F.text.startswith("/"))
async def ignore_unknown_commands(message: Message):
    # Noma'lum buyruqlarni matn sifatida ovozga aylantirmaslik uchun
    return


@router.message(F.voice | F.audio)
async def handle_voice_sample(message: Message, bot: Bot):
    user_id = message.from_user.id
    if user_id not in pending_clone:
        # /clone buyrug'i berilmagan bo'lsa, kelgan audioga e'tibor bermaymiz
        return
    pending_clone.discard(user_id)

    status = await message.answer("\U0001F9EC Ovozingiz o'rganilmoqda, biroz kuting...")

    try:
        file_obj = message.voice or message.audio
        tg_file = await bot.get_file(file_obj.file_id)
        buf = await bot.download_file(tg_file.file_path)
        audio_bytes = buf.read()

        voice_id = await asyncio.wait_for(
            asyncio.to_thread(
                _clone_voice_sync, f"user_{user_id}", audio_bytes, "sample.ogg"
            ),
            timeout=90,
        )
        user_voices[str(user_id)] = voice_id
        save_user_voices(user_voices)

        await status.edit_text(
            "✅ Ovozingiz muvaffaqiyatli klonlandi!\n\n"
            "Endi menga yuborgan har qanday matn shu ovozda o'qiladi.\n"
            "Standart ovozga qaytish uchun /default buyrug'ini yuboring."
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Vaqt tugadi, qaytadan /clone buyrug'ini yuborib urinib ko'ring.")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.warning(f"Voice cloning xato: {e.code} {body}")
        await status.edit_text(
            "❌ Ovozni klonlab bo'lmadi (namuna juda qisqa yoki noaniq bo'lishi mumkin). "
            "Qaytadan /clone buyrug'ini yuborib urinib ko'ring."
        )
    except Exception as e:
        log.error(f"Kutilmagan xato (voice cloning): {e}")
        await status.edit_text("❌ Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.")


@router.message(F.text)
async def handle_text(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return

    text = message.text.strip()
    if not text:
        return

    if len(text) > MAX_CHARS:
        await message.answer(
            f"⚠️ Matn juda uzun ({len(text)} belgi). "
            f"Iltimos, {MAX_CHARS} belgidan qisqaroq matn yuboring."
        )
        return

    voice_id = user_voices.get(str(message.from_user.id), ELEVENLABS_VOICE_ID)
    status = await message.answer("\U0001F3A7 Ovoz yaratilmoqda...")

    try:
        audio_bytes = await asyncio.wait_for(
            asyncio.to_thread(_generate_speech_sync, text, voice_id), timeout=60
        )
    except asyncio.TimeoutError:
        await status.edit_text("❌ Vaqt tugadi, birozdan keyin qayta urinib ko'ring.")
        return
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.warning(f"ElevenLabs xato: {e.code} {body}")
        await status.edit_text("❌ Ovoz yaratib bo'lmadi. Birozdan keyin qayta urinib ko'ring.")
        return
    except Exception as e:
        log.error(f"Kutilmagan xato: {e}")
        await status.edit_text("❌ Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.")
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        await message.answer_audio(
            audio=FSInputFile(tmp_path, filename="AkoAI.mp3"),
            title="AkoAI",
        )
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
        BotCommand(command="clone", description="O'z ovozingizni klonlash"),
        BotCommand(command="default", description="Standart ovozga qaytish"),
    ])

    log.info("AkoAI bot ishga tushmoqda...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
