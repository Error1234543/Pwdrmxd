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
from telegram.error import RetryAfter, TimedOut, NetworkError
import json
import time
import io
from aiohttp import web

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
AUTHORIZED_USERS = [8226637107, 8356297447]
CREDIT_TAG = "@xdsonic"
DOWNLOAD_DIR = Path("downloads")
RESUME_FILE = Path("resume_state.json")
DOWNLOAD_DIR.mkdir(exist_ok=True)

user_sessions = {}
active_tasks = {}

# ─── AUTH ─────────────────────────────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS

# ─── RESUME ───────────────────────────────────────────────────────────────────
def load_resume():
    try:
        if RESUME_FILE.exists():
            with open(RESUME_FILE) as f:
                return json.load(f)
    except:
        pass
    return {}

def save_resume(state: dict):
    try:
        with open(RESUME_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Resume save error: {e}")

# ─── PARSE FILE ───────────────────────────────────────────────────────────────
def parse_links_file(text: str) -> list:
    entries = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or "https://" not in line:
            continue
        idx = line.rfind(":https://")
        if idx != -1:
            name = line[:idx].strip()
            url = "https://" + line[idx+9:].strip()
        else:
            parts = line.split(" https://", 1)
            if len(parts) == 2:
                name = parts[0].strip()
                url = "https://" + parts[1].strip()
            else:
                continue
        if name and url:
            entries.append({"name": name, "url": url})
    return entries

# ─── FORMAT HELPERS ───────────────────────────────────────────────────────────
def fmt_size(b: int) -> str:
    if b < 1024:         return f"{b} B"
    elif b < 1048576:    return f"{b/1024:.1f} KB"
    elif b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"

def fmt_speed(bps: float) -> str:
    return f"{fmt_size(int(bps))}/s"

def progress_bar(done: int, total: int, width=12) -> str:
    if total == 0:
        return "▓" * width
    filled = int(width * done / total)
    return "▓" * filled + "░" * (width - filled)

# ─── SAFE TELEGRAM SEND (Flood wait handle) ───────────────────────────────────
async def safe_edit(msg, text, retries=5):
    for attempt in range(retries):
        try:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            return
        except RetryAfter as e:
            wait = e.retry_after + 1
            logger.warning(f"Flood wait: {wait}s")
            await asyncio.sleep(wait)
        except TimedOut:
            await asyncio.sleep(3)
        except NetworkError:
            await asyncio.sleep(5)
        except Exception as e:
            if "Message is not modified" in str(e):
                return  # Same text, ignore
            logger.warning(f"Edit failed (attempt {attempt+1}): {e}")
            await asyncio.sleep(2)

async def safe_send(bot, chat_id, text, retries=5):
    for attempt in range(retries):
        try:
            return await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError):
            await asyncio.sleep(5)
        except Exception as e:
            logger.warning(f"Send failed (attempt {attempt+1}): {e}")
            await asyncio.sleep(3)
    return None

async def safe_send_doc(bot, chat_id, filepath, filename, caption, retries=5):
    for attempt in range(retries):
        try:
            with open(filepath, "rb") as f:
                return await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=filename,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30
                )
        except RetryAfter as e:
            wait = e.retry_after + 2
            logger.warning(f"Upload flood wait: {wait}s")
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as e:
            logger.warning(f"Upload network error (attempt {attempt+1}): {e}")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Upload failed (attempt {attempt+1}): {e}")
            await asyncio.sleep(5)
    return None

# ─── DOWNLOAD WITH RETRY ──────────────────────────────────────────────────────
async def download_file(url: str, filepath: Path, progress_cb=None, retries=4) -> bool:
    for attempt in range(retries):
        try:
            timeout = aiohttp.ClientTimeout(total=600, connect=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.error(f"HTTP {resp.status} attempt {attempt+1}")
                        await asyncio.sleep(3)
                        continue
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    start = time.time()
                    async with aiofiles.open(filepath, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            await f.write(chunk)
                            downloaded += len(chunk)
                            if progress_cb:
                                elapsed = time.time() - start
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                await progress_cb(downloaded, total, speed)
            return True
        except asyncio.CancelledError:
            raise  # Propagate cancel
        except Exception as e:
            logger.error(f"Download attempt {attempt+1} error: {e}")
            if filepath.exists():
                filepath.unlink()
            if attempt < retries - 1:
                await asyncio.sleep(5 * (attempt + 1))
    return False

# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("❌ Aap authorized nahi hain.")
        return

    resume = load_resume()
    uid_str = str(uid)
    keyboard = []
    if uid_str in resume:
        idx = resume[uid_str].get("current_index", 0)
        total = len(resume[uid_str].get("entries", []))
        keyboard.append([InlineKeyboardButton(
            f"▶️ Resume (#{idx+1} se, {total-idx} files baki)",
            callback_data="resume"
        )])
    keyboard.append([InlineKeyboardButton("🆕 New Session", callback_data="new")])

    await update.message.reply_text(
        "👋 *PDF Downloader Bot*\n\n"
        "📋 *Steps:*\n"
        "1️⃣ Text file bhejo\n"
        "2️⃣ Start number batao\n"
        "3️⃣ Auto download + upload 🚀\n\n"
        f"📥 Credit: `{CREDIT_TAG}`\n"
        "⏹️ Stop: `/stop`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─── BUTTON HANDLER ───────────────────────────────────────────────────────────
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
            start_idx = sess.get("current_index", 0)
            total = len(sess.get("entries", []))
            await query.edit_message_text(
                f"▶️ *Resuming from #{start_idx + 1}*\n"
                f"📄 Remaining: {total - start_idx} files\n"
                "Starting...",
                parse_mode=ParseMode.MARKDOWN
            )
            task = asyncio.create_task(
                run_downloads(uid, ctx, query.message.chat_id, start_idx)
            )
            active_tasks[uid] = task

    elif query.data == "new":
        user_sessions.pop(uid, None)
        await query.edit_message_text(
            "📁 *Text file bhejo* (PDF links)\n\n"
            "*Format:*\n`File Name:https://link.pdf`",
            parse_mode=ParseMode.MARKDOWN
        )

# ─── DOCUMENT HANDLER ─────────────────────────────────────────────────────────
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        return
    doc = update.message.document
    if not doc:
        return
    if not (doc.file_name.endswith(".txt") or "text" in (doc.mime_type or "")):
        await update.message.reply_text("⚠️ Sirf .txt file bhejo.")
        return

    msg = await update.message.reply_text("⏳ File read ho rahi hai...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        content = buf.getvalue().decode("utf-8", errors="ignore")
    except Exception as e:
        await msg.edit_text(f"❌ File read error: {e}")
        return

    entries = parse_links_file(content)
    if not entries:
        await msg.edit_text(
            "❌ Koi valid link nahi mili!\n\n"
            "*Format:*\n`Name:https://url.pdf`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    user_sessions[uid] = {"entries": entries, "current_index": 0, "waiting_start": True}

    await msg.edit_text(
        f"✅ *{len(entries)} PDF links mili!*\n\n"
        f"📌 Konse number se start karna hai?\n"
        f"_(1 se {len(entries)} tak)_",
        parse_mode=ParseMode.MARKDOWN
    )

# ─── TEXT HANDLER ─────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        return

    sess = user_sessions.get(uid)
    if not sess or not sess.get("waiting_start", False):
        return

    try:
        num = int(update.message.text.strip())
        entries = sess["entries"]
        if num < 1 or num > len(entries):
            await update.message.reply_text(f"❌ 1 se {len(entries)} ke beech number do.")
            return

        start_idx = num - 1
        sess["current_index"] = start_idx
        sess["waiting_start"] = False
        user_sessions[uid] = sess

        await update.message.reply_text(
            f"🚀 *Download shuru!*\n"
            f"📌 #{num} se #{len(entries)} tak\n"
            f"📁 Total: {len(entries) - start_idx} files\n\n"
            f"_/stop se band karo_",
            parse_mode=ParseMode.MARKDOWN
        )

        task = asyncio.create_task(
            run_downloads(uid, ctx, update.effective_chat.id, start_idx)
        )
        active_tasks[uid] = task

    except ValueError:
        await update.message.reply_text("❌ Sirf number type karo.")

# ─── STOP COMMAND ─────────────────────────────────────────────────────────────
async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        return
    if uid in active_tasks:
        active_tasks[uid].cancel()
        await update.message.reply_text(
            "⏹️ *Download stop kar diya.*\n"
            "Resume ke liye /start karo.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("⚠️ Koi active download nahi hai.")

# ─── MAIN DOWNLOAD + UPLOAD LOOP ──────────────────────────────────────────────
async def run_downloads(uid: int, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, start_idx: int):
    sess = user_sessions.get(uid, {})
    entries = sess.get("entries", [])
    total = len(entries)

    progress_msg = await safe_send(ctx.bot, chat_id, "📊 *Download shuru ho raha hai...*")
    last_edit = [0.0]

    for i in range(start_idx, total):
        # ── Save progress after EVERY file ──
        sess["current_index"] = i
        resume_state = load_resume()
        resume_state[str(uid)] = sess
        save_resume(resume_state)

        entry = entries[i]
        name = entry["name"]
        url = entry["url"]
        safe_name = name.replace("/", "-").replace("\\", "-").replace(":", "-")[:100]
        filename = f"{safe_name}.pdf"
        filepath = DOWNLOAD_DIR / filename

        # Cleanup leftover file
        if filepath.exists():
            filepath.unlink()

        async def update_progress(downloaded, file_total, speed, _i=i, _name=name):
            now = time.time()
            if now - last_edit[0] < 3:
                return
            last_edit[0] = now
            bar = progress_bar(downloaded, file_total)
            pct = f"{downloaded*100//file_total}%" if file_total > 0 else "..."
            txt = (
                f"📥 *Downloading {_i+1}/{total}*\n"
                f"📄 `{_name[:45]}`\n\n"
                f"`{bar}` {pct}\n"
                f"💾 {fmt_size(downloaded)}"
                + (f" / {fmt_size(file_total)}" if file_total else "") +
                f"\n⚡ {fmt_speed(speed)}\n\n"
                f"✅ Done: {_i - start_idx} | ⏳ Left: {total - _i - 1}"
            )
            await safe_edit(progress_msg, txt)

        try:
            # ── Step 1: Download with retry ──
            if progress_msg:
                await safe_edit(
                    progress_msg,
                    f"📥 *Downloading {i+1}/{total}*\n`{name[:50]}`\n⏳ Please wait..."
                )

            ok = await download_file(url, filepath, update_progress, retries=4)

            if not ok:
                logger.warning(f"Skipping #{i+1} after all retries failed")
                await safe_send(ctx.bot, chat_id,
                    f"⚠️ *Skip #{i+1}* (download fail)\n`{name[:50]}`"
                )
                continue  # ← SKIP, aage badhte hain — STOP NAHI

            # ── Step 2: Upload with retry ──
            file_size = filepath.stat().st_size
            if progress_msg:
                await safe_edit(
                    progress_msg,
                    f"📤 *Uploading {i+1}/{total}*\n`{name[:50]}`\n💾 {fmt_size(file_size)}"
                )

            caption = f"📄 *{name}*\n\n📥 Download by {CREDIT_TAG}"
            uploaded = await safe_send_doc(ctx.bot, chat_id, filepath, filename, caption, retries=5)

            # Delete local file after upload (or even if upload failed)
            if filepath.exists():
                filepath.unlink()

            if not uploaded:
                logger.warning(f"Upload failed for #{i+1}, skipping")
                await safe_send(ctx.bot, chat_id,
                    f"⚠️ *Upload fail #{i+1}* (skipping)\n`{name[:50]}`"
                )
                continue  # ← SKIP, aage badhte hain

            # ── Step 3: Update progress ──
            if progress_msg:
                await safe_edit(
                    progress_msg,
                    f"✅ *{i+1}/{total} done!*\n"
                    f"📄 `{name[:50]}`\n\n"
                    f"⏳ Next file..."
                )

            # Small delay to avoid Telegram flood
            await asyncio.sleep(2)

        except asyncio.CancelledError:
            await safe_send(ctx.bot, chat_id,
                f"⏹️ *Stopped at #{i+1}*\n"
                f"Resume ke liye /start karo.\n"
                f"_(#{i+1} se shuru hoga)_"
            )
            if filepath.exists():
                filepath.unlink()
            return

        except Exception as e:
            # CRITICAL: Catch ALL errors, log, and CONTINUE to next file
            logger.error(f"Unexpected error at #{i+1}: {e}", exc_info=True)
            await safe_send(ctx.bot, chat_id,
                f"⚠️ *Error #{i+1}* (skipping)\n`{str(e)[:100]}`"
            )
            if filepath.exists():
                filepath.unlink()
            await asyncio.sleep(3)
            continue  # ← ALWAYS continue

    # ── All done ──
    resume_state = load_resume()
    resume_state.pop(str(uid), None)
    save_resume(resume_state)
    active_tasks.pop(uid, None)
    user_sessions.pop(uid, None)

    await safe_send(ctx.bot, chat_id,
        f"🎉 *Sab files complete!*\n\n"
        f"✅ Total processed: {total - start_idx}\n"
        f"📥 Credit: `{CREDIT_TAG}`"
    )

# ─── HEALTH SERVER ────────────────────────────────────────────────────────────
async def health_handler(request):
    return web.Response(text="OK", status=200)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable not set!")

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("stop", stop_cmd))
    tg_app.add_handler(CallbackQueryHandler(button_handler))
    tg_app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    http_app = web.Application()
    http_app.router.add_get("/", health_handler)
    http_app.router.add_get("/health", health_handler)
    port = int(os.environ.get("PORT", 8000))

    async def run_all():
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Health server on port {port}")

        async with tg_app:
            await tg_app.start()
            logger.info("Bot started!")
            await tg_app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            while True:
                await asyncio.sleep(3600)

    asyncio.run(run_all())

if __name__ == "__main__":
    main()
