"""
AkoAI — matnni o'zbekcha ovozga aylantiruvchi va YouTube/TikTok videolarni
o'zbek tiliga dublyaj qiluvchi Telegram bot.
ElevenLabs Text-to-Speech + Voice Cloning + Dubbing API orqali ishlaydi.

Botdan foydalanish uchun foydalanuvchi @Namanganliklar_uz kanaliga obuna
bo'lgan bo'lishi shart — aks holda bot ishlamaydi.

Foydalanuvchi ikkita rejim orasida tugmalar orqali tanlov qiladi ("🔊 Textni
audio qilish" / "🎬 Video tarjima") — shu tufayli link yoki matn noto'g'ri
rejimda ishlab ketmaydi.

BUYRUQLAR / FUNKSIYALAR:
  /start           — botni ishga tushirish, rejim tanlash tugmalari chiqadi
  /menu            — rejimni istalgan vaqtda qayta tanlash
  /clone           — o'z ovozingizni klonlash (ovozli xabar namunasi orqali)
  /default         — standart ovozga qaytish (klonlangan ovozdan voz kechish)
  "🔊 Textni audio qilish" rejimida — matn ovozga aylantiriladi (mp3 fayl)
  "🎬 Video tarjima" rejimida — YouTube/TikTok linki (2 daqiqagacha) o'zbek
                     tiliga dublyaj qilinadi; undan uzun videolar rad etiladi

ENV VARIABLES (Railway -> Variables):
  BOT_TOKEN            — @BotFather bergan token
  ELEVENLABS_API_KEY   — ElevenLabs'dan olingan API kalit
  ELEVENLABS_VOICE_ID  — standart ovozning ID'si (Voice Library'dan olinadi)

MUHIM: Botni @Namanganliklar_uz kanaliga ADMIN qilib qo'shish kerak,
aks holda obunani tekshirish ishlamaydi (Telegram talabi shunday).

ESLATMA 1: Klonlangan ovozlar user_voices.json fayliga saqlanadi. Railway'ning
standart (persistent bo'lmagan) diskida bu fayl har yangi deploy'da o'chib
ketishi mumkin — doimiy saqlash kerak bo'lsa, Railway Volume ulash tavsiya etiladi.

ESLATMA 2: ElevenLabs'ning tayyor Dubbing API'si o'zbek tilini target sifatida
QO'LLAB-QUVVATLAMAYDI ("Target language 'uz' is not supported"). Shuning uchun
video tarjima o'z pipeline'imiz orqali qilinadi: video yuklab olinadi (yt-dlp)
-> nutq matnga aylantiriladi (mahalliy/bepul Whisper — faster-whisper) -> matn
o'zbek tiliga tarjima qilinadi (deep-translator / Google Translate) -> tarjima
ElevenLabs Text-to-Speech orqali ovozga aylantiriladi -> yangi ovoz videoga
ffmpeg bilan qayta joylanadi. Natijada lab-sync mukammal bo'lmasligi mumkin
(audio uzunligi original video bilan aynan mos kelmasligi mumkin).

ESLATMA 3: Bir nechta tashqi vosita kerak:
  - requirements.txt fayliga "yt-dlp", "deep-translator" va "faster-whisper"
    qatorlarini qo'shing
  - Railway'da RAILPACK_DEPLOY_APT_PACKAGES o'zgaruvchisiga "ffmpeg" ni qo'shing
  Bularsiz bot ishga tushmaydi yoki dublyaj funksiyasi ishlamaydi.

ESLATMA 4: Matnga aylantirish (Speech-to-Text) endi mahalliy Whisper orqali
BEPUL amalga oshiriladi (ElevenLabs kredit sarflamaydi) — ElevenLabs faqat
Text-to-Speech (ovoz generatsiyasi) uchun ishlatiladi. Whisper modeli hajmini
WHISPER_MODEL_SIZE o'zgaruvchisi orqali sozlash mumkin (standart: "base";
kichikroq/tezroq uchun "tiny", sifatliroq uchun "small" — Railway serveri
resurslariga qarab tanlang).
"""
import asyncio
import base64
import html
import logging
import os
import re
import subprocess
import tempfile
import threading
import uuid

import urllib.request
import urllib.error
import json

import yt_dlp
import time

from deep_translator import GoogleTranslator, MyMemoryTranslator

# YouTube/TikTok link aniqlash uchun (dublyaj funksiyasi shu link kelganda ishga tushadi)
# re.search bilan ishlatiladi, shuning uchun link matnning istalgan joyida
# ("https://www.youtube.com/..." kabi, boshida turmasa ham) topiladi.
VIDEO_URL_REGEX = re.compile(
    r"(youtube\.com/watch\?v=|youtube\.com/shorts/|youtu\.be/|tiktok\.com/)",
    re.IGNORECASE,
)
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

# ESLATMA: YouTube ba'zan server (datacenter) IP-manzillaridan kelgan so'rovlarni
# "bot" deb hisoblab, "Sign in to confirm you're not a bot" xatosi bilan bloklaydi.
# Buni chetlab o'tish uchun brauzerdan eksport qilingan cookies.txt faylini Railway
# Variable sifatida qo'shish mumkin (ixtiyoriy — bo'lmasa, YouTube ba'zi videolarni
# berishdan bosh tortishi mumkin). Ikki xil variable qo'llab-quvvatlanadi:
#   YOUTUBE_COOKIES      — cookies.txt matnini TO'G'RIDAN-TO'G'RI (base64siz) joylang
#   YOUTUBE_COOKIES_B64   — yoki cookies.txt'ning Base64 shakli
# Fayl noto'g'ri/buzilgan bo'lsa ham bot yiqilmaydi — shunchaki cookies'siz davom
# etadi (aks holda buzilgan fayl BARCHA video so'rovlarini buzib qo'yishi mumkin edi).
_log = logging.getLogger("akoai-bot")


def _load_youtube_cookies() -> str | None:
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        b64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
        if not b64:
            return None
        try:
            raw = base64.b64decode(b64).decode("utf-8")
        except Exception as e:
            _log.warning(f"YOUTUBE_COOKIES_B64 noto'g'ri/buzilgan, cookies'siz davom etiladi: {e}")
            return None

    if "netscape" not in raw.lower() and "\t" not in raw:
        _log.warning("YOUTUBE_COOKIES(_B64) cookies.txt formatiga o'xshamayapti, e'tiborga olinmaydi.")
        return None

    try:
        path = os.path.join(tempfile.gettempdir(), "youtube_cookies.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        return path
    except Exception as e:
        _log.warning(f"Cookies faylini yozishda xato, cookies'siz davom etiladi: {e}")
        return None


YOUTUBE_COOKIES_FILE: str | None = _load_youtube_cookies()

# Majburiy obuna kanali
CHANNEL_USERNAME = "@Namanganliklar_uz"
CHANNEL_URL = "https://t.me/Namanganliklar_uz"

# Eleven v3 modelining bitta so'rovdagi belgi chegarasi ~3000. Xavfsizlik
# uchun biroz kamroq chegara qo'yamiz, aks holda API xato qaytaradi.
MAX_CHARS = 2800

# ESLATMA: Bepul/norasmiy tarjima xizmatlari (Google Translate scraping, MyMemory)
# tez-tez limitga tushib qoladi (bir nechta video ketma-ket yuborilganda ayniqsa).
# Shu sababli, agar GOOGLE_TRANSLATE_API_KEY o'rnatilgan bo'lsa, bot RASMIY Google
# Cloud Translation API'ni ishlatadi (oyiga 500,000 belgigacha bepul, ancha
# barqaror). O'rnatilmagan bo'lsa, eski (kamroq ishonchli) bepul usulga qaytadi.
GOOGLE_TRANSLATE_API_KEY = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "").strip()

# Klonlangan ovozlar: {"<user_id>": "<voice_id>"} ko'rinishida saqlanadi
USER_VOICES_FILE = "user_voices.json"

router = Router()

# /clone buyrug'idan keyin ovoz namunasi kutilayotgan foydalanuvchilar
pending_clone: set[int] = set()

# Foydalanuvchining tanlagan rejimi: "tts" (matn -> ovoz) yoki "dub" (video tarjima)
# Standart holat — "tts"
user_mode: dict[int, str] = {}


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


def _base_ydl_opts() -> dict:
    """Barcha yt-dlp chaqiruvlari uchun umumiy sozlamalar.

    ESLATMA: YouTube 2026-yilda "n-challenge" degan JavaScript-asoslangan
    tekshiruvni joriy qildi — buni yechish uchun yt-dlp'ga Deno (JS runtime)
    kerak (nixpacks.toml'da qo'shilgan). "ios" klientini ATAYLAB ishlatmaymiz,
    chunki u cookie orqali autentifikatsiyani e'tiborsiz qoldiradi (OAuth talab
    qiladi). "web" klienti esa serverlardan kelgan so'rovlarni "sign in to
    confirm you're not a bot" bilan bloklashi mumkin (hatto cookie bilan ham) —
    shuning uchun avval sign-in devoridan xoli "android"/"tv" klientlarini,
    keyin cookie foydali bo'lishi mumkin bo'lgan "web"/"mweb"'ni sinaymiz.
    """
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "extractor_args": {"youtube": {"player_client": ["android", "tv", "web", "mweb"]}},
    }
    if YOUTUBE_COOKIES_FILE:
        opts["cookiefile"] = YOUTUBE_COOKIES_FILE
    return opts


def _get_video_duration_sync(url: str) -> float | None:
    """Videoni yuklab olmasdan, uning davomiyligini (soniyalarda) aniqlaydi.
    Aniqlab bo'lmasa (masalan live efir) None qaytaradi."""
    ydl_opts = _base_ydl_opts()
    ydl_opts["skip_download"] = True
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info.get("duration")


def _download_video_sync(url: str, output_path: str) -> None:
    """Videoni (audio bilan birga) berilgan fayl yo'liga yuklab oladi."""
    ydl_opts = _base_ydl_opts()
    ydl_opts["merge_output_format"] = "mp4"
    ydl_opts["outtmpl"] = output_path
    ydl_opts["overwrites"] = True
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base").strip() or "base"
_whisper_model = None
_whisper_model_lock = threading.Lock()


def _get_whisper_model():
    """Whisper modelini birinchi chaqiruvda yuklaydi va keyingi chaqiruvlar
    uchun xotirada saqlaydi (qayta-qayta yuklamaslik uchun)."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_model_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                log.info(
                    f"Whisper modeli ({WHISPER_MODEL_SIZE}) yuklanmoqda — "
                    "birinchi marta biroz vaqt olishi mumkin..."
                )
                _whisper_model = WhisperModel(
                    WHISPER_MODEL_SIZE, device="cpu", compute_type="int8"
                )
                log.info("Whisper modeli tayyor.")
    return _whisper_model


def _transcribe_video_sync(file_path: str) -> str:
    """Whisper (faster-whisper, mahalliy va BEPUL) orqali audio faylni
    matnga aylantiradi (bloklaydigan/sinxron — alohida threadda ishga
    tushiriladi).
    ESLATMA: bu yerga faqat AUDIO fayl (mp3) berilishi kerak, video emas —
    _extract_audio_sync orqali oldindan ajratib olinadi.
    ESLATMA 2: avval bu yerda ElevenLabs Speech-to-Text ishlatilgan edi
    (kredit sarflardi) — endi ElevenLabs faqat Text-to-Speech (ovoz
    generatsiyasi) uchun ishlatiladi, matnga aylantirish esa mahalliy va
    bepul Whisper orqali amalga oshiriladi."""
    model = _get_whisper_model()
    segments, _info = model.transcribe(file_path, beam_size=5)
    return " ".join(segment.text.strip() for segment in segments).strip()


def _is_translation_noop(original: str, translated: str) -> bool:
    """Tarjima natijasi asl matn bilan bir xil bo'lsa — demak tarjima
    amalda ishlamagan (xizmat vaqtincha bloklagan/limitga tushgan) degani."""
    return (
        translated.strip().lower() == original.strip().lower()
        and len(original.strip()) > 15
    )


# ESLATMA: MyMemory kabi bepul tarjima xizmatlari xato/limit holatida HTTP xato
# QAYTARMAYDI — buning o'rniga "translatedText" maydonining o'ziga inglizcha xato
# xabarini yozib yuboradi (masalan "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE
# TRANSLATIONS..." yoki "... ERROR 500 ..."). Shuni tarjima deb qabul qilib olsak,
# video aynan shu xato matnini ovozga aylantirib yuboradi — bu foydalanuvchi
# ko'rgan holat. Shu sababli natijani qo'shimcha tekshiramiz.
_TRANSLATION_ERROR_SIGNATURES = (
    "mymemory warning",
    "query length limit",
    "invalid source",
    "invalid target",
    "please select two distinct languages",
    "too many requests",
    "quota",
    "not available right now",
    "translation unavailable",
    "amount of words",
)


def _looks_like_service_error(text: str) -> bool:
    lower = text.strip().lower()
    if any(sig in lower for sig in _TRANSLATION_ERROR_SIGNATURES):
        return True
    # "error 500", "500 error", "error: 503" kabi HTTP status kod + error/warning so'zi
    if re.search(r"\b(4\d{2}|5\d{2})\b", lower) and ("error" in lower or "warning" in lower):
        return True
    return False


def _translate_chunk_official_sync(chunk: str) -> str:
    """Rasmiy Google Cloud Translation API v2 orqali tarjima qiladi
    (GOOGLE_TRANSLATE_API_KEY o'rnatilgan bo'lsa ishlatiladi — bepul/norasmiy
    usullardan ancha barqaror, chunki haqiqiy HTTP xato qaytaradi (masalan
    limit tugasa) va hech qachon xato xabarini "tarjima" sifatida qaytarmaydi)."""
    body = json.dumps({"q": chunk, "target": "uz", "format": "text"}).encode("utf-8")
    req = urllib.request.Request(
        f"https://translation.googleapis.com/language/translate/v2?key={GOOGLE_TRANSLATE_API_KEY}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["data"]["translations"][0]["translatedText"]
    return html.unescape(text)


def _translate_chunk_sync(chunk: str) -> str:
    """Bitta matn bo'lagini o'zbek tiliga tarjima qiladi.

    ESLATMA: Bepul/rasmiy bo'lmagan tarjima xizmatlari (Google Translate va
    hokazo) server (datacenter) IP-manzillaridan kelgan so'rovlarni ba'zan
    vaqtincha bloklaydi yoki jim tarzda o'zgarishsiz matn qaytaradi — bu
    ayniqsa ketma-ket ko'p so'rov yuborilganda (bir nechta video) yuz beradi.
    Shu sababli: (1) bir necha marta qayta urinamiz, (2) muvaffaqiyatsiz
    bo'lsa boshqa (zaxira) xizmatga o'tamiz — aks holda video "tarjima
    qilingandek" ko'rinib, aslida asl (masalan inglizcha) matn ovozga
    aylantirilib yuboriladi."""
    if GOOGLE_TRANSLATE_API_KEY:
        try:
            return _translate_chunk_official_sync(chunk)
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="ignore")
            log.error(f"Google Cloud Translate API xato: {e.code} {body_text}")
            raise RuntimeError(f"Google Cloud Translate API xato: {e.code}") from e
        except Exception as e:
            log.error(f"Google Cloud Translate API kutilmagan xato: {e}")
            raise RuntimeError(f"Google Cloud Translate API xato: {e}") from e

    # --- GOOGLE_TRANSLATE_API_KEY o'rnatilmagan bo'lsa, eski (kamroq ishonchli)
    # bepul/norasmiy usulga qaytamiz ---
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            result = GoogleTranslator(source="auto", target="uz").translate(chunk)
            if (
                result and result.strip()
                and not _is_translation_noop(chunk, result)
                and not _looks_like_service_error(result)
            ):
                return result
            log.warning(
                f"Google Translate no-op/bo'sh/xato natija berdi (urinish {attempt + 1}/3): "
                f"{result!r}"
            )
        except Exception as e:
            last_error = e
            log.warning(f"Google Translate xato (urinish {attempt + 1}/3): {e}")
        time.sleep(2 * (attempt + 1))

    log.warning("Google Translate ishlamadi — zaxira xizmat (MyMemory) sinaladi.")
    try:
        # MyMemory bepul tarifda ~500 belgigacha qabul qiladi, shuning uchun kichikroq
        # bo'laklarga bo'lamiz.
        sub_chunks = [chunk[i:i + 480] for i in range(0, len(chunk), 480)] or [chunk]
        parts = []
        for sub in sub_chunks:
            r = MyMemoryTranslator(source="auto", target="uz").translate(sub)
            if not r or not r.strip():
                raise RuntimeError("MyMemory bo'sh natija qaytardi.")
            if _looks_like_service_error(r):
                raise RuntimeError(f"MyMemory xato xabarini qaytardi: {r!r}")
            parts.append(r)
        result = " ".join(parts)
        if not _is_translation_noop(chunk, result):
            return result
        log.warning("MyMemory Translator ham no-op natija berdi.")
    except Exception as e:
        last_error = e
        log.warning(f"MyMemory Translator xato: {e}")

    raise RuntimeError(f"Tarjima xizmatlari javob bermadi (oxirgi xato: {last_error})")


def _translate_to_uzbek_sync(text: str) -> str:
    """Matnni o'zbek tiliga tarjima qiladi. Uzun matn bo'laklarga bo'lib tarjima qilinadi."""
    chunks = [text[i:i + 4500] for i in range(0, len(text), 4500)] or [text]
    translated_parts = [_translate_chunk_sync(chunk) for chunk in chunks]
    final_text = " ".join(p for p in translated_parts if p)
    log.info(f"Tarjima natijasi (birinchi 200 belgi): {final_text[:200]!r}")
    return final_text


def _extract_audio_sync(video_path: str, audio_out_path: str) -> None:
    """Video faylidan faqat audio yo'lini ajratib oladi (ffmpeg orqali).

    ESLATMA: Butun video faylini (video+audio birga) to'g'ridan-to'g'ri
    Speech-to-Text'ga yuborish ba'zan noto'g'ri/"gallyutsinatsiya qilingan"
    (haqiqiy nutqqa aloqasi yo'q, ko'pincha bir xil) natija berishi mumkin.
    Shuning uchun avval toza audio ajratib olinadi.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-q:a", "4",
            audio_out_path,
        ],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg (audio ajratish) xato: {result.stderr.decode(errors='ignore')[-500:]}")


def _mux_audio_into_video_sync(video_path: str, audio_path: str, output_path: str) -> None:
    """ffmpeg yordamida videoning audio yo'lini yangi (o'zbekcha) audio bilan almashtiradi."""
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            output_path,
        ],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg xato: {result.stderr.decode('utf-8', errors='ignore')[-500:]}"
        )


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
    "\U0001F916 AkoAI botiga xush kelibsiz!\n\n"
    "Men ikki xil ishni qila olaman:\n"
    "\U0001F50A Textni audio qilish — matningizni mp3 ovozga aylantiraman.\n"
    "\U0001F3AC Video tarjima — YouTube/TikTok videosini o'zbek tiliga dublyaj qilaman "
    f"({DUBBING_MAX_SECONDS // 60} daqiqagacha bo'lgan videolar uchun).\n\n"
    "Quyidan kerakli rejimni tanlang \U0001F447"
)

MODE_TTS_TEXT = (
    "\U0001F50A Rejim: Textni audio qilish\n\n"
    "Menga istalgan matnni yozing, men uni mp3 audio fayl qilib qaytaraman.\n"
    f"Bir martada {MAX_CHARS} belgigacha matn qabul qilaman.\n\n"
    "\U0001F3A4 O'z ovozingizda gapirtirishni xohlasangiz — /clone buyrug'ini yuboring.\n\n"
    "Boshqa rejimga o'tish uchun /menu buyrug'ini yuboring."
)

MODE_DUB_TEXT = (
    "\U0001F3AC Rejim: Video tarjima (Dubbing)\n\n"
    "Menga YouTube yoki TikTok video linkini yuboring — men uni o'zbek tiliga "
    f"tarjima qilib qaytaraman ({DUBBING_MAX_SECONDS // 60} daqiqagacha bo'lgan videolar uchun).\n\n"
    "Jarayon bir necha bosqichdan iborat (yuklash → matnga aylantirish → tarjima → "
    "ovoz yaratish → video yig'ish), shuning uchun bir necha daqiqa vaqt olishi mumkin.\n\n"
    "Boshqa rejimga o'tish uchun /menu buyrug'ini yuboring."
)


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="\U0001F50A Textni audio qilish", callback_data="mode_tts")],
            [InlineKeyboardButton(text="\U0001F3AC Video tarjima", callback_data="mode_dub")],
        ]
    )


@router.message(F.text == "/start")
async def cmd_start(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return
    await message.answer(WELCOME_TEXT, reply_markup=mode_keyboard())


@router.message(F.text == "/menu")
async def cmd_menu(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return
    await message.answer("Qaysi rejimda ishlashni xohlaysiz?", reply_markup=mode_keyboard())


@router.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, callback.from_user.id):
        await callback.message.edit_text(WELCOME_TEXT, reply_markup=mode_keyboard())
        await callback.answer("✅ Obuna tasdiqlandi!")
    else:
        await callback.answer("❌ Siz hali kanalga obuna bo'lmagansiz.", show_alert=True)


@router.callback_query(F.data == "mode_tts")
async def set_mode_tts(callback: CallbackQuery):
    user_mode[callback.from_user.id] = "tts"
    await callback.message.edit_text(MODE_TTS_TEXT)
    await callback.answer("🔊 Textni audio qilish rejimi tanlandi")


@router.callback_query(F.data == "mode_dub")
async def set_mode_dub(callback: CallbackQuery):
    user_mode[callback.from_user.id] = "dub"
    await callback.message.edit_text(MODE_DUB_TEXT)
    await callback.answer("🎬 Video tarjima rejimi tanlandi")


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


async def do_dubbing(message: Message, url: str) -> None:
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

    video_path = tempfile.mktemp(suffix=".mp4")
    extracted_audio_path = tempfile.mktemp(suffix=".mp3")
    audio_path = tempfile.mktemp(suffix=".mp3")
    final_path = tempfile.mktemp(suffix=".mp4")

    try:
        await status.edit_text("\U0001F4E5 Video yuklab olinmoqda...")
        await asyncio.to_thread(_download_video_sync, url, video_path)

        # Nutqni aniqroq tanish uchun avval videodan faqat audio ajratib olinadi
        # (butun videoni to'g'ridan-to'g'ri yuborish gallyutsinatsiyaga olib kelgan edi).
        await asyncio.to_thread(_extract_audio_sync, video_path, extracted_audio_path)

        await status.edit_text("\U0001F4DD Nutq matnga aylantirilmoqda...")
        original_text = await asyncio.to_thread(_transcribe_video_sync, extracted_audio_path)
        log.info(f"Transkripsiya (birinchi 200 belgi): {original_text[:200]!r}")
        if not original_text.strip():
            await status.edit_text(
                "❌ Videoda tushunarli nutq topilmadi (faqat musiqa/shovqin bo'lishi mumkin)."
            )
            return

        await status.edit_text("\U0001F1FA\U0001F1FF O'zbek tiliga tarjima qilinmoqda...")
        translated_text = await asyncio.to_thread(_translate_to_uzbek_sync, original_text)

        await status.edit_text("\U0001F3A4 O'zbekcha ovoz yaratilmoqda...")
        speech_bytes = await asyncio.to_thread(
            _generate_speech_sync, translated_text, ELEVENLABS_VOICE_ID
        )
        with open(audio_path, "wb") as f:
            f.write(speech_bytes)

        await status.edit_text("\U0001F3AC Video yig'ilmoqda...")
        await asyncio.to_thread(_mux_audio_into_video_sync, video_path, audio_path, final_path)

        await message.answer_video(video=FSInputFile(final_path, filename="dublyaj.mp4"))
        await status.delete()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.warning(f"Dublyaj pipeline xato (API): {e.code} {body}")
        await status.edit_text("❌ Xatolik yuz berdi. Birozdan keyin qayta urinib ko'ring.")
    except Exception as e:
        log.error(f"Dublyaj pipeline xato: {e}")
        await status.edit_text(
            "❌ Videoni dublyaj qilib bo'lmadi (fayl juda katta yoki xato yuz berdi bo'lishi mumkin). "
            "Boshqa video bilan qayta urinib ko'ring."
        )
    finally:
        for p in (video_path, extracted_audio_path, audio_path, final_path):
            if p and os.path.exists(p):
                os.remove(p)


async def do_tts(message: Message, text: str) -> None:
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


@router.message(F.text)
async def handle_text(message: Message, bot: Bot):
    if not await is_subscribed(bot, message.from_user.id):
        await message.answer(SUBSCRIBE_TEXT, reply_markup=subscribe_keyboard())
        return

    text = message.text.strip()
    if not text:
        return

    mode = user_mode.get(message.from_user.id, "tts")
    is_video_link = VIDEO_URL_REGEX.search(text) is not None

    if mode == "dub":
        if not is_video_link:
            await message.answer(
                "⚠️ Siz hozir \U0001F3AC \"Video tarjima\" rejimidasiz — menga YouTube "
                "yoki TikTok link yuboring.\n\n"
                "Boshqa rejimga o'tish uchun /menu buyrug'ini yuboring."
            )
            return
        await do_dubbing(message, text)
        return

    # mode == "tts"
    if is_video_link:
        await message.answer(
            "⚠️ Siz hozir \U0001F50A \"Textni audio qilish\" rejimidasiz.\n\n"
            "Bu linkni video tarjima qilishimni xohlasangiz, /menu orqali "
            "\U0001F3AC \"Video tarjima\" rejimiga o'ting."
        )
        return
    await do_tts(message, text)


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Botni ishga tushirish / yordam"),
        BotCommand(command="menu", description="Rejimni tanlash (Text/Video)"),
        BotCommand(command="clone", description="O'z ovozingizni klonlash"),
        BotCommand(command="default", description="Standart ovozga qaytish"),
    ])

    log.info("AkoAI bot ishga tushmoqda...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
