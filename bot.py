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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT        = int(os.environ.get("PORT", "8000"))

PROXY_BASE   = "https://anonymouspwplayer-ce3f42358cca.herokuapp.com/pw"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_USERS = {8226637107, 8356297447}
sessions: dict = {}


# ═══════════════════════════════════════════
#  ACCESS CHECK
# ═══════════════════════════════════════════

async def check_access(update: Update) -> bool:
    uid = update.effective_user.id
    if uid in ALLOWED_USERS:
        return True
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 *Access Denied!*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ Yeh bot sirf authorized users ke liye hai\\.\n\n"
        "💰 *Bot Price: ₹400 Only*\n\n"
        "📩 Purchase ke liye contact karo:\n"
        "👉 @Batman\\_x\\_duo\\_bot\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Powered By Downloader Zone* ⚡",
        parse_mode="MarkdownV2"
    )
    return False


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


def parse_selection(text: str, total: int) -> list[int]:
    """Parse '1,3,5' or '2-6' or 'all' → list of 0-based indices"""
    text = text.strip().lower()
    if text == "all":
        return list(range(total))
    selected = set()
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                for i in range(int(a), int(b)+1):
                    if 1 <= i <= total:
                        selected.add(i-1)
            except Exception:
                pass
        else:
            try:
                i = int(part)
                if 1 <= i <= total:
                    selected.add(i-1)
            except Exception:
                pass
    return sorted(selected)


def build_list_msg(items: list[dict]) -> str:
    """Build numbered list of all items"""
    lines = ["📋 *Links found in file:*\n"]
    for i, item in enumerate(items, 1):
        icon = "🎬" if item["type"] == "video" else "📄" if item["type"] == "pdf" else "🔗"
        name = item["name"][:45] + "…" if len(item["name"]) > 45 else item["name"]
        lines.append(f"`{i:02d}.` {icon} {name}")
    lines.append(f"\n📦 Total: `{len(items)}`")
    lines.append("\n✏️ *Kaunsi links download karni hain?*")
    lines.append("Reply karo:\n• `all` — sab\n• `1,3,5` — specific numbers\n• `2-8` — range")
    return "\n".join(lines)


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


def fmt_size(b: int) -> str:
    if b <= 0: return "0B"
    for unit in ["B","KB","MB","GB"]:
        if b < 1024: return f"{b:.2f}{unit}"
        b /= 1024
    return f"{b:.2f}TB"


def fmt_time(secs: float) -> str:
    secs = int(secs)
    if secs < 60:   return f"{secs}s"
    elif secs < 3600: return f"{secs//60}m{secs%60}s"
    else:             return f"{secs//3600}h{(secs%3600)//60}m"


def progress_box(name: str, done: int, total: int, speed: float, elapsed: float, action: str) -> str:
    pct    = done / total if total > 0 else 0
    filled = int(pct * 12)
    bar    = "●" * filled + "○" * (12 - filled)
    eta    = (total - done) / speed if speed > 0 else 0
    return (
        f"*{name[:50]}*\n"
        f"╭ Task By 𝐃𝐨𝐰𝐧𝐥𝐨𝐚𝐝𝐞𝐫 𝐙𝐨𝐧𝐞\n"
        f"┊ [{bar}] {pct*100:.1f}%\n"
        f"┊ Status  : {action}\n"
        f"┊ Done    : {fmt_size(done)}\n"
        f"┊ Total   : {fmt_size(total)}\n"
        f"┊ Speed   : {fmt_size(int(speed))}/s\n"
        f"┊ ETA     : {fmt_time(eta)}\n"
        f"╰ Past    : {fmt_time(elapsed)}\n"
        f"⋗ *Powered By Downloader Zone* ⚡"
    )


async def download_file_progress(url: str, dest: Path, prog_msg, name: str) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=600)) as r:
                if r.status != 200:
                    return False
                total     = int(r.headers.get("Content-Length", 0))
                done      = 0
                start     = time.time()
                last_edit = 0
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(524288):
                        await f.write(chunk)
                        done    += len(chunk)
                        now      = time.time()
                        elapsed  = now - start
                        speed    = done / elapsed if elapsed > 0 else 0
                        if now - last_edit > 4 and total > 0:
                            last_edit = now
                            try:
                                await prog_msg.edit_text(
                                    progress_box(name, done, total, speed, elapsed, "📥 Downloading"),
                                    parse_mode="Markdown"
                                )
                            except Exception:
                                pass
        return True
    except Exception as e:
        log.error(f"download_file_progress: {e}")
        return False


async def download_video_ffmpeg(proxy_url: str, dest: Path, prog_msg, name: str) -> bool:
    try:
        start = time.time()
        proc  = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", proxy_url,
            "-c", "copy", "-bsf:a", "aac_adtstoasc", str(dest),
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
                    now     = time.time()
                    if now - last_edit > 8:
                        last_edit = now
                        try:
                            await prog_msg.edit_text(
                                progress_box(name, done, done, speed, elapsed, "📥 Downloading Video"),
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass

        watch_task = asyncio.create_task(watcher())
        _, stderr  = await asyncio.wait_for(proc.communicate(), timeout=1800)
        watch_task.cancel()

        if proc.returncode != 0:
            log.error(f"ffmpeg: {stderr.decode()[-500:]}")
            return False
        return True
    except asyncio.TimeoutError:
        log.error("ffmpeg timeout")
        return False
    except Exception as e:
        log.error(f"ffmpeg: {e}")
        return False


# ═══════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    uid = update.effective_user.id
    sessions[uid] = {"step": "wait_file"}
    await update.message.reply_text(
        "⚡ *PW Downloader Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📁 Send me a *.txt file* with video/PDF links.\n\n"
        "📌 *Format:*\n"
        "`Lecture Name : https://...mpd&parentId=xxx`\n"
        "`PDF Notes    : https://static.pw.live/.../file.pdf`\n\n"
        "Send the file now ↓\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Powered By Downloader Zone* ⚡",
        parse_mode="Markdown"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    uid = update.effective_user.id
    if uid in sessions:
        sessions.pop(uid)
        await update.message.reply_text("🚫 Cancelled. Send /start to begin again.")
    else:
        await update.message.reply_text("No active session.")


async def recv_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
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
    tg_file   = await ctx.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(file_path))

    async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = await f.read()

    items = parse_links(content)
    if not items:
        await msg.edit_text("❌ No valid links found. Check format and try again.")
        sessions[uid] = {"step": "wait_file"}
        return

    sessions[uid] = {"step": "wait_selection", "items": items}

    # Send list - split if too long
    list_msg = build_list_msg(items)
    if len(list_msg) > 4000:
        # Send as chunks
        chunk = "📋 *Links found in file:*\n\n"
        for i, item in enumerate(items, 1):
            icon = "🎬" if item["type"] == "video" else "📄" if item["type"] == "pdf" else "🔗"
            name = item["name"][:45] + "…" if len(item["name"]) > 45 else item["name"]
            line = f"`{i:02d}.` {icon} {name}\n"
            if len(chunk) + len(line) > 4000:
                await msg.edit_text(chunk, parse_mode="Markdown")
                msg   = await ctx.bot.send_message(update.effective_chat.id, "...")
                chunk = ""
            chunk += line
        chunk += (
            f"\n📦 Total: `{len(items)}`\n\n"
            f"✏️ *Kaunsi links download karni hain?*\n"
            f"• `all` — sab\n• `1,3,5` — specific\n• `2-8` — range"
        )
        await msg.edit_text(chunk, parse_mode="Markdown")
    else:
        await msg.edit_text(list_msg, parse_mode="Markdown")


async def recv_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    uid   = update.effective_user.id
    state = sessions.get(uid, {})
    step  = state.get("step")
    text  = update.message.text.strip()

    # ── STEP 1: Link selection ──
    if step == "wait_selection":
        items    = state["items"]
        indices  = parse_selection(text, len(items))
        if not indices:
            await update.message.reply_text(
                "❌ Invalid selection. Try:\n• `all`\n• `1,3,5`\n• `2-8`",
                parse_mode="Markdown"
            )
            return

        selected = [items[i] for i in indices]
        sessions[uid]["selected"] = selected
        sessions[uid]["step"]     = "wait_token"

        videos = sum(1 for i in selected if i["type"] == "video")
        pdfs   = sum(1 for i in selected if i["type"] == "pdf")

        await update.message.reply_text(
            f"✅ *Selected {len(selected)} links!*\n\n"
            f"🎬 Videos : `{videos}`\n"
            f"📄 PDFs   : `{pdfs}`\n\n"
            f"🔑 Send your *PW Token* (starts with `eyJ...`):",
            parse_mode="Markdown"
        )
        return

    # ── STEP 2: Token ──
    if step == "wait_token":
        if len(text) < 50 or not text.startswith("eyJ"):
            await update.message.reply_text("❌ Invalid token. Must start with `eyJ...`", parse_mode="Markdown")
            return
        sessions[uid]["token"] = text
        sessions[uid]["step"]  = "wait_quality"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("360p",  callback_data=f"q_{uid}_360"),
             InlineKeyboardButton("480p",  callback_data=f"q_{uid}_480")],
            [InlineKeyboardButton("720p",  callback_data=f"q_{uid}_720"),
             InlineKeyboardButton("1080p", callback_data=f"q_{uid}_1080")],
        ])
        await update.message.reply_text("🎬 *Select video quality:*", reply_markup=kb, parse_mode="Markdown")
        return

    # ── STEP 3: Caption prefix ──
    if step == "wait_caption":
        sessions[uid]["caption_prefix"] = text
        sessions[uid]["step"]           = "downloading"
        selected = sessions[uid]["selected"]
        await update.message.reply_text(
            f"✅ Caption: `{text}`\n\n"
            f"🚀 Starting download & upload...\n"
            f"📦 Downloading: `{len(selected)}` items",
            parse_mode="Markdown"
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
        f"✅ Quality: *{quality}p*\n\n"
        f"📝 Send the *caption prefix* for videos.\n"
        f"_Example: Yakeen NEET 2026 | Gujarati Batch_",
        parse_mode="Markdown"
    )
    await query.answer()


# ═══════════════════════════════════════════
#  DOWNLOAD + UPLOAD LOOP
# ═══════════════════════════════════════════

async def process_downloads(bot, chat_id: int, uid: int):
    state   = sessions[uid]
    items   = state["selected"]
    token   = state["token"]
    quality = state["quality"]
    prefix  = state["caption_prefix"]
    total   = len(items)

    status   = await bot.send_message(chat_id, f"⏳ Starting... 0/{total}")
    ok_cnt   = 0
    fail_cnt = 0

    for idx, item in enumerate(items, 1):
        name  = item["name"]
        url   = item["url"]
        ftype = item["type"]
        caption = f"*{prefix}*\n\n📌 `{name}`\n🔢 `{idx}/{total}`\n\n⚡ *Powered By Downloader Zone*"

        prog_msg = await bot.send_message(
            chat_id,
            progress_box(name, 0, 1, 0, 0, "⏳ Starting..."),
            parse_mode="Markdown"
        )

        # ── PDF ──
        if ftype == "pdf":
            dest = DOWNLOAD_DIR / f"{uid}_{idx}.pdf"
            ok   = await download_file_progress(url, dest, prog_msg, name)
            if ok and dest.exists() and dest.stat().st_size > 0:
                fsize = dest.stat().st_size
                try:
                    await prog_msg.edit_text(
                        progress_box(name, fsize, fsize, 0, 0, "📤 Uploading PDF"),
                        parse_mode="Markdown"
                    )
                    with open(dest, "rb") as fh:
                        await bot.send_document(chat_id, document=fh, caption=caption, parse_mode="Markdown")
                    await prog_msg.edit_text(f"✅ Done: `{name}`", parse_mode="Markdown")
                    ok_cnt += 1
                except Exception as e:
                    await prog_msg.edit_text(f"❌ Upload failed: `{name}`\n`{str(e)[:150]}`", parse_mode="Markdown")
                    fail_cnt += 1
                finally:
                    dest.unlink(missing_ok=True)
            else:
                fail_cnt += 1
                await prog_msg.edit_text(f"❌ Download failed: `{name}`", parse_mode="Markdown")

        # ── VIDEO ──
        elif ftype == "video":
            proxy = build_proxy_url(url, token, quality)
            dest  = DOWNLOAD_DIR / f"{uid}_{idx}.mp4"
            ok    = await download_video_ffmpeg(proxy, dest, prog_msg, name)

            if ok and dest.exists() and dest.stat().st_size > 0:
                fsize    = dest.stat().st_size
                start_up = time.time()
                last_upd = [time.time()]
                try:
                    await prog_msg.edit_text(
                        progress_box(name, 0, fsize, 0, 0, "📤 Uploading Video"),
                        parse_mode="Markdown"
                    )
                    with open(dest, "rb") as fh:
                        await bot.send_video(
                            chat_id, video=fh,
                            caption=caption, parse_mode="Markdown",
                            supports_streaming=True,
                            write_timeout=600, read_timeout=600,
                        )
                    await prog_msg.edit_text(f"✅ Done: `{name}`", parse_mode="Markdown")
                    ok_cnt += 1
                except Exception as e:
                    await prog_msg.edit_text(f"❌ Upload failed: `{name}`\n`{str(e)[:150]}`", parse_mode="Markdown")
                    fail_cnt += 1
                finally:
                    dest.unlink(missing_ok=True)
            else:
                fail_cnt += 1
                await prog_msg.edit_text(f"❌ ffmpeg failed: `{name}`", parse_mode="Markdown")

        # Overall status update
        try:
            await status.edit_text(
                f"📊 *Progress: {idx}/{total}*\n"
                f"✔️ Success: `{ok_cnt}`  ❌ Failed: `{fail_cnt}`\n\n"
                f"⚡ *Powered By Downloader Zone*",
                parse_mode="Markdown"
            )
        except Exception:
            pass

        await asyncio.sleep(2)

    try:
        await status.edit_text(
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *All Done!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✔️ Success : `{ok_cnt}`\n"
            f"❌ Failed  : `{fail_cnt}`\n"
            f"📦 Total   : `{total}`\n\n"
            f"Send /start to process another file.\n\n"
            f"⚡ *Powered By Downloader Zone*",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    sessions.pop(uid, None)


# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════

def main():
    if not BOT_TOKEN:   raise RuntimeError("BOT_TOKEN not set!")
    if not WEBHOOK_URL: raise RuntimeError("WEBHOOK_URL not set!")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start",  cmd_start))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(MessageHandler(filters.Document.ALL, recv_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recv_text))
    application.add_handler(CallbackQueryHandler(cb_quality, pattern=r"^q_\d+_\d+$"))

    log.info(f"Webhook → {WEBHOOK_URL}  port={PORT}")
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
