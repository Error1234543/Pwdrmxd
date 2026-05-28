import os
import asyncio
import aiohttp
import aiofiles
import logging
from pathlib import Path
from urllib.parse import quote
import time

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ── LOGGING ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── CONFIG ──
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL", "")   # https://your-app.koyeb.app
PORT         = int(os.environ.get("PORT", "8000"))

PROXY_BASE   = "https://anonymouspwplayer-ce3f42358cca.herokuapp.com/pw"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── SESSION STORE ──
sessions: dict = {}


# ═══════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════

def parse_links(text: str) -> list[dict]:
    items = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        http_idx = line.find("https://")
        if http_idx == -1:
            continue
        name  = line[:http_idx].rstrip(": ").strip() or f"Lecture {len(items)+1}"
        url   = line[http_idx:].strip()
        if url.endswith(".pdf") or ("static.pw.live" in url and ".pdf" in url):
            ftype = "pdf"
        elif ".mpd" in url or ".m3u8" in url:
            ftype = "video"
        else:
            ftype = "other"
        items.append({"name": name, "url": url, "type": ftype})
    return items


def build_proxy_url(url: str, token: str, quality: str) -> str:
    amp_idx = url.find("&")
    if amp_idx != -1:
        video_url    = url[:amp_idx]
        extra_params = "&" + url[amp_idx+1:]
    else:
        video_url    = url
        extra_params = ""
    encoded = quote(video_url, safe="")
    return f"{PROXY_BASE}?url={encoded}{extra_params}&quality={quality}&token={token}"


def progress_bar(current: int, total: int) -> str:
    pct    = current / total
    filled = int(pct * 20)
    bar    = "█" * filled + "░" * (20 - filled)
    return f"[{bar}] {pct*100:.1f}%\n{current/1048576:.1f} MB / {total/1048576:.1f} MB"


async def download_file(url: str, dest: Path) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=600)) as r:
                if r.status != 200:
                    log.error(f"Download HTTP {r.status}: {url}")
                    return False
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(524288):
                        await f.write(chunk)
        return True
    except Exception as e:
        log.error(f"download_file error: {e}")
        return False


async def download_video_ffmpeg(proxy_url: str, dest: Path) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", proxy_url,
            "-c", "copy", "-bsf:a", "aac_adtstoasc", str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode != 0:
            log.error(f"ffmpeg: {stderr.decode()[-500:]}")
            return False
        return True
    except asyncio.TimeoutError:
        log.error("ffmpeg timeout")
        return False
    except Exception as e:
        log.error(f"ffmpeg exception: {e}")
        return False


# ═══════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions[uid] = {"step": "wait_file"}
    await update.message.reply_text(
        "👋 Welcome to PW Downloader Bot!\n\n"
        "📁 Send me a .txt file with video/PDF links.\n\n"
        "📌 Format:\n"
        "Lecture Name : https://...mpd&parentId=xxx\n"
        "PDF Notes    : https://static.pw.live/.../file.pdf\n\n"
        "Send the file now ↓"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in sessions:
        sessions.pop(uid)
        await update.message.reply_text("🚫 Cancelled. Send /start to begin again.")
    else:
        await update.message.reply_text("No active session.")


async def recv_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = sessions.get(uid, {})

    if state.get("step") != "wait_file":
        return

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("⚠️ Please send a .txt file only.")
        return

    msg       = await update.message.reply_text("⏳ Reading file...")
    file_path = DOWNLOAD_DIR / f"{uid}_input.txt"

    tg_file = await ctx.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(file_path))

    async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = await f.read()

    items = parse_links(content)
    if not items:
        await msg.edit_text("❌ No valid links found. Check the format and try again.")
        sessions[uid] = {"step": "wait_file"}
        return

    sessions[uid] = {"step": "wait_token", "items": items, "total": len(items)}
    videos = sum(1 for i in items if i["type"] == "video")
    pdfs   = sum(1 for i in items if i["type"] == "pdf")

    await msg.edit_text(
        f"✅ File parsed!\n\n"
        f"🎬 Videos: {videos}\n"
        f"📄 PDFs:   {pdfs}\n"
        f"📦 Total:  {len(items)}\n\n"
        f"🔑 Now send your PW Token (JWT — starts with eyJ...):"
    )


async def recv_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = sessions.get(uid, {})
    step  = state.get("step")

    if step == "wait_token":
        token = update.message.text.strip()
        if len(token) < 50 or not token.startswith("eyJ"):
            await update.message.reply_text("❌ Invalid token. Send a valid JWT (starts with eyJ...).")
            return
        sessions[uid]["token"] = token
        sessions[uid]["step"]  = "wait_quality"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("360p",  callback_data=f"q_{uid}_360"),
             InlineKeyboardButton("480p",  callback_data=f"q_{uid}_480")],
            [InlineKeyboardButton("720p",  callback_data=f"q_{uid}_720"),
             InlineKeyboardButton("1080p", callback_data=f"q_{uid}_1080")],
        ])
        await update.message.reply_text("🎬 Select video quality:", reply_markup=kb)

    elif step == "wait_caption":
        prefix = update.message.text.strip()
        sessions[uid]["caption_prefix"] = prefix
        sessions[uid]["step"]           = "downloading"
        await update.message.reply_text(
            f"✅ Caption prefix: {prefix}\n\n"
            f"🚀 Starting download & upload...\n"
            f"📦 Total: {state['total']}"
        )
        asyncio.create_task(process_downloads(ctx.bot, update.message.chat_id, uid))


async def cb_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    parts   = query.data.split("_")
    uid     = int(parts[1])
    quality = parts[2]

    if query.from_user.id != uid:
        await query.answer("Not your session!", show_alert=True)
        return

    state = sessions.get(uid)
    if not state:
        await query.answer("Session expired. Send /start again.", show_alert=True)
        return

    sessions[uid]["quality"] = quality
    sessions[uid]["step"]    = "wait_caption"

    await query.message.edit_text(
        f"✅ Quality: {quality}p\n\n"
        f"📝 Now send the caption prefix for videos.\n"
        f"Example: Yakeen NEET 2026 | Gujarati Batch"
    )
    await query.answer()


# ═══════════════════════════════════════════
#  DOWNLOAD + UPLOAD LOOP
# ═══════════════════════════════════════════

async def process_downloads(bot, chat_id: int, uid: int):
    state   = sessions[uid]
    items   = state["items"]
    token   = state["token"]
    quality = state["quality"]
    prefix  = state["caption_prefix"]
    total   = len(items)

    status   = await bot.send_message(chat_id, f"⏳ Processing 0/{total}...")
    ok_cnt   = 0
    fail_cnt = 0

    for idx, item in enumerate(items, 1):
        name  = item["name"]
        url   = item["url"]
        ftype = item["type"]

        try:
            await status.edit_text(f"⏬ Downloading {idx}/{total}\n📌 {name}\n📂 {ftype.upper()}")
        except Exception:
            pass

        caption = f"*{prefix}*\n\n📌 `{name}`\n🔢 `{idx}/{total}`"

        # ── PDF ──
        if ftype == "pdf":
            dest = DOWNLOAD_DIR / f"{uid}_{idx}.pdf"
            ok   = await download_file(url, dest)
            if ok and dest.exists() and dest.stat().st_size > 0:
                try:
                    with open(dest, "rb") as fh:
                        await bot.send_document(chat_id, document=fh, caption=caption, parse_mode="Markdown")
                    ok_cnt += 1
                except Exception as e:
                    log.error(f"PDF upload: {e}")
                    await bot.send_message(chat_id, f"❌ Upload failed: {name}\n{e}")
                    fail_cnt += 1
                finally:
                    dest.unlink(missing_ok=True)
            else:
                fail_cnt += 1
                await bot.send_message(chat_id, f"❌ Download failed: {name}")

        # ── VIDEO ──
        elif ftype == "video":
            proxy = build_proxy_url(url, token, quality)
            dest  = DOWNLOAD_DIR / f"{uid}_{idx}.mp4"
            ok    = await download_video_ffmpeg(proxy, dest)

            if ok and dest.exists() and dest.stat().st_size > 0:
                prog_msg = await bot.send_message(chat_id, f"📤 Uploading: {name}...")
                try:
                    with open(dest, "rb") as fh:
                        await bot.send_video(
                            chat_id, video=fh,
                            caption=caption, parse_mode="Markdown",
                            supports_streaming=True,
                            write_timeout=600, read_timeout=600,
                        )
                    await prog_msg.delete()
                    ok_cnt += 1
                except Exception as e:
                    log.error(f"Video upload: {e}")
                    await bot.send_message(chat_id, f"❌ Upload failed: {name}\n{str(e)[:200]}")
                    fail_cnt += 1
                finally:
                    dest.unlink(missing_ok=True)
            else:
                fail_cnt += 1
                await bot.send_message(chat_id, f"❌ ffmpeg failed: {name}")

        await asyncio.sleep(2)

    try:
        await status.edit_text(
            f"✅ All Done!\n\n"
            f"✔️ Success: {ok_cnt}\n"
            f"❌ Failed:  {fail_cnt}\n"
            f"📦 Total:   {total}\n\n"
            f"Send /start to process another file."
        )
    except Exception:
        pass

    sessions.pop(uid, None)


# ═══════════════════════════════════════════
#  MAIN — WEBHOOK
# ═══════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var not set!")
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL env var not set!")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start",  cmd_start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(MessageHandler(filters.Document.ALL, recv_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recv_text))
    application.add_handler(CallbackQueryHandler(cb_quality, pattern=r"^q_\d+_\d+$"))

    log.info(f"Starting webhook → {WEBHOOK_URL}  port={PORT}")

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
