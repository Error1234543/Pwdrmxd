import os
import re
import asyncio
import aiohttp
import aiofiles
import subprocess
import logging
from pathlib import Path
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait
from aiohttp import web
import time

# ── LOGGING ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── CONFIG ──
API_ID    = int(os.environ.get("API_ID", "0"))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

PROXY_BASE = "https://anonymouspwplayer-ce3f42358cca.herokuapp.com/pw"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── USER SESSION STORE ──
# user_id -> state dict
sessions = {}

QUALITY_OPTIONS = ["360", "480", "720", "1080"]

# ═══════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════

def parse_links(text: str) -> list[dict]:
    """Parse text file lines into list of {name, url, type}"""
    items = []
    lines = text.strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Format: "Name:https://..."  or  "Name : https://..."
        if ":" in line:
            # Split on first occurrence of 'https://'
            http_idx = line.find("https://")
            if http_idx == -1:
                continue
            name = line[:http_idx].rstrip(": ").strip()
            url  = line[http_idx:].strip()
        else:
            url  = line.strip()
            name = f"Lecture {len(items)+1}"

        if not url.startswith("https://"):
            continue

        # Detect type
        if url.endswith(".pdf") or "static.pw.live" in url and ".pdf" in url:
            ftype = "pdf"
        elif ".mpd" in url or ".m3u8" in url:
            ftype = "video"
        else:
            ftype = "other"

        items.append({"name": name, "url": url, "type": ftype})

    return items


def build_proxy_url(url: str, token: str, quality: str) -> str:
    """
    Input url format:
    https://d1d34p8...master.mpd&parentId=xxx&childId=yyy&videoId=zzz

    Output:
    https://proxy/pw?url=https%3A%2F%2F...master.mpd&parentId=xxx&childId=yyy&videoId=zzz&quality=720&token=eyJ...
    """
    amp_idx = url.find("&")
    if amp_idx != -1:
        video_url   = url[:amp_idx]
        extra_params = "&" + url[amp_idx+1:]
    else:
        video_url   = url
        extra_params = ""

    from urllib.parse import quote
    encoded_url = quote(video_url, safe="")
    return f"{PROXY_BASE}?url={encoded_url}{extra_params}&quality={quality}&token={token}"


async def download_file(session: aiohttp.ClientSession, url: str, dest: Path, progress_cb=None) -> bool:
    """Download file with progress callback"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
            if resp.status != 200:
                log.error(f"Download failed {resp.status}: {url}")
                return False
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            async with aiofiles.open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(512 * 1024):
                    await f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        await progress_cb(downloaded, total)
        return True
    except Exception as e:
        log.error(f"Download error: {e}")
        return False


async def download_video_ffmpeg(proxy_url: str, dest: Path) -> bool:
    """Use ffmpeg to download encrypted/DASH video stream"""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", proxy_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(dest)
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode != 0:
            log.error(f"ffmpeg error: {stderr.decode()[-500:]}")
            return False
        return True
    except asyncio.TimeoutError:
        log.error("ffmpeg timeout")
        return False
    except Exception as e:
        log.error(f"ffmpeg exception: {e}")
        return False


def progress_bar(current, total) -> str:
    pct = current / total
    filled = int(pct * 20)
    bar = "█" * filled + "░" * (20 - filled)
    mb_cur = current / 1024 / 1024
    mb_tot = total / 1024 / 1024
    return f"[{bar}] {pct*100:.1f}%\n{mb_cur:.1f} MB / {mb_tot:.1f} MB"


# ═══════════════════════════════════════════
#  BOT INIT
# ═══════════════════════════════════════════

app = Client("pw_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# ═══════════════════════════════════════════
#  /start
# ═══════════════════════════════════════════

@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    uid = message.from_user.id
    sessions[uid] = {"step": "wait_file"}

    await message.reply_text(
        "👋 **Welcome to PW Downloader Bot!**\n\n"
        "📁 Please send me your **text file** containing video/PDF links.\n\n"
        "📌 **Text file format:**\n"
        "```\n"
        "Lecture Name : https://d1d34p8...mpd&parentId=xxx&childId=yyy&videoId=zzz\n"
        "PDF Notes    : https://static.pw.live/.../file.pdf\n"
        "```\n\n"
        "Send the file now ↓",
        parse_mode=enums.ParseMode.MARKDOWN
    )


# ═══════════════════════════════════════════
#  RECEIVE TEXT FILE
# ═══════════════════════════════════════════

@app.on_message(filters.document)
async def recv_file(client: Client, message: Message):
    uid = message.from_user.id
    state = sessions.get(uid, {})

    if state.get("step") != "wait_file":
        return

    doc = message.document
    if not doc.file_name.endswith(".txt"):
        await message.reply_text("⚠️ Please send a **.txt** file only.")
        return

    msg = await message.reply_text("⏳ Reading file...")
    file_path = DOWNLOAD_DIR / f"{uid}_input.txt"
    await client.download_media(message, file_name=str(file_path))

    async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = await f.read()

    items = parse_links(content)
    if not items:
        await msg.edit_text("❌ No valid links found in the file. Check the format and try again.")
        sessions[uid] = {"step": "wait_file"}
        return

    sessions[uid] = {
        "step": "wait_token",
        "items": items,
        "total": len(items)
    }

    video_count = sum(1 for i in items if i["type"] == "video")
    pdf_count   = sum(1 for i in items if i["type"] == "pdf")

    await msg.edit_text(
        f"✅ **File parsed successfully!**\n\n"
        f"📊 **Found:**\n"
        f"  🎬 Videos: `{video_count}`\n"
        f"  📄 PDFs: `{pdf_count}`\n"
        f"  📦 Total: `{len(items)}`\n\n"
        f"🔑 Now send me your **PW Token** (JWT token):",
        parse_mode=enums.ParseMode.MARKDOWN
    )


# ═══════════════════════════════════════════
#  RECEIVE TOKEN
# ═══════════════════════════════════════════

@app.on_message(filters.text & ~filters.command(["start", "cancel"]))
async def recv_text(client: Client, message: Message):
    uid = message.from_user.id
    state = sessions.get(uid, {})
    step  = state.get("step")

    # ── WAIT TOKEN ──
    if step == "wait_token":
        token = message.text.strip()
        if len(token) < 50 or not token.startswith("eyJ"):
            await message.reply_text("❌ Invalid token. Please send a valid JWT token (starts with `eyJ...`).")
            return

        sessions[uid]["token"] = token
        sessions[uid]["step"]  = "wait_quality"

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("360p",  callback_data=f"quality_{uid}_360"),
                InlineKeyboardButton("480p",  callback_data=f"quality_{uid}_480"),
            ],
            [
                InlineKeyboardButton("720p",  callback_data=f"quality_{uid}_720"),
                InlineKeyboardButton("1080p", callback_data=f"quality_{uid}_1080"),
            ]
        ])
        await message.reply_text(
            "🎬 **Select video quality:**",
            reply_markup=kb,
            parse_mode=enums.ParseMode.MARKDOWN
        )
        return

    # ── WAIT CAPTION ──
    if step == "wait_caption":
        caption_prefix = message.text.strip()
        sessions[uid]["caption_prefix"] = caption_prefix
        sessions[uid]["step"] = "downloading"

        await message.reply_text(
            f"✅ Caption prefix set: **{caption_prefix}**\n\n"
            f"🚀 Starting download & upload process...\n"
            f"📦 Total items: `{state['total']}`",
            parse_mode=enums.ParseMode.MARKDOWN
        )
        await process_downloads(client, message, uid)
        return


# ═══════════════════════════════════════════
#  QUALITY CALLBACK
# ═══════════════════════════════════════════

@app.on_callback_query(filters.regex(r"^quality_(\d+)_(\d+)$"))
async def cb_quality(client: Client, query: CallbackQuery):
    parts = query.data.split("_")
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
        f"✅ Quality set: **{quality}p**\n\n"
        f"📝 Now send me the **caption prefix** for videos.\n"
        f"_(This will appear below each uploaded video)_\n\n"
        f"Example: `Yakeen NEET 2026 | Gujarati Batch`",
        parse_mode=enums.ParseMode.MARKDOWN
    )
    await query.answer()


# ═══════════════════════════════════════════
#  MAIN DOWNLOAD + UPLOAD LOOP
# ═══════════════════════════════════════════

async def process_downloads(client: Client, message: Message, uid: int):
    state   = sessions[uid]
    items   = state["items"]
    token   = state["token"]
    quality = state["quality"]
    prefix  = state["caption_prefix"]
    chat_id = message.chat.id

    status_msg = await client.send_message(chat_id,
        f"⏳ **Processing 0/{len(items)}**\nPlease wait...",
        parse_mode=enums.ParseMode.MARKDOWN
    )

    success_count = 0
    fail_count    = 0

    async with aiohttp.ClientSession() as http_session:
        for idx, item in enumerate(items, 1):
            name  = item["name"]
            url   = item["url"]
            ftype = item["type"]

            caption = f"**{prefix}**\n\n📌 `{name}`\n\n🔢 `{idx}/{len(items)}`"

            try:
                await status_msg.edit_text(
                    f"⏬ **Downloading {idx}/{len(items)}**\n\n"
                    f"📌 `{name}`\n"
                    f"📂 Type: `{ftype.upper()}`",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception:
                pass

            # ── PDF ──
            if ftype == "pdf":
                dest = DOWNLOAD_DIR / f"{uid}_{idx}.pdf"
                ok = await download_file(http_session, url, dest)
                if ok and dest.exists() and dest.stat().st_size > 0:
                    try:
                        await client.send_document(
                            chat_id,
                            document=str(dest),
                            caption=caption,
                            parse_mode=enums.ParseMode.MARKDOWN
                        )
                        success_count += 1
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        log.error(f"Upload PDF error: {e}")
                        await client.send_message(chat_id, f"❌ Upload failed: `{name}`\nError: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)
                        fail_count += 1
                    finally:
                        dest.unlink(missing_ok=True)
                else:
                    fail_count += 1
                    await client.send_message(chat_id, f"❌ Download failed: `{name}`", parse_mode=enums.ParseMode.MARKDOWN)

            # ── VIDEO ──
            elif ftype == "video":
                proxy_url = build_proxy_url(url, token, quality)
                dest = DOWNLOAD_DIR / f"{uid}_{idx}.mp4"

                ok = await download_video_ffmpeg(proxy_url, dest)
                if ok and dest.exists() and dest.stat().st_size > 0:
                    try:
                        prog_msg = await client.send_message(chat_id, f"📤 Uploading: `{name}`...", parse_mode=enums.ParseMode.MARKDOWN)
                        last_edit = [time.time()]

                        async def upload_progress(current, total):
                            now = time.time()
                            if now - last_edit[0] > 4:
                                last_edit[0] = now
                                try:
                                    bar = progress_bar(current, total)
                                    await prog_msg.edit_text(
                                        f"📤 **Uploading:** `{name}`\n\n{bar}",
                                        parse_mode=enums.ParseMode.MARKDOWN
                                    )
                                except Exception:
                                    pass

                        await client.send_video(
                            chat_id,
                            video=str(dest),
                            caption=caption,
                            parse_mode=enums.ParseMode.MARKDOWN,
                            progress=upload_progress,
                            supports_streaming=True
                        )
                        await prog_msg.delete()
                        success_count += 1
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        log.error(f"Upload video error: {e}")
                        await client.send_message(chat_id, f"❌ Upload failed: `{name}`\nError: `{str(e)[:200]}`", parse_mode=enums.ParseMode.MARKDOWN)
                        fail_count += 1
                    finally:
                        dest.unlink(missing_ok=True)
                else:
                    fail_count += 1
                    await client.send_message(chat_id,
                        f"❌ Download failed: `{name}`\n"
                        f"Proxy URL: `{proxy_url[:80]}...`",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )

            await asyncio.sleep(2)  # Avoid flood

    # ── DONE ──
    try:
        await status_msg.edit_text(
            f"✅ **All Done!**\n\n"
            f"✔️ Success: `{success_count}`\n"
            f"❌ Failed: `{fail_count}`\n"
            f"📦 Total: `{len(items)}`\n\n"
            f"Send /start to process another file.",
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception:
        pass

    sessions.pop(uid, None)


# ═══════════════════════════════════════════
#  /cancel
# ═══════════════════════════════════════════

@app.on_message(filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message):
    uid = message.from_user.id
    if uid in sessions:
        sessions.pop(uid)
        await message.reply_text("🚫 Session cancelled. Send /start to begin again.")
    else:
        await message.reply_text("No active session found.")


# ═══════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════

# ═══════════════════════════════════════════
#  HEALTH CHECK SERVER (port 8000 for Koyeb)
# ═══════════════════════════════════════════

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    server = web.Application()
    server.router.add_get("/", health_check)
    server.router.add_get("/health", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()
    log.info("Health check server started on port 8000")

async def main():
    log.info("Bot starting...")
    await start_health_server()
    await app.start()
    log.info("Bot started! Idle...")
    await asyncio.Event().wait()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())
