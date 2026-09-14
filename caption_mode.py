"""
"/caption" bo'limi — AkoAI botga qo'shimcha modul.

Nima qiladi:
  1) /caption bosilsa (yoki menyudan tugma orqali) rejim yoqiladi
  2) Foydalanuvchi video fayl YOKI Instagram post/reel ssilkasini yuboradi
  3) ffmpeg bilan audio ajratiladi
  4) maxsus o'zbek tiliga moslashtirilgan (fine-tuned) ASR modeli
     (OvozifyLabs/whisper-small-uz-v1, transformers kutubxonasi orqali)
     bilan transkript qilinadi — umumiy Whisper modellari (hatto
     "medium" o'lchamda ham) o'zbek tilida juda kam ma'lumot bilan
     o'qitilgani uchun sifat past bo'ladi, shu sababli maxsus model
     ishlatiladi
  5) So'z darajasidagi vaqt belgilaridan SRT yasaladi (har qator ~4-5
     so'zdan oshmaydi)
  6) ffmpeg (libass) bilan subtitr videoga "kuydiriladi" (hardsub)
  7) Tayyor video foydalanuvchiga qaytariladi

Botga ulash:
  1) Bu faylni loyihaga qo'shing, masalan: handlers/caption_mode.py
  2) main.py (yoki dispatcher sozlanadigan joyda):
         from handlers.caption_mode import caption_router
         dp.include_router(caption_router)
  3) MUHIM: bu modul o'z ichida ALOHIDA ASR modelini yuklaydi
     (bot.py'dagi faster-whisper'dan mustaqil) — chunki ular boshqa-
     boshqa format/kutubxona (CTranslate2 vs transformers). Shuning
     uchun ikkita alohida model xotirada turadi; agar server resursi
     yetishmasa, CAPTION_ASR_MODEL orqali kichikroq model tanlashingiz
     yoki bu funksiyani alohida servisga chiqarishingiz mumkin.
  4) /menu inline klaviaturangizga quyidagi tugmani qo'shing:
         InlineKeyboardButton(text="📝 Titr qo'shish", callback_data="mode_caption")
  5) Kerakli paketlar — requirements.txt'ga qo'shing:
         transformers, torch (CPU versiyasi yetarli), yt-dlp, ffmpeg
         (RAILPACK_DEPLOY_APT_PACKAGES orqali — allaqachon bor)
     ESLATMA: torch o'rnatilishi build vaqtini va konteyner hajmini
     sezilarli oshiradi (yuzlab MB).
  6) Instagramning yopiq/limitli postlari uchun, YOUTUBE_COOKIES bilan
     qilingan patternga o'xshab, ixtiyoriy INSTAGRAM_COOKIES env
     o'zgaruvchisini qo'shishingiz mumkin (pastda ishlatilgan).
"""

import asyncio
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, FSInputFile

logger = logging.getLogger(__name__)

caption_router = Router(name="caption_mode")

# ---------------------------------------------------------------- sozlamalar

MAX_VIDEO_SECONDS = int(os.getenv("CAPTION_MAX_SECONDS", "180"))  # 3 daqiqa
MAX_FILE_MB = 200

INSTAGRAM_URL_RE = re.compile(r"(https?://)?(www\.)?instagram\.com/\S+", re.IGNORECASE)

_asr_pipeline = None
_asr_lock = threading.Lock()

# Umumiy (generic) Whisper modellari o'zbek tilida juda kam ma'lumot bilan
# o'qitilgan — hattoki "medium" o'lcham ham deyarli foyda bermaydi. Shuning
# uchun maxsus o'zbek tiliga moslashtirilgan (fine-tuned) model ishlatiladi:
# haqiqiy Telegram ovozli xabarlari (norasmiy nutq) ustida o'qitilgan,
# ijtimoiy tarmoq videolariga juda mos keladi.
ASR_MODEL_NAME = os.getenv("CAPTION_ASR_MODEL", "OvozifyLabs/whisper-small-uz-v1")


def get_asr_pipeline():
    """Maxsus o'zbek tiliga moslashtirilgan ASR pipeline'ni bitta marta
    yuklaydi (lazy singleton). ESLATMA: bu bot.py'dagi umumiy Whisper
    modelidan MUSTAQIL — chunki bu boshqa (transformers) formatida va
    maxsus o'zbek tili uchun o'qitilgan, shuning uchun ular bilan
    almashtirib bo'lmaydi."""
    global _asr_pipeline
    if _asr_pipeline is None:
        with _asr_lock:
            if _asr_pipeline is None:
                from transformers import pipeline as hf_pipeline

                logger.info(f"O'zbekcha ASR modeli ({ASR_MODEL_NAME}) yuklanmoqda...")
                _asr_pipeline = hf_pipeline(
                    "automatic-speech-recognition",
                    model=ASR_MODEL_NAME,
                    chunk_length_s=30,
                    stride_length_s=5,
                    device=-1,  # CPU
                )
                logger.info("O'zbekcha ASR modeli tayyor.")
    return _asr_pipeline


class CaptionStates(StatesGroup):
    waiting_input = State()


# ------------------------------------------------------------- /caption kirish

@caption_router.message(Command("caption"))
async def cmd_caption(message: Message, state: FSMContext):
    await state.set_state(CaptionStates.waiting_input)
    await message.answer(
        "🎬 <b>Titr qo'shish rejimi yoqildi.</b>\n\n"
        "Menga video fayl yuboring YOKI Instagram post/reel ssilkasini tashlang.\n"
        "Agar video o'zbek tilida bo'lsa — unga avtomatik titr yozib qaytaraman.\n\n"
        "Bekor qilish uchun /cancel bosing.",
        parse_mode="HTML",
    )


@caption_router.callback_query(F.data == "mode_caption")
async def cb_caption_mode(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await cmd_caption(callback.message, state)


@caption_router.message(Command("cancel"), CaptionStates.waiting_input)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.")


# ---------------------------------------------------------------- video kelsa

@caption_router.message(CaptionStates.waiting_input, F.video | F.document)
async def handle_video_input(message: Message, bot: Bot, state: FSMContext):
    media = message.video or message.document
    if media.file_size and media.file_size > MAX_FILE_MB * 1024 * 1024:
        await message.answer(f"Video juda katta ({MAX_FILE_MB}MB dan oshmasin).")
        return

    status = await message.answer("⏳ Video yuklab olinmoqda...")

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "input.mp4"
        tg_file = await bot.get_file(media.file_id)
        await bot.download_file(tg_file.file_path, destination=src_path)
        await process_and_reply(message, status, src_path, tmp)

    await state.clear()


# ------------------------------------------------------- Instagram link kelsa

@caption_router.message(CaptionStates.waiting_input, F.text)
async def handle_link_input(message: Message, state: FSMContext):
    url = message.text.strip()
    if not INSTAGRAM_URL_RE.search(url):
        await message.answer(
            "Bu Instagram ssilkasiga o'xshamayapti.\n"
            "Video fayl yuboring yoki to'g'ri Instagram post/reel ssilkasini tashlang."
        )
        return

    status = await message.answer("⏳ Instagramdan video yuklab olinmoqda...")

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        src_path = tmp / "input.mp4"
        ok = await download_instagram_video(url, src_path)
        if not ok:
            await status.edit_text(
                "❌ Videoni yuklab bo'lmadi. Post ochiq (public) ekanligiga ishonch hosil qiling."
            )
            await state.clear()
            return
        await process_and_reply(message, status, src_path, tmp)

    await state.clear()


# ----------------------------------------------------------------- yordamchi

async def download_instagram_video(url: str, dest: Path) -> bool:
    """yt-dlp orqali Instagram post/reel'ni yuklab oladi."""
    cmd = [
        "yt-dlp",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(dest),
        url,
    ]

    cookies_content = os.getenv("INSTAGRAM_COOKIES")
    if cookies_content:
        cookies_path = dest.parent / "ig_cookies.txt"
        cookies_path.write_text(cookies_content)
        cmd += ["--cookies", str(cookies_path)]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("yt-dlp instagram xatosi: %s", stderr.decode(errors="ignore"))
        return False
    return dest.exists()


async def process_and_reply(message: Message, status: Message, src_path: Path, tmp: Path):
    """Audio ajratish -> til aniqlash/transkript -> SRT -> kuydirish -> yuborish."""

    duration = await get_duration(src_path)
    if duration and duration > MAX_VIDEO_SECONDS:
        await status.edit_text(
            f"❌ Video juda uzun ({int(duration)}s). {MAX_VIDEO_SECONDS}s dan qisqa video yuboring."
        )
        return

    audio_path = tmp / "audio.wav"
    try:
        await run_ffmpeg([
            "ffmpeg", "-y", "-i", str(src_path),
            "-ac", "1", "-ar", "16000", str(audio_path),
        ])
    except RuntimeError as e:
        logger.error("ffmpeg audio ajratish xatosi: %s", e)
        await status.edit_text("❌ Videoni o'qib bo'lmadi. Fayl formatini tekshiring.")
        return

    await status.edit_text("🧠 Nutq tanilmoqda...")
    pipe = get_asr_pipeline()
    result = await asyncio.to_thread(
        pipe,
        str(audio_path),
        return_timestamps="word",
        generate_kwargs={"language": "uzbek", "task": "transcribe"},
    )
    words = result.get("chunks") or []

    if not words:
        await status.edit_text("❌ Videoda nutq topilmadi.")
        return

    srt_path = tmp / "subs.srt"
    write_srt(words, srt_path)

    await status.edit_text("🎞 Titr videoga yozilmoqda...")
    out_path = tmp / "output.mp4"
    try:
        await burn_subtitles(src_path, srt_path, out_path)
    except RuntimeError as e:
        logger.error("ffmpeg subtitr kuydirish xatosi: %s", e)
        await status.edit_text("❌ Titr yozishda xatolik yuz berdi.")
        return

    await status.edit_text("📤 Yuborilmoqda...")
    await message.answer_video(FSInputFile(out_path), caption="✅ Tayyor!")
    await status.delete()


async def get_duration(path: Path) -> Optional[float]:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return None


async def run_ffmpeg(cmd: list):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore"))


def format_timestamp(seconds: float) -> str:
    ms_total = int(round(seconds * 1000))
    h, ms_total = divmod(ms_total, 3600_000)
    m, ms_total = divmod(ms_total, 60_000)
    s, ms = divmod(ms_total, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def group_words_into_lines(words, max_words: int = 5, max_chars: int = 42, max_duration: float = 4.0):
    """transformers ASR pipeline'ning so'z darajasidagi natijasidan
    (return_timestamps="word" bilan olingan, har biri {"text": ..,
    "timestamp": (start, end)} ko'rinishidagi lug'at) qisqa subtitr
    qatorlarini yasaydi."""
    lines = []
    current_words = []
    current_start = None
    last_end = 0.0

    for word in words:
        text = word.get("text", "")
        start, end = word.get("timestamp", (None, None))
        if start is None:
            start = last_end
        if end is None:
            end = start
        last_end = end

        if current_start is None:
            current_start = start
        current_words.append(text)

        text_so_far = "".join(current_words).strip()
        duration = end - current_start

        if (
            len(current_words) >= max_words
            or len(text_so_far) >= max_chars
            or duration >= max_duration
        ):
            lines.append((current_start, end, text_so_far))
            current_words = []
            current_start = None

    if current_words:
        text_so_far = "".join(current_words).strip()
        lines.append((current_start, last_end, text_so_far))

    return lines


def write_srt(words, path: Path):
    lines = []
    entries = group_words_into_lines(words)
    for i, (start, end, text) in enumerate(entries, start=1):
        if not text:
            continue
        lines.append(str(i))
        lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


async def burn_subtitles(src: Path, srt: Path, out: Path):
    """libass 'subtitles' filtri bilan SRT'ni videoga hardsub qiladi."""
    srt_escaped = str(srt).replace("\\", "/").replace(":", "\\:")
    style = (
        "FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=30"
    )
    vf = f"subtitles='{srt_escaped}':force_style='{style}'"
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-c:a", "copy", str(out)]
    await run_ffmpeg(cmd)
