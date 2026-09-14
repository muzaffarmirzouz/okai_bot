"""
"/caption" bo'limi — AkoAI botga qo'shimcha modul.

Nima qiladi:
  1) /caption bosilsa (yoki menyudan tugma orqali) rejim yoqiladi
  2) Foydalanuvchi video fayl YOKI Instagram post/reel ssilkasini yuboradi
  3) ffmpeg bilan audio ajratiladi
  4) faster-whisper bilan til aniqlanadi + transkript qilinadi
     - agar til "uz" bo'lmasa -> rad javobi qaytariladi
  5) Segmentlardan SRT yasaladi
  6) ffmpeg (libass) bilan subtitr videoga "kuydiriladi" (hardsub)
  7) Tayyor video foydalanuvchiga qaytariladi

Botga ulash:
  1) Bu faylni loyihaga qo'shing, masalan: handlers/caption_mode.py
  2) main.py (yoki dispatcher sozlanadigan joyda):
         from handlers.caption_mode import caption_router
         dp.include_router(caption_router)
  3) Agar faster-whisper modelingiz allaqachon boshqa joyda (masalan
     video tarjima funksiyasida) global qilib yuklangan bo'lsa, shu
     yerdagi get_whisper_model() o'rniga o'sha global obyektni
     import qiling — bitta jarayonda modelni ikki marta yuklamang.
  4) /menu inline klaviaturangizga quyidagi tugmani qo'shing:
         InlineKeyboardButton(text="📝 Titr qo'shish", callback_data="mode_caption")
  5) Kerakli paketlar (loyihada allaqachon bo'lishi kerak, chunki
     video tarjima funksiyasi ham ulardan foydalanadi):
         faster-whisper, yt-dlp, ffmpeg (RAILPACK_DEPLOY_APT_PACKAGES orqali)
  6) Instagramning yopiq/limitli postlari uchun, YOUTUBE_COOKIES bilan
     qilingan patternga o'xshab, ixtiyoriy INSTAGRAM_COOKIES env
     o'zgaruvchisini qo'shishingiz mumkin (pastda ishlatilgan).
"""

import asyncio
import logging
import os
import re
import tempfile
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

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
MAX_VIDEO_SECONDS = int(os.getenv("CAPTION_MAX_SECONDS", "180"))  # 3 daqiqa
MAX_FILE_MB = 200

INSTAGRAM_URL_RE = re.compile(r"(https?://)?(www\.)?instagram\.com/\S+", re.IGNORECASE)

def get_whisper_model():
    """bot.py'da video-tarjima funksiyasi uchun allaqachon yuklangan Whisper
    modelini qayta ishlatadi (o'sha lazy-singleton _get_whisper_model()) —
    shunda xotirada ikkita alohida Whisper modeli birga turib qolmaydi.
    Import funksiya ICHIDA qilinadi (module darajasida emas), chunki
    bot.py caption_router'ni import qilganda, bot.py hali to'liq
    yuklanib ulgurmagan bo'ladi (circular import) — bu chaqiruv esa faqat
    foydalanuvchi haqiqatan video yuborganda, ya'ni bot allaqachon to'liq
    ishga tushgandan keyin amalga oshadi."""
    from bot import _get_whisper_model
    return _get_whisper_model()


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
    cmd = ["yt-dlp", "-f", "mp4", "-o", str(dest), url]

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
    model = get_whisper_model()
    segments_gen, info = model.transcribe(str(audio_path), language=None, task="transcribe")
    segments = list(segments_gen)

    detected_lang = info.language
    if detected_lang != "uz":
        await status.edit_text(
            f"❌ Video o'zbek tilida emasga o'xshaydi (aniqlangan til: {detected_lang}).\n"
            "Faqat o'zbek tilidagi videolar qo'llab-quvvatlanadi."
        )
        return

    if not segments:
        await status.edit_text("❌ Videoda nutq topilmadi.")
        return

    srt_path = tmp / "subs.srt"
    write_srt(segments, srt_path)

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


def write_srt(segments, path: Path):
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}")
        lines.append(seg.text.strip())
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
