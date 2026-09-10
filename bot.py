"""
AkoAI — matnni o'zbekcha ovozga aylantiruvchi va YouTube/TikTok videolarni
o'zbek tiliga dublyaj qiluvchi Telegram bot.
ElevenLabs Text-to-Speech + Voice Cloning + Dubbing API orqali ishlaydi.

Botdan foydalanish uchun foydalanuvchi @Namanganliklar_uz kanaliga obuna
bo'lgan bo'lishi shart — aks holda bot ishlamaydi.

BUYRUQLAR / FUNKSIYALAR:
  /start           — botni ishga tushirish / yordam
  /clone           — o'z ovozingizni klonlash (ovozli xabar namunasi orqali)
  /default         — standart ovozga qaytish (klonlangan ovozdan voz kechish)
  (oddiy matn)     — matn ovozga aylantiriladi (mp3 audio fayl qilib qaytariladi)
  (YouTube/TikTok link) — 2 daqiqagacha bo'lgan videolar o'zbek tiliga dublyaj
                     qilib qaytariladi; undan uzun videolar rad etiladi

ENV VARIABLES (Railway -> Variables):
  BOT_TOKEN            — @BotFather bergan token
  ELEVENLABS_API_KEY   — ElevenLabs'dan olingan API kalit
  ELEVENLABS_VOICE_ID  — standart ovozning ID'si (Voice Library'dan olinadi)

MUHIM: Botni @Namanganliklar_uz kanaliga ADMIN qilib qo'shish kerak,
aks holda obunani tekshirish ishlamaydi (Telegram talabi shunday).

ESLATMA 1: Klonlangan ovozlar user_voices.json fayliga saqlanadi. Railway'ning
standart (persistent bo'lmagan) diskida bu fayl har yangi deploy'da o'chib
ketishi mumkin — doimiy saqlash kerak bo'lsa, Railway Volume ulash tavsiya etiladi.

ESLATMA 2: Dubbing (video tarjima) funksiyasi ElevenLabs kreditlarini oddiy
Text-to-Speech'ga qaraganda ANCHA ko'proq sarflaydi (video uzunligiga qarab
bir necha daqiqalik audio narxiga teng) — Creator tarifdagi oylik kredit
tez tugashi mumkin, ehtiyot bo'ling. Shuning uchun 2 daqiqadan uzun videolar
avtomatik rad etiladi (kredit sarflanmasdan oldin).

ESLATMA 3: Video davomiyligini aniqlash uchun "yt-dlp" kutubxonasi kerak —
requirements.txt fayliga "yt-dlp" qatorini qo'shishni unutmang, aks holda
bot ishga tushmaydi (ImportError).
"""
import asyncio
import logging
import os
import tempfile
import uuid

import urllib.request
import urllib.error
import json

import yt_dlp

# YouTube/TikTok link aniqlash uchun (dublyaj funksiyasi shu link kelganda ishga tushadi)
VIDEO_URL_REGEX = r"(?i)(youtube\.com/watch\?v=|youtu\.be/|tiktok\.com/)"
DUBBING_TARGET_LANG = "uz"
DUBBING_POLL_INTERVAL = 10  # soniya — status necha soniyada bir tekshiriladi
DUBBING_MAX_WAIT = 600  # soniya — maksimal necha soniya kutiladi (10 daqiqa)
DUBBING_MAX_SECONDS = 120  # 2 daqiqadan uzun videolar rad etiladi

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


def _get_video_duration_sync(url: str) -> float | None:
    """Videoni yuklab olmasdan, uning davomiyligini (soniyalarda) aniqlaydi.
    Aniqlab bo'lmasa (masalan live efir) None qaytaradi."""
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info.get("duration")


def _start_dubbing_sync(source_url: str) -> str:
    """ElevenLabs Dubbing API'ga video linkini yuborib, dubbing_id qaytaradi
    (bloklaydigan/sinxron — alohida threadda ishga tushiriladi)."""
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

    add_field("source_url", source_url)
    add_field("target_lang", DUBBING_TARGET_LANG)
    add_field("source_lang", "auto")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/dubbing",
        data=bytes(body),
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["dubbing_id"]


def _check_dubbing_status_sync(dubbing_id: str) -> dict:
    """Dublyaj holatini tekshiradi: status "dubbing" | "dubbed" | "failed" bo'lishi mumkin."""
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/dubbing/{dubbing_id}",
        headers={"xi-api-key": ELEVENLABS_API_KEY},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_dubbed_media_sync(dubbing_id: str, lang: str) -> bytes:
    """Tayyor bo'lgan dublyaj qilingan video/audio faylini yuklab oladi."""
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/dubbing/{dubbing_id}/audio/{lang}",
        headers={"xi-api-key": ELEVENLABS_API_KEY},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


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
    "\U0001F3A4 O'z ovozingizda gapirtirishni xohlasangiz — /clone buyrug'ini yuboring.\n\n"
    "\U0001F3AC YouTube yoki TikTok video linkini yuborsangiz, uni o'zbek tiliga "
    f"dublyaj qilib qaytaraman ({DUBBING_MAX_SECONDS // 60} daqiqagacha bo'lgan "
    "videolar uchun, bir necha daqiqa vaqt olishi mumkin)."
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
        "Aniq, sokin joyda, tabiiy ohangda gapiring — sifat shunga qarab yaxshi bo'ladi.\n\n"
        "ℹ️ Bot qanday ishlaydi: ovoz namunasini yuborganingizdan so'ng, "
        "bundan buyon menga yozgan HAR QANDAY matn — sizning shu klonlangan ovozingizda "
        "audio qilib qaytariladi. Bu holat siz /default buyrug'ini yubormaguningizcha davom etadi."
    )


@router.message(F.text == "/default")
async def cmd_default(message: Message):
    had_clone = user_voices.pop(str(message.from_user.id), None) is not None
    save_user_voices(user_voices)
    if had_clone:
        await message.answer(
            "\U0001F501 Standart ovozga qaytdingiz.\n\n"
            "ℹ️ Bot qanday ishlaydi: bundan buyon yuborgan matningiz standart "
            "(bot o'zining odatiy) ovozida audio qilib qaytariladi. "
            "Xohlagan vaqtingizda /clone buyrug'i orqali qayta o'z ovozingizga o'tishingiz mumkin."
        )
    else:
        await message.answer(
            "ℹ️ Siz hozir ham standart ovozdasiz — klonlangan ovoz ulanmagan edi.\n\n"
            "O'z ovozingizda gapirtirish uchun /clone buyrug'ini yuboring."
        )


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


@router.message(F.text.regexp(VIDEO_URL_REGEX))
async def handle_video_dub(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return

    url = message.text.strip()
    status = await message.answer("\U0001F50D Video tekshirilmoqda...")

    try:
        duration = await asyncio.to_thread(_get_video_duration_sync, url)
    except Exception as e:
        log.warning(f"Video davomiyligini aniqlashda xato: {e}")
        await status.edit_text(
            "❌ Videoni ochib bo'lmadi. Link to'g'riligini va videoning ochiq "
            "(public) ekanligini tekshiring."
        )
        return

    if duration is None:
        await status.edit_text(
            "❌ Videoning davomiyligini aniqlab bo'lmadi (masalan, live efir bo'lishi mumkin). "
            "Boshqa video bilan urinib ko'ring."
        )
        return

    if duration > DUBBING_MAX_SECONDS:
        mins = int(duration // 60)
        secs = int(duration % 60)
        await status.edit_text(
            f"⚠️ Bu video {mins} daqiqa {secs} soniya — juda uzun.\n\n"
            f"Men faqat {DUBBING_MAX_SECONDS // 60} daqiqagacha bo'lgan videolarni "
            "dublyaj qila olaman. Qisqaroq video yuboring."
        )
        return

    await status.edit_text(
        "\U0001F3AC Video yuklanmoqda va o'zbek tiliga dublyaj qilinmoqda...\n"
        "Bu bir necha daqiqa vaqt olishi mumkin, iltimos kuting."
    )

    try:
        dubbing_id = await asyncio.to_thread(_start_dubbing_sync, url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.warning(f"Dubbing boshlashda xato: {e.code} {body}")
        await status.edit_text(
            "❌ Videoni qabul qilib bo'lmadi. Link to'g'riligini va videoning ochiq "
            "(public) ekanligini tekshiring."
        )
        return
    except Exception as e:
        log.error(f"Kutilmagan xato (dubbing start): {e}")
        await status.edit_text("❌ Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.")
        return

    waited = 0
    final_status = None
    while waited < DUBBING_MAX_WAIT:
        await asyncio.sleep(DUBBING_POLL_INTERVAL)
        waited += DUBBING_POLL_INTERVAL
        try:
            info = await asyncio.to_thread(_check_dubbing_status_sync, dubbing_id)
        except Exception as e:
            log.error(f"Dubbing holatini tekshirishda xato: {e}")
            continue
        st = info.get("status")
        if st == "dubbed":
            final_status = "dubbed"
            break
        if st == "failed":
            final_status = "failed"
            log.warning(f"Dubbing failed: {info.get('error')}")
            break

    if final_status != "dubbed":
        await status.edit_text(
            "❌ Dublyaj yakunlanmadi (vaqt tugadi yoki xato yuz berdi). "
            "Qisqaroq video bilan qayta urinib ko'ring."
        )
        return

    try:
        media_bytes = await asyncio.to_thread(
            _download_dubbed_media_sync, dubbing_id, DUBBING_TARGET_LANG
        )
    except Exception as e:
        log.error(f"Dublyajni yuklab olishda xato: {e}")
        await status.edit_text("❌ Tayyor faylni yuklab bo'lmadi.")
        return

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(media_bytes)
            tmp_path = tmp.name

        try:
            await message.answer_video(video=FSInputFile(tmp_path, filename="dublyaj.mp4"))
        except Exception:
            # Manba audio bo'lsa (video emas), audio fayl sifatida yuboramiz
            await message.answer_audio(
                audio=FSInputFile(tmp_path, filename="dublyaj.mp3"), title="Dublyaj"
            )
        await status.delete()
    except Exception as e:
        log.error(f"Faylni yuborishda xato: {e}")
        await status.edit_text(
            "❌ Tayyor faylni yuborib bo'lmadi (fayl juda katta bo'lishi mumkin — "
            "Telegram bot orqali yuborish uchun 50MB limit bor)."
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


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
