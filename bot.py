import os
import asyncio
import aiohttp
import aiofiles
import logging
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
import json
import time

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
AUTHORIZED_USERS = [8226637107, 8356297447]  # Auth user IDs
UPLOAD_CHANNEL = "@xdsonic"                   # Upload channel/group
DOWNLOAD_DIR = Path("downloads")
RESUME_FILE = Path("resume_state.json")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ─── USER SESSION STATE ───────────────────────────────────────────────────────
user_sessions = {}   # user_id -> session dict
active_tasks = {}    # user_id -> asyncio.Task

# ─── AUTH CHECK ───────────────────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS

# ─── RESUME STATE ─────────────────────────────────────────────────────────────
def load_resume():
    if RESUME_FILE.exists():
        with open(RESUME_FILE) as f:
            return json.load(f)
    return {}

def save_resume(state: dict):
    with open(RESUME_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─── PARSE TEXT FILE ──────────────────────────────────────────────────────────
def parse_links_file(text: str) -> list[dict]:
    """
    Supports format:
    File Name : https://url
    OR
    File Name:https://url
    """
    entries = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "https://" in line:
            # Split on last occurrence of ':' before 'https'
            idx = line.rfind(":https://")
            if idx == -1:
                idx = line.rfind(" https://")
                if idx != -1:
                    name = line[:idx].strip()
                    url = line[idx:].strip()
                else:
                    continue
            else:
                name = line[:idx].strip()
                url = line[idx+1:].strip()
            entries.append({"name": name, "url": url})
    return entries

# ─── DOWNLOAD WITH PROGRESS ───────────────────────────────────────────────────
async def download_file(url: str, filepath: Path, progress_cb=None) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    return False
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                start = time.time()
                async with aiofiles.open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            elapsed = time.time() - start
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            await progress_cb(downloaded, total, speed)
        return True
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False

# ─── FORMAT HELPERS ───────────────────────────────────────────────────────────
def fmt_size(b: int) -> str:
    if b < 1024:       return f"{b} B"
    elif b < 1048576:  return f"{b/1024:.1f} KB"
    elif b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"

def fmt_speed(bps: float) -> str:
    return f"{fmt_size(int(bps))}/s"

def progress_bar(done: int, total: int, width=10) -> str:
    if total == 0:
        return "▓" * width
    filled = int(width * done / total)
    return "▓" * filled + "░" * (width - filled)

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("❌ Aap authorized nahi hain.")
        return

    # Check resume
    resume = load_resume()
    uid_str = str(uid)

    keyboard = []
    if uid_str in resume:
        keyboard.append([InlineKeyboardButton("▶️ Resume Previous Session", callback_data="resume")])
    keyboard.append([InlineKeyboardButton("🆕 New Session", callback_data="new")])

    await update.message.reply_text(
        "👋 *PDF Downloader & Uploader Bot*\n\n"
        "📋 *How to use:*\n"
        "1️⃣ Text file bhejo (links list)\n"
        "2️⃣ Total PDF count dikhega\n"
        "3️⃣ Start number batao\n"
        "4️⃣ Bot download + upload karega\n\n"
        "📤 *Upload Channel:* `@xdsonic`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─── CALLBACK: resume / new ───────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not is_authorized(uid):
        await query.answer("Unauthorized", show_alert=True)
        return
    await query.answer()

    if query.data == "resume":
        resume = load_resume()
        uid_str = str(uid)
        if uid_str in resume:
            sess = resume[uid_str]
            user_sessions[uid] = sess
            entries = sess["entries"]
            start_idx = sess["current_index"]
            await query.edit_message_text(
                f"▶️ *Resuming from:* #{start_idx + 1}\n"
                f"📄 Remaining: {len(entries) - start_idx} files\n\n"
                "Starting download...",
                parse_mode=ParseMode.MARKDOWN
            )
            task = asyncio.create_task(run_downloads(uid, ctx, query.message.chat_id, start_idx))
            active_tasks[uid] = task
    elif query.data == "new":
        user_sessions.pop(uid, None)
        await query.edit_message_text(
            "📁 *Text file bhejo* jisme PDF links hain.\n\n"
            "Format:\n`File Name:https://link.pdf`",
            parse_mode=ParseMode.MARKDOWN
        )

# ─── RECEIVE TEXT FILE ────────────────────────────────────────────────────────
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("❌ Unauthorized.")
        return

    doc = update.message.document
    if not doc:
        return
    if not (doc.file_name.endswith(".txt") or doc.mime_type == "text/plain"):
        await update.message.reply_text("⚠️ Sirf .txt file bhejo.")
        return

    msg = await update.message.reply_text("⏳ File read ho rahi hai...")

    file = await ctx.bot.get_file(doc.file_id)
    content = b""
    import io
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    content = buf.getvalue().decode("utf-8", errors="ignore")

    entries = parse_links_file(content)
    if not entries:
        await msg.edit_text("❌ Koi valid link nahi mili. Format check karo:\n`Name:https://url`")
        return

    user_sessions[uid] = {"entries": entries, "current_index": 0}

    await msg.edit_text(
        f"✅ *{len(entries)} PDF links mili!*\n\n"
        f"📌 *Konse number se start karna hai?*\n"
        f"_(1 se {len(entries)} tak type karo)_",
        parse_mode=ParseMode.MARKDOWN
    )

# ─── RECEIVE START NUMBER ─────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        return

    text = update.message.text.strip()

    # Stop command
    if text.lower() in ["/stop", "stop"]:
        if uid in active_tasks:
            active_tasks[uid].cancel()
            active_tasks.pop(uid, None)
            await update.message.reply_text("⏹️ Download stop kar diya.")
        return

    sess = user_sessions.get(uid)
    if not sess:
        await update.message.reply_text("Pehle /start karo aur file bhejo.")
        return

    if "waiting_start" not in sess or sess.get("waiting_start", True):
        try:
            num = int(text)
            entries = sess["entries"]
            if num < 1 or num > len(entries):
                await update.message.reply_text(f"❌ 1 se {len(entries)} ke beech number do.")
                return

            start_idx = num - 1
            sess["current_index"] = start_idx
            sess["waiting_start"] = False
            user_sessions[uid] = sess

            await update.message.reply_text(
                f"🚀 *Download start!*\n"
                f"📌 #{num} se {len(entries)} tak\n"
                f"📁 Total: {len(entries) - start_idx} files\n\n"
                f"_/stop type karke band kar sakte ho_",
                parse_mode=ParseMode.MARKDOWN
            )

            task = asyncio.create_task(
                run_downloads(uid, ctx, update.effective_chat.id, start_idx)
            )
            active_tasks[uid] = task

        except ValueError:
            await update.message.reply_text("❌ Sirf number type karo.")

# ─── MAIN DOWNLOAD + UPLOAD LOOP ──────────────────────────────────────────────
async def run_downloads(uid: int, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, start_idx: int):
    sess = user_sessions.get(uid, {})
    entries = sess.get("entries", [])
    total = len(entries)

    resume_state = load_resume()

    progress_msg = await ctx.bot.send_message(
        chat_id,
        "📊 *Progress:*\nStarting...",
        parse_mode=ParseMode.MARKDOWN
    )

    last_edit = 0

    for i in range(start_idx, total):
        # Save resume state
        sess["current_index"] = i
        resume_state[str(uid)] = sess
        save_resume(resume_state)

        entry = entries[i]
        name = entry["name"]
        url = entry["url"]
        filename = f"{name}.pdf".replace("/", "-").replace("\\", "-")
        filepath = DOWNLOAD_DIR / filename

        # ── Progress update callback ──
        async def update_progress(downloaded, file_total, speed, _i=i, _name=name):
            nonlocal last_edit
            now = time.time()
            if now - last_edit < 2:
                return
            last_edit = now
            bar = progress_bar(downloaded, file_total)
            pct = f"{downloaded*100//file_total}%" if file_total else "?"
            text = (
                f"📥 *Downloading ({_i+1}/{total})*\n"
                f"`{_name[:40]}`\n\n"
                f"`{bar}` {pct}\n"
                f"💾 {fmt_size(downloaded)} / {fmt_size(file_total)}\n"
                f"⚡ Speed: {fmt_speed(speed)}\n\n"
                f"✅ Done: {_i - start_idx} | 🔄 Left: {total - _i - 1}"
            )
            try:
                await progress_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            except:
                pass

        try:
            # Download
            await progress_msg.edit_text(
                f"📥 *Downloading ({i+1}/{total})*\n`{name[:50]}`...",
                parse_mode=ParseMode.MARKDOWN
            )
            success = await download_file(url, filepath, update_progress)

            if not success:
                await ctx.bot.send_message(
                    chat_id,
                    f"⚠️ Download fail: `{name}`",
                    parse_mode=ParseMode.MARKDOWN
                )
                continue

            # Upload
            await progress_msg.edit_text(
                f"📤 *Uploading ({i+1}/{total})*\n`{name[:50]}`...",
                parse_mode=ParseMode.MARKDOWN
            )

            caption = (
                f"📄 *{name}*\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📦 Source: `@xdsonic`"
            )

            with open(filepath, "rb") as pdf_file:
                await ctx.bot.send_document(
                    chat_id=UPLOAD_CHANNEL,
                    document=pdf_file,
                    filename=filename,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                )

            # Delete local file
            filepath.unlink(missing_ok=True)

            await progress_msg.edit_text(
                f"✅ *Done ({i+1}/{total})*\n`{name[:50]}`\n\n"
                f"⏳ Next file loading...",
                parse_mode=ParseMode.MARKDOWN
            )

            await asyncio.sleep(2)

        except asyncio.CancelledError:
            await ctx.bot.send_message(
                chat_id,
                f"⏹️ *Download stopped!*\nLast completed: #{i}\n"
                f"Resume ke liye /start karo.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        except Exception as e:
            logger.error(f"Error on entry {i}: {e}")
            await ctx.bot.send_message(
                chat_id,
                f"❌ Error on `{name}`: {e}",
                parse_mode=ParseMode.MARKDOWN
            )
            continue

    # All done — clear resume
    resume_state.pop(str(uid), None)
    save_resume(resume_state)
    active_tasks.pop(uid, None)
    user_sessions.pop(uid, None)

    await progress_msg.edit_text(
        f"🎉 *Sab files complete!*\n\n"
        f"✅ Total uploaded: {total - start_idx}\n"
        f"📤 Channel: {UPLOAD_CHANNEL}",
        parse_mode=ParseMode.MARKDOWN
    )

# ─── HEALTH CHECK (Koyeb) ─────────────────────────────────────────────────────
from aiohttp import web

async def health_handler(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server running on port {port}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    await start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot starting...")
    await app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
