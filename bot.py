import os, asyncio, aiohttp, aiofiles, logging, re, json, time, io
from pathlib import Path
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
OWNER_IDS    = [8226637107, 8356297447]   # Owner — sab permissions
USERS_FILE   = Path("auth_users.json")
DOWNLOAD_DIR = Path("downloads")
RESUME_FILE  = Path("resume_state.json")
CREDIT_TAG   = "@xdsonic"
DOWNLOAD_DIR.mkdir(exist_ok=True)

user_sessions  = {}
active_tasks   = {}
user_mode      = {}   # uid -> "download"|"quiz_poll"|"quiz_html"
mcq_buffer     = {}   # uid -> list of MCQ text chunks (for /done mode)

# ─── AUTH USERS (file-based) ──────────────────────────────────────────────────
def load_auth_users() -> set:
    try:
        if USERS_FILE.exists():
            with open(USERS_FILE) as f:
                return set(json.load(f))
    except: pass
    return set()

def save_auth_users(users: set):
    with open(USERS_FILE, "w") as f:
        json.dump(list(users), f)

def is_owner(uid):    return uid in OWNER_IDS
def is_authorized(uid): return uid in OWNER_IDS or uid in load_auth_users()

# ─── RESUME ───────────────────────────────────────────────────────────────────
def load_resume():
    try:
        if RESUME_FILE.exists():
            with open(RESUME_FILE) as f: return json.load(f)
    except: pass
    return {}

def save_resume(state):
    try:
        with open(RESUME_FILE,"w") as f: json.dump(state,f,indent=2)
    except Exception as e: logger.error(f"Resume save: {e}")

# ─── PARSE LINKS ──────────────────────────────────────────────────────────────
def parse_links_file(text):
    entries = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or "https://" not in line: continue
        idx = line.rfind(":https://")
        if idx != -1: name, url = line[:idx].strip(), "https://"+line[idx+9:].strip()
        else:
            parts = line.split(" https://", 1)
            if len(parts)==2: name, url = parts[0].strip(), "https://"+parts[1].strip()
            else: continue
        if name and url: entries.append({"name":name,"url":url})
    return entries

# ─── PARSE MCQ ────────────────────────────────────────────────────────────────
def parse_questions(text):
    """
    Unlimited MCQ support — handles duplicate Q numbers across multiple pasted chunks.
    Splits on Q+number pattern, parses each block independently.
    """
    questions = []
    # Split on any Q<number>. or Q<number>) — keeps delimiter at start of each part
    parts = re.split(r'(?=Q\d+[.)])', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Strip the Q<n>. prefix
        block = re.sub(r'^Q\d+[.)]\s*', '', part, count=1)
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        if len(lines) < 3:
            continue
        question = lines[0]
        options, answer = [], None
        for line in lines[1:]:
            if re.match(r'^\(\d+\)', line):
                options.append(re.sub(r'^\(\d+\)\s*', '', line))
            elif re.match(r'^\d+[.)]\s', line):
                options.append(re.sub(r'^\d+[.)]\s*', '', line))
            elif line.upper().startswith("ANS:"):
                try: answer = int(re.split(r':', line, 1)[1].strip()) - 1
                except: pass
        if question and len(options) >= 2 and answer is not None and 0 <= answer < len(options):
            questions.append((question, options, answer))
    return questions

# ─── FORMAT HELPERS ───────────────────────────────────────────────────────────
def fmt_size(b):
    if b<1024: return f"{b} B"
    elif b<1048576: return f"{b/1024:.1f} KB"
    elif b<1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.2f} GB"
def fmt_speed(bps): return f"{fmt_size(int(bps))}/s"
def progress_bar(done,total,width=12):
    if total==0: return "▓"*width
    f=int(width*done/total); return "▓"*f+"░"*(width-f)

# ─── SAFE WRAPPERS ────────────────────────────────────────────────────────────
async def safe_edit(msg, text, retries=5):
    for i in range(retries):
        try: await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN); return
        except RetryAfter as e: await asyncio.sleep(e.retry_after+1)
        except TimedOut: await asyncio.sleep(3)
        except NetworkError: await asyncio.sleep(5)
        except Exception as e:
            if "Message is not modified" in str(e): return
            await asyncio.sleep(2)

async def safe_send(bot, chat_id, text, retries=5):
    for i in range(retries):
        try: return await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
        except RetryAfter as e: await asyncio.sleep(e.retry_after+1)
        except (TimedOut,NetworkError): await asyncio.sleep(5)
        except Exception as e: logger.warning(f"Send({i+1}): {e}"); await asyncio.sleep(3)
    return None

async def safe_send_doc(bot, chat_id, filepath, filename, caption, retries=5):
    for i in range(retries):
        try:
            with open(filepath,"rb") as f:
                return await bot.send_document(
                    chat_id=chat_id, document=f, filename=filename,
                    caption=caption, parse_mode=ParseMode.MARKDOWN,
                    read_timeout=120, write_timeout=120, connect_timeout=30)
        except RetryAfter as e: await asyncio.sleep(e.retry_after+2)
        except (TimedOut,NetworkError) as e: logger.warning(f"Upload({i+1}):{e}"); await asyncio.sleep(10)
        except Exception as e: logger.error(f"Upload({i+1}):{e}"); await asyncio.sleep(5)
    return None

# ─── DOWNLOAD ─────────────────────────────────────────────────────────────────
async def download_file(url, filepath, progress_cb=None, retries=4):
    for attempt in range(retries):
        try:
            timeout = aiohttp.ClientTimeout(total=600, connect=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status!=200: await asyncio.sleep(3); continue
                    total = int(resp.headers.get("Content-Length",0))
                    downloaded = 0; start = time.time()
                    async with aiofiles.open(filepath,"wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            await f.write(chunk); downloaded+=len(chunk)
                            if progress_cb:
                                elapsed=time.time()-start
                                await progress_cb(downloaded,total,downloaded/elapsed if elapsed>0 else 0)
            return True
        except asyncio.CancelledError: raise
        except Exception as e:
            logger.error(f"Download({attempt+1}): {e}")
            if filepath.exists(): filepath.unlink()
            if attempt<retries-1: await asyncio.sleep(5*(attempt+1))
    return False

# ─── HTML BUILDER ─────────────────────────────────────────────────────────────
def build_quiz_html(title: str, questions: list) -> str:
    # json.dumps — handles Gujarati/Hindi/special chars, unlimited questions
    q_data = [{"q": q, "opts": opts, "ans": ans} for (q, opts, ans) in questions]
    import json as _json
    q_json = _json.dumps(q_data, ensure_ascii=False)
    safe_title = title.replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{safe_title}</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0a0a0f;--surface:#111118;--surface2:#1a1a24;--border:#2a2a3a;--accent:#7c6af7;--accent2:#f7c76a;--accent3:#6af7c7;--text:#e8e8f0;--muted:#7070a0;--danger:#f76a6a;--success:#6af7a0;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;}}
body::before{{content:'';position:fixed;inset:0;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:40px 40px;opacity:0.2;pointer-events:none;z-index:0;}}
.app{{position:relative;z-index:1;max-width:780px;margin:0 auto;padding:28px 16px;}}
.header{{text-align:center;margin-bottom:28px;}}
.badge{{display:inline-block;background:linear-gradient(135deg,var(--accent),var(--accent3));color:#fff;font-size:10px;font-weight:700;letter-spacing:3px;padding:4px 14px;border-radius:20px;text-transform:uppercase;margin-bottom:12px;}}
.header h1{{font-size:clamp(22px,5vw,38px);font-weight:800;background:linear-gradient(135deg,#fff 30%,var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;line-height:1.15;margin-bottom:6px;}}
.header p{{color:var(--muted);font-size:12px;font-family:'JetBrains Mono',monospace;}}
.panel{{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:22px;margin-bottom:16px;position:relative;overflow:hidden;}}
.panel::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),var(--accent3),var(--accent2));}}
.panel-title{{font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--accent);margin-bottom:12px;font-family:'JetBrains Mono',monospace;}}
.quiz-meta{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:space-between;margin-bottom:16px;}}
.meta-badge{{background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:5px 12px;font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--muted);}}
.progress-bar{{height:4px;background:var(--surface2);border-radius:2px;margin-bottom:20px;overflow:hidden;}}
.progress-fill{{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent3));border-radius:2px;transition:width .4s ease;}}
.question-card{{background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:20px;animation:fadeUp .3s ease;}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:translateY(0)}}}}
.q-header{{display:flex;gap:12px;align-items:flex-start;margin-bottom:16px;}}
.q-num{{background:var(--accent);color:#fff;font-size:11px;font-weight:700;padding:4px 10px;border-radius:6px;white-space:nowrap;font-family:'JetBrains Mono',monospace;margin-top:2px;flex-shrink:0;}}
.q-text{{font-size:15px;font-weight:600;line-height:1.55;}}
.options-grid{{display:grid;gap:9px;}}
.opt-btn{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px;text-align:left;color:var(--text);font-family:'Syne',sans-serif;font-size:14px;cursor:pointer;transition:all .2s;display:flex;gap:10px;align-items:center;width:100%;}}
.opt-btn:hover:not(:disabled){{border-color:var(--accent);background:rgba(124,106,247,0.08);transform:translateX(4px);}}
.opt-btn.correct{{border-color:var(--success);background:rgba(106,247,160,0.08);color:var(--success);}}
.opt-btn.wrong{{border-color:var(--danger);background:rgba(247,106,106,0.08);color:var(--danger);}}
.opt-btn:disabled{{cursor:default;}}
.opt-letter{{font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;width:22px;height:22px;border-radius:5px;background:var(--surface2);display:flex;align-items:center;justify-content:center;flex-shrink:0;}}
.explain-box{{background:rgba(106,247,199,.06);border:1px solid rgba(106,247,199,.2);border-radius:10px;padding:11px 14px;margin-top:12px;font-size:13px;color:var(--accent3);}}
.nav-row{{display:flex;gap:10px;justify-content:space-between;margin-top:14px;align-items:center;flex-wrap:wrap;}}
.btn{{background:linear-gradient(135deg,var(--accent),#5b4fd4);color:#fff;border:none;border-radius:10px;padding:10px 18px;font-family:'Syne',sans-serif;font-size:13px;font-weight:700;cursor:pointer;transition:all .2s;}}
.btn:hover{{transform:translateY(-2px);box-shadow:0 6px 20px rgba(124,106,247,0.3);}}
.btn:disabled{{opacity:.4;cursor:not-allowed;transform:none;}}
.btn-outline{{background:transparent;border:1px solid var(--border);color:var(--muted);}}
.btn-outline:hover{{border-color:var(--accent);color:var(--accent);box-shadow:none;}}
.btn-success{{background:linear-gradient(135deg,var(--success),#2a9a60);color:#000;}}
#score-card{{display:none;text-align:center;padding:30px 18px;animation:fadeUp .4s ease;}}
.score-ring{{width:130px;height:130px;margin:0 auto 18px;position:relative;}}
.score-ring svg{{transform:rotate(-90deg);}}
.ring-bg{{fill:none;stroke:var(--surface2);stroke-width:8;}}
.ring-fill{{fill:none;stroke-width:8;stroke-linecap:round;stroke-dasharray:0 314;transition:stroke-dasharray 1s ease;}}
.score-center{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;}}
.big-score{{font-size:28px;font-weight:800;line-height:1;}}
.score-label{{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-top:3px;}}
.grade-badge{{font-size:24px;font-weight:800;margin-bottom:5px;}}
.score-stats{{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:14px 0;}}
.stat-item{{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:10px 16px;text-align:center;}}
.stat-val{{font-size:20px;font-weight:800;margin-bottom:2px;}}
.stat-key{{font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace;text-transform:uppercase;letter-spacing:1px;}}
.download-bar{{display:flex;gap:10px;justify-content:center;margin-top:14px;flex-wrap:wrap;}}
.toast{{position:fixed;bottom:20px;right:18px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 15px;font-size:12px;z-index:999;transform:translateY(80px);opacity:0;transition:all .3s;font-family:'JetBrains Mono',monospace;}}
.toast.show{{transform:translateY(0);opacity:1;}}
.toast.success{{border-color:var(--success);color:var(--success);}}
.footer{{text-align:center;padding:20px;color:var(--muted);font-size:11px;font-family:'JetBrains Mono',monospace;}}
::-webkit-scrollbar{{width:5px;}}::-webkit-scrollbar-track{{background:var(--surface);}}::-webkit-scrollbar-thumb{{background:var(--border);border-radius:3px;}}
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <div class="badge">NEET Quiz</div>
    <h1>{safe_title}</h1>
    <p>// attempt karo — har question ka jawab do</p>
  </div>
  <div class="panel" id="quiz-panel">
    <div class="quiz-meta">
      <div>
        <div class="panel-title" style="margin-bottom:3px;">{safe_title}</div>
        <div style="font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;">
          Q<span id="cur-num">1</span> of <span id="tot-num">?</span>
        </div>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <div class="meta-badge">📚 Practice</div>
        <button class="btn btn-outline" onclick="restartQuiz()" style="padding:5px 12px;font-size:12px;">↺ Restart</button>
      </div>
    </div>
    <div class="progress-bar"><div class="progress-fill" id="prog-fill"></div></div>
    <div id="q-area"></div>
    <div class="nav-row">
      <button class="btn btn-outline" id="prev-btn" onclick="nav(-1)">← Prev</button>
      <button class="btn btn-outline" id="skip-btn" onclick="skipQ()">Skip →</button>
      <button class="btn" id="next-btn" style="display:none" onclick="nav(1)">Next →</button>
      <button class="btn btn-success" id="fin-btn" style="display:none" onclick="finishQuiz()">Finish ✓</button>
    </div>
  </div>
  <div class="panel" id="score-card">
    <div class="score-ring">
      <svg viewBox="0 0 110 110" width="130" height="130">
        <circle class="ring-bg" cx="55" cy="55" r="50"></circle>
        <circle class="ring-fill" id="ring-fill" cx="55" cy="55" r="50"></circle>
        <defs><linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" style="stop-color:#7c6af7"/>
          <stop offset="100%" style="stop-color:#6af7c7"/>
        </linearGradient></defs>
      </svg>
      <div class="score-center">
        <div class="big-score" id="score-pct">0%</div>
        <div class="score-label">SCORE</div>
      </div>
    </div>
    <div class="grade-badge" id="grade-txt"></div>
    <div style="color:var(--muted);font-size:13px;margin-bottom:12px;" id="grade-msg"></div>
    <div class="score-stats">
      <div class="stat-item"><div class="stat-val" style="color:var(--success)" id="s-cor">0</div><div class="stat-key">Correct</div></div>
      <div class="stat-item"><div class="stat-val" style="color:var(--danger)" id="s-wrg">0</div><div class="stat-key">Wrong</div></div>
      <div class="stat-item"><div class="stat-val" style="color:var(--muted)" id="s-skp">0</div><div class="stat-key">Skipped</div></div>
    </div>
    <div class="download-bar">
      <button class="btn btn-outline" onclick="restartQuiz()">↺ Try Again</button>
    </div>
  </div>
  <div class="footer">Made with ❤️ by {CREDIT_TAG}</div>
</div>
<div class="toast" id="toast"></div>
<script>
const LETTERS=['A','B','C','D','E','F'];
const questions={q_json};
let userAns={{}},curIdx=0;
document.getElementById('tot-num').textContent=questions.length;
renderQ();
function renderQ(){{
  const q=questions[curIdx];
  const done=userAns[curIdx]!==undefined;
  const isLast=curIdx===questions.length-1;
  document.getElementById('cur-num').textContent=curIdx+1;
  document.getElementById('prog-fill').style.width=((curIdx+1)/questions.length*100)+'%';
  document.getElementById('prev-btn').disabled=curIdx===0;
  document.getElementById('skip-btn').style.display=!done?'block':'none';
  document.getElementById('next-btn').style.display=done&&!isLast?'block':'none';
  document.getElementById('fin-btn').style.display=done&&isLast?'block':'none';
  const optHtml=q.opts.map((opt,i)=>{{
    let cls='opt-btn';
    if(done){{if(i===q.ans)cls+=' correct';else if(i===userAns[curIdx])cls+=' wrong';}}
    return '<button class="'+cls+'" onclick="pick('+i+')" '+(done?'disabled':'')+'>'+
      '<span class="opt-letter">'+LETTERS[i]+'</span><span>'+opt+'</span></button>';
  }}).join('');
  const explainHtml=done?'<div class="explain-box">✓ Sahi Jawab: <strong style="color:#fff">'+LETTERS[q.ans]+'. '+q.opts[q.ans]+'</strong></div>':'';
  document.getElementById('q-area').innerHTML='<div class="question-card"><div class="q-header"><span class="q-num">Q'+(curIdx+1)+'</span><div class="q-text">'+q.q+'</div></div><div class="options-grid">'+optHtml+'</div>'+explainHtml+'</div>';
}}
function pick(i){{userAns[curIdx]=i;renderQ();}}
function nav(d){{const n=curIdx+d;if(n<0||n>=questions.length)return;curIdx=n;renderQ();}}
function skipQ(){{if(userAns[curIdx]===undefined)userAns[curIdx]=-1;if(curIdx<questions.length-1){{curIdx++;renderQ();}}else finishQuiz();}}
function restartQuiz(){{userAns={{}};curIdx=0;document.getElementById('score-card').style.display='none';document.getElementById('quiz-panel').style.display='block';renderQ();}}
function finishQuiz(){{
  for(let i=0;i<questions.length;i++)if(userAns[i]===undefined)userAns[i]=-1;
  let cor=0,wrg=0,skp=0;
  for(let i=0;i<questions.length;i++){{const a=userAns[i];if(a===-1)skp++;else if(a===questions[i].ans)cor++;else wrg++;}}
  const pct=Math.round(cor/questions.length*100);
  const grades=[[90,'🏆 Excellent!','Outstanding!'],[75,'🎯 Great!'