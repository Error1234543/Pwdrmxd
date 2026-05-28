# PW Downloader Bot 🤖

Telegram bot jo PW (Physics Wallah) videos aur PDFs download karke upload karta hai.

## Features
- 📁 Text file se bulk download
- 🎬 Video (DASH/MPD) → ffmpeg → Telegram upload
- 📄 PDF → direct download → Telegram upload
- 🎯 Quality select: 360p / 480p / 720p / 1080p
- 📝 Custom caption prefix
- 📊 Progress bar
- 👥 Group mein bhi kaam karta hai

## Text File Format
```
Lecture Name : https://d1d34p8...master.mpd&parentId=xxx&childId=yyy&videoId=zzz
PDF Notes    : https://static.pw.live/.../file.pdf
```

## Setup

### 1. Environment Variables
```
API_ID      = Telegram API ID (from https://my.telegram.org/apps)
API_HASH    = Telegram API Hash
BOT_TOKEN   = Bot token (from @BotFather)
```

### 2. Deploy on Koyeb

1. GitHub pe repo banao aur yeh files push karo
2. Koyeb.com pe jaao → New Service → GitHub
3. Environment variables set karo (API_ID, API_HASH, BOT_TOKEN)
4. Deploy!

### 3. Local Run
```bash
pip install -r requirements.txt
# ffmpeg install karo: https://ffmpeg.org/download.html
export API_ID=xxx
export API_HASH=xxx
export BOT_TOKEN=xxx
python bot.py
```

## Bot Commands
- `/start` - Start bot, file bhejo
- `/cancel` - Current session cancel karo
