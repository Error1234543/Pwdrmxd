import os
import asyncio
import aiohttp
import aiofiles
import logging
from pathlib import Path
from urllib.parse import quote
import time

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL", "")
PORT         = int(os.environ.get("PORT", "8000"))
PROXY_BASE   = "https://anonymouspwplayer-ce3f42358cca.herokuapp.com/pw"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_USERS = {8226637107, 8356297447}
sessions: dict = {}


async def check_access(update: Update) -> bool:
    uid = update.effective_user.id
    if uid in ALLOWED_USERS:
        return True
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 Access Denied!\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ Yeh bot sirf authorized users ke liye hai.\n\n"
        "💰 Bot Price: Rs.400 Only\n\n"
        "📩 Purchase ke liye contact karo:\n"
        "👉 @Batman_x_duo_bot\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Powered By Downloader Zone ⚡"
    )
    return False


def parse_links(text: str) -> list[dict]:
    items = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        idx = line.find("https://")
        if idx == -1:
            continue
        name  = line[:idx].rstrip(": ").strip() or f"File {len(items)+1}"
        url   = line[idx:].strip()
        if ".pdf" in url:
            ftype = "pdf"
        elif ".mpd" in url or ".m3u8" in url:
            ftype = "video"
        else:
            ftype = "other"
        items.append({"name": name, "url": url, "type": ftype})
    return items


def safe_filename(name: str) -> str:
    return "".join(c for c in name if c not in r'\/:*?"<>|').strip()[:80]


def build_proxy_url(url: str, token: str, quality: str) -> str:
    amp = url.find("&")
    base  = url[:amp] if amp != -1 else url
    extra = "&" + url[amp+1:] if amp != -1 else ""
    return f"{PROXY_BASE}?url={quote(base, safe='')}{extra}&quality={quality}&token={token}"


def fmt_size(b: float) -> str:
    if b <= 0: return "0B"
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024: return f"{b:.2f}{unit}"
        b /= 1024
    return f"{b:.2f}TB"


def fmt_time(s: float) -> str:
    s = max(0, int(s))
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m{s%60}s"
    return f"{s//3600}h{(s%3600)//60}m"


def progress_box(name: str, done: int, total: int, speed: float, elapsed: float, status: str) -> str:
    pct    = min(done / total, 1.0) if total > 0 else 0
    filled = int(pct * 12)
    bar    = "●" * filled + "○" * (12 - filled)
    eta    = (total - done) / speed if speed > 0 and total > done else 0
    return (
        f"{name[:55]}\n"
        f"╭ Task By 𝐃𝐨𝐰𝐧𝐥𝐨𝐚𝐝𝐞𝐫 𝐙𝐨𝐧𝐞\n"
        f"┊ [{bar}] {pct*100:.1f}%\n"
        f"┊ Status  : {status}\n"
        f"┊ Done    : {fmt_size(done)}\n"
        f"┊ Total   : {fmt_size(total) if total > 0 else 'N/A'}\n"
        f"┊ Speed   : {fmt_size(speed)}/s\n"
        f"┊ ETA     : {fmt_time(eta)}\n"
        f"╰ Past    : {fmt_time(elapsed)}\n"
        f"⋗ Powered By Downloader Zone ⚡"
    )


async def safe_edit(msg, text: str):
    """Edit message - RetryAfter pe wait karo, silently."""
    try:
        await msg.edit_text(text)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await msg.edit_text(text)
        except Exception:
            pass
    except Exception:
        pass


async def safe_send_document(bot, chat_id, dest, fname, caption):
    """Send document with auto retry on flood."""
    for attempt in range(6):
        try:
            with open(dest, "rb") as fh:
                msg = await bot.send_document(
                    chat_id,
                    document=fh,
                    filename=f"{fname}.pdf",
                    caption=caption,
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )
            return True
        except RetryAfter as e:
            wait = e.retry_after + 2
            log.warning(f"FloodWait {wait}s (attempt {attempt+1})")
            await asyncio.sleep(wait)
        except Exception as e:
            log.error(f"send_document attempt {attempt+1}: {e}")
            await asyncio.sleep(3)
    return False


async def safe_send_video(bot, chat_id, dest, fname, caption):
    """Send video with auto retry on flood."""
    for attempt in range(6):
        try:
            with open(dest, "rb") as fh:
                await bot.send_video(
                    chat_id,
                    video=fh,
                    filename=f"{fname}.mp4",
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=3600,
                    write_timeout=3600,
                    connect_timeout=60,
                    pool_timeout=3600,
                )
            return True
        except RetryAfter as e:
            wait = e.retry_after + 2
            log.warning(f"FloodWait video {wait}s (attempt {attempt+1})")
            await asyncio.sleep(wait)
        except Exception as e:
            log.error(f"send_video attempt {attempt+1}: {e}")
            # Try as document fallback
            try:
                with open(dest, "rb") as fh:
                    await bot.send_document(
                        chat_id,
                        document=fh,
                        filename=f"{fname}.mp4",
                        caption=caption,
                        read_timeout=3600,
                        write_timeout=3600,
                        connect_timeout=60,
                        pool_timeout=3600,
                    )
                return True
            except Exception:
                await asyncio.sleep(3)
    return False


async def download_pdf(url: str, dest: Path, prog_msg, name: str) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=600)) as r:
                if r.status != 200:
                    log.error(f"PDF HTTP {r.status}")
                    return False
                total     = int(r.headers.get("Content-Length", 0))
                done      = 0
                start     = time.time()
                last_edit = 0
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(524288):
                        await f.write(chunk)
                        done += len(chunk)
                        now     = time.time()
                        elapsed = now - start
                        speed   = done / elapsed if elapsed > 0 else 0
                        if now - last_edit > 5 and total > 0:
                            last_edit = now
                            await safe_edit(
                                prog_msg,
                                progress_box(name, done, total, speed, elapsed, "📥 Downloading PDF")
                            )
        return True
    except Exception as e:
        log.error(f"download_pdf: {e}")
        return False


async def download_video(proxy_url: str, dest: Path, prog_msg, name: str) -> bool:
    try:
        start = time.time()
        total_size = 0
        try:
            probe = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=size",
                "-of", "default=noprint_wrappers=1:nokey=1",
                proxy_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=30)
            total_size = int(stdout.decode().strip())
        except Exception:
            total_size = 0

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", proxy_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        async def watcher():
            last_edit = 0
            while proc.returncode is None:
                await asyncio.sleep(5)
                if dest.exists():
                    done    = dest.stat().st_size
                    elapsed = time.time() - start
                    speed   = done / elapsed if elapsed > 0 else 0
                    display = total_size if total_size > 0 else done
                    now = time.time()
                    if now - last_edit > 8:
                        last_edit = now
                        await safe_edit(
                            prog_msg,
                            progress_box(name, done, display, speed, elapsed, "📥 Downloading Video")
                        )

        wt = asyncio.create_task(watcher())
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        wt.cancel()

        if proc.returncode != 0:
            log.error(f"ffmpeg: {stderr.decode()[-300:]}")
            return False
        return True
    except asyncio.TimeoutError:
        log.error("ffmpeg timeout")
        return False
    except Exception as e:
        log.error(f"download_video: {e}")
        return False


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    uid = update.effective_user.id
    sessions[uid] = {"step": "wait_file"}
    await update.message.reply_text(
        "⚡ PW Downloader Bot\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📁 .txt file bhejo jisme video/PDF links hain\n\n"
        "📌 Format:\n"
        "Lecture Name : https://....mpd&parentId=xxx\n"
        "PDF Notes    : https://static.pw.live/.../file.pdf\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Powered By Downloader Zone ⚡"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("🚫 Cancelled. /start se dobara shuru karo.")


async def recv_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    uid   = update.effective_user.id
    state = sessions.get(uid, {})

    if state.get("step") != "wait_file":
        await update.message.reply_text("⚠️ Pehle /start karo.")
        return

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("⚠️ Sirf .txt file bhejo.")
        return

    msg       = await update.message.reply_text("⏳ File padh raha hun...")
    file_path = DOWNLOAD_DIR / f"{uid}_input.txt"
    tg_file   = await ctx.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(file_path))

    async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = await f.read()

    items = parse_links(content)
    if not items:
        await msg.edit_text("❌ Koi valid link nahi mila. Format check karo.")
        sessions[uid] = {"step": "wait_file"}
        return

    sessions[uid] = {"step": "wait_start_num", "items": items}
    videos = sum(1 for i in items if i["type"] == "video")
    pdfs   = sum(1 for i in items if i["type"] == "pdf")
    total  = len(items)

    await msg.edit_text(
        f"✅ File parse ho gayi!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎬 Videos : {videos}\n"
        f"📄 PDFs   : {pdfs}\n"
        f"📦 Total  : {total}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 Kis number se download start karna hai?\n\n"
        f"• 25 bhejo → 25 se {total} tak\n"
        f"• 1 bhejo → sab ({total} links)"
    )


async def recv_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    uid   = update.effective_user.id
    state = sessions.get(uid, {})
    step  = state.get("step")
    text  = update.message.text.strip()

    if step == "wait_start_num":
        items = state["items"]
        total = len(items)
        try:
            start_num = int(text)
            if not (1 <= start_num <= total):
                raise ValueError
        except ValueError:
            await update.message.reply_text(f"❌ 1 se {total} ke beech number bhejo.\nExample: 25")
            return
        selected = items[start_num - 1:]
        sessions[uid]["selected"]   = selected
        sessions[uid]["start_from"] = start_num
        sessions[uid]["step"]       = "wait_token"
        videos = sum(1 for i in selected if i["type"] == "video")
        pdfs   = sum(1 for i in selected if i["type"] == "pdf")
        await update.message.reply_text(
            f"✅ Selection confirmed!\n\n"
            f"📌 Range  : {start_num} → {total}\n"
            f"🎬 Videos : {videos}\n"
            f"📄 PDFs   : {pdfs}\n"
            f"📦 Count  : {len(selected)}\n\n"
            f"🔑 PW Token bhejo (eyJ... se shuru):"
        )
        return

    if step == "wait_token":
        if len(text) < 50 or not text.startswith("eyJ"):
            await update.message.reply_text("❌ Invalid token. eyJ... se shuru hona chahiye.")
            return
        sessions[uid]["token"] = text
        sessions[uid]["step"]  = "wait_quality"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("360p",  callback_data=f"q_{uid}_360"),
             InlineKeyboardButton("480p",  callback_data=f"q_{uid}_480")],
            [InlineKeyboardButton("720p",  callback_data=f"q_{uid}_720"),
             InlineKeyboardButton("1080p", callback_data=f"q_{uid}_1080")],
        ])
        await update.message.reply_text("🎬 Video quality select karo:", reply_markup=kb)
        return

    if step == "wait_caption":
        sessions[uid]["caption_prefix"] = text
        sessions[uid]["step"]           = "downloading"
        selected = sessions[uid]["selected"]
        await update.message.reply_text(
            f"✅ Batch name: {text}\n\n"
            f"🚀 Download + Upload shuru...\n"
            f"📦 Total: {len(selected)} items"
        )
        asyncio.create_task(process_all(ctx.bot, update.message.chat_id, uid))


async def cb_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    parts   = query.data.split("_")
    uid     = int(parts[1])
    quality = parts[2]
    if query.from_user.id != uid:
        await query.answer("Yeh tumhara session nahi!", show_alert=True)
        return
    state = sessions.get(uid)
    if not state:
        await query.answer("Session expire. /start karo.", show_alert=True)
        return
    sessions[uid]["quality"] = quality
    sessions[uid]["step"]    = "wait_caption"
    await query.message.edit_text(
        f"✅ Quality: {quality}p\n\n"
        f"📝 Batch name bhejo:\n"
        f"Example: Yakeen NEET 2026 | Gujarati Batch"
    )
    await query.answer()


async def process_all(bot, chat_id: int, uid: int):
    state      = sessions.get(uid, {})
    selected   = state["selected"]
    token      = state["token"]
    quality    = state["quality"]
    batch_name = state["caption_prefix"]
    total      = len(selected)
    start_from = state.get("start_from", 1)

    status   = await bot.send_message(chat_id, f"⏳ Starting... 0/{total}")
    ok_cnt   = 0
    fail_cnt = 0

    for idx, item in enumerate(selected, 1):
        name     = item["name"]
        url      = item["url"]
        ftype    = item["type"]
        real_num = start_from + idx - 1
        fname    = safe_filename(name)

        caption = (
            f"Index: {real_num:03d}\n\n"
            f"Title: {name}\n\n"
            f"Batch: {batch_name}\n\n"
            f"Extracted by: Batman"
        )

        # Single progress message - no extra messages
        prog_msg = await bot.send_message(
            chat_id,
            progress_box(name, 0, 0, 0, 0, "⏳ Starting...")
        )

        if ftype == "pdf":
            dest = DOWNLOAD_DIR / f"{uid}_{idx}_{fname}.pdf"
            ok   = await download_pdf(url, dest, prog_msg, name)
            if ok and dest.exists() and dest.stat().st_size > 0:
                fsize = dest.stat().st_size
                await safe_edit(prog_msg, progress_box(name, fsize, fsize, 0, 0, "📤 Uploading PDF..."))
                up_ok = await safe_send_document(bot, chat_id, dest, fname, caption)
                if up_ok:
                    await safe_edit(prog_msg, f"✅ Done: {name}")
                    ok_cnt += 1
                else:
                    await safe_edit(prog_msg, f"❌ Upload failed: {name}")
                    fail_cnt += 1
                dest.unlink(missing_ok=True)
            else:
                fail_cnt += 1
                await safe_edit(prog_msg, f"❌ Download failed: {name}")

        elif ftype == "video":
            proxy = build_proxy_url(url, token, quality)
            dest  = DOWNLOAD_DIR / f"{uid}_{idx}_{fname}.mp4"
            ok    = await download_video(proxy, dest, prog_msg, name)
            if ok and dest.exists() and dest.stat().st_size > 0:
                fsize = dest.stat().st_size
                await safe_edit(prog_msg, progress_box(name, 0, fsize, 0, 0, "📤 Uploading Video..."))
                up_ok = await safe_send_video(bot, chat_id, dest, fname, caption)
                if up_ok:
                    await safe_edit(prog_msg, f"✅ Done: {name}")
                    ok_cnt += 1
                else:
                    await safe_edit(prog_msg, f"❌ Upload failed: {name}")
                    fail_cnt += 1
                dest.unlink(missing_ok=True)
            else:
                fail_cnt += 1
                await safe_edit(prog_msg, f"❌ ffmpeg failed: {name}")

        # Status update - only every 5 files to reduce API calls
        if idx % 5 == 0 or idx == total:
            try:
                await status.edit_text(
                    f"📊 Progress: {idx}/{total}\n"
                    f"✔️ Done   : {ok_cnt}\n"
                    f"❌ Failed : {fail_cnt}\n\n"
                    f"⚡ Powered By Downloader Zone"
                )
            except Exception:
                pass

        await asyncio.sleep(1)

    try:
        await status.edit_text(
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Sab khatam!\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✔️ Success : {ok_cnt}\n"
            f"❌ Failed  : {fail_cnt}\n"
            f"📦 Total   : {total}\n\n"
            f"Dobara karna ho to /start karo.\n\n"
            f"⚡ Powered By Downloader Zone"
        )
    except Exception:
        pass

    sessions.pop(uid, None)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN set nahi hai!")
    if not WEBHOOK_URL:
        raise RuntimeError("WEBHOOK_URL set nahi hai!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.Document.ALL, recv_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recv_text))
    app.add_handler(CallbackQueryHandler(cb_quality, pattern=r"^q_\d+_\d+$"))

    log.info(f"Webhook → {WEBHOOK_URL}  port={PORT}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
