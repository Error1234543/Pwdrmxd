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
  const grades=[[90,'🏆 Excellent!','Outstanding!'],[75,'🎯 Great!','Well done!'],[60,'👍 Good','Keep practicing!'],[40,'📖 Fair','Review topics.'],[0,'💪 Keep Going!','Practice more!']];
  const [,gt,gm]=grades.find(([m])=>pct>=m);
  document.getElementById('quiz-panel').style.display='none';
  document.getElementById('score-card').style.display='block';
  document.getElementById('score-pct').textContent=pct+'%';
  document.getElementById('grade-txt').textContent=gt;
  document.getElementById('grade-msg').textContent=gm;
  document.getElementById('s-cor').textContent=cor;
  document.getElementById('s-wrg').textContent=wrg;
  document.getElementById('s-skp').textContent=skp;
  const c=2*Math.PI*50,fill=pct/100*c;
  const ring=document.getElementById('ring-fill');
  ring.style.stroke=pct>=75?'url(#grad)':pct>=50?'var(--accent2)':'var(--danger)';
  setTimeout(()=>ring.style.strokeDasharray=fill+' '+c,100);
  document.getElementById('score-card').scrollIntoView({{behavior:'smooth'}});
}}
</script>
</body>
</html>"""

# ══════════════════════════════════════════════════════════════════════════════
#  OWNER COMMANDS — /adduser /removeuser /users
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Sirf owner use kar sakta hai."); return
    if not ctx.args:
        await update.message.reply_text("Usage: `/adduser 123456789`", parse_mode=ParseMode.MARKDOWN); return
    try:
        new_uid = int(ctx.args[0])
        users = load_auth_users()
        if new_uid in OWNER_IDS:
            await update.message.reply_text("⚠️ Yeh pehle se owner hai!"); return
        users.add(new_uid)
        save_auth_users(users)
        await update.message.reply_text(f"✅ User `{new_uid}` add kar diya!", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Valid user ID do.")

async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Sirf owner use kar sakta hai."); return
    if not ctx.args:
        await update.message.reply_text("Usage: `/removeuser 123456789`", parse_mode=ParseMode.MARKDOWN); return
    try:
        rem_uid = int(ctx.args[0])
        if rem_uid in OWNER_IDS:
            await update.message.reply_text("❌ Owner ko remove nahi kar sakte!"); return
        users = load_auth_users()
        if rem_uid not in users:
            await update.message.reply_text("⚠️ Yeh user list mein nahi hai."); return
        users.discard(rem_uid)
        save_auth_users(users)
        await update.message.reply_text(f"✅ User `{rem_uid}` remove kar diya!", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Valid user ID do.")

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Sirf owner dekh sakta hai."); return
    users = load_auth_users()
    owner_list = "\n".join(f"👑 `{o}` (Owner)" for o in OWNER_IDS)
    auth_list  = "\n".join(f"✅ `{u}`" for u in users) if users else "_Koi nahi_"
    await update.message.reply_text(
        f"*👥 Authorized Users*\n\n*Owners:*\n{owner_list}\n\n*Auth Users:*\n{auth_list}",
        parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("❌ Aap authorized nahi hain.\n\nOwner se access lo."); return

    resume = load_resume()
    keyboard = [
        [InlineKeyboardButton("📥 PDF Downloader", callback_data="mode_download")],
        [
            InlineKeyboardButton("📊 Quiz Poll",  callback_data="mode_quiz_poll"),
            InlineKeyboardButton("🌐 Quiz HTML",  callback_data="mode_quiz_html"),
        ],
    ]
    if str(uid) in resume:
        idx   = resume[str(uid)].get("current_index",0)
        total = len(resume[str(uid)].get("entries",[]))
        keyboard.append([InlineKeyboardButton(
            f"▶️ Resume Download (#{idx+1}, {total-idx} left)", callback_data="resume")])

    role = "👑 Owner" if is_owner(uid) else "✅ Auth User"
    await update.message.reply_text(
        f"👋 *SONIC Bot* — {role}\n\nKya karna hai?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard))

# ══════════════════════════════════════════════════════════════════════════════
#  BUTTON HANDLER
# ══════════════════════════════════════════════════════════════════════════════
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    if not is_authorized(uid):
        await query.answer("Unauthorized", show_alert=True); return
    await query.answer()

    if query.data == "mode_download":
        user_mode[uid] = "download"
        user_sessions.pop(uid, None)
        await query.edit_message_text(
            "📥 *PDF Downloader Mode*\n\nText file bhejo (PDF links).\n\n*Format:*\n`File Name:https://link.pdf`",
            parse_mode=ParseMode.MARKDOWN)

    elif query.data == "mode_quiz_poll":
        user_mode[uid] = "quiz_poll"
        mcq_buffer.pop(uid, None)
        await query.edit_message_text(
            "📊 *Quiz Poll Mode*\n\n"
            "MCQ paste karo — Telegram Quiz Polls banega.\n\n"
            "*Format:*\n`Q1. Question`\n`(1) A`\n`(2) B`\n`(3) C`\n`(4) D`\n`ANS: 2`\n\n"
            "💡 _Bahut saare MCQ hain? Multiple messages mein paste karo, phir /done_",
            parse_mode=ParseMode.MARKDOWN)

    elif query.data == "mode_quiz_html":
        user_mode[uid] = "quiz_html"
        mcq_buffer.pop(uid, None)
        user_sessions[uid] = {"waiting_title": True}
        await query.edit_message_text(
            "🌐 *Quiz HTML Mode*\n\n"
            "Pehle *quiz ka naam* type karo 👇\n\n"
            "_Jaise: Motion DPP 07 | Biology Ch-3_",
            parse_mode=ParseMode.MARKDOWN)

    elif query.data == "resume":
        resume = load_resume()
        if str(uid) in resume:
            sess = resume[str(uid)]
            user_sessions[uid] = sess
            user_mode[uid] = "download"
            start_idx = sess.get("current_index",0)
            total     = len(sess.get("entries",[]))
            await query.edit_message_text(
                f"▶️ *Resuming from #{start_idx+1}*\n📄 Remaining: {total-start_idx} files",
                parse_mode=ParseMode.MARKDOWN)
            task = asyncio.create_task(run_downloads(uid,ctx,query.message.chat_id,start_idx))
            active_tasks[uid] = task

# ══════════════════════════════════════════════════════════════════════════════
#  /done — finalize MCQ buffer → generate HTML or polls
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if not is_authorized(uid): return
    mode = user_mode.get(uid)

    if mode not in ("quiz_html","quiz_poll") or uid not in mcq_buffer:
        await update.message.reply_text("⚠️ Pehle Quiz mode choose karo aur MCQ paste karo."); return

    buf   = mcq_buffer.get(uid, {})
    texts = buf.get("chunks", [])
    title = buf.get("title", "Quiz")

    if not texts:
        await update.message.reply_text("⚠️ Koi MCQ nahi mila buffer mein."); return

    combined = "\n".join(texts)
    mcq_buffer.pop(uid, None)

    if mode == "quiz_html":
        await handle_quiz_html(update, ctx, combined, title)
    elif mode == "quiz_poll":
        await handle_quiz_poll(update, ctx, combined)

# ══════════════════════════════════════════════════════════════════════════════
#  DOCUMENT HANDLER
# ══════════════════════════════════════════════════════════════════════════════
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid): return
    doc = update.message.document
    if not doc: return
    if not (doc.file_name.endswith(".txt") or "text" in (doc.mime_type or "")):
        await update.message.reply_text("⚠️ Sirf .txt file bhejo."); return

    user_mode[uid] = "download"
    msg = await update.message.reply_text("⏳ File read ho rahi hai...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        content = buf.getvalue().decode("utf-8", errors="ignore")
    except Exception as e:
        await msg.edit_text(f"❌ File read error: {e}"); return

    entries = parse_links_file(content)
    if not entries:
        await msg.edit_text("❌ Koi valid link nahi!\n\n*Format:*\n`Name:https://url.pdf`",
                            parse_mode=ParseMode.MARKDOWN); return

    user_sessions[uid] = {"entries":entries,"current_index":0,"waiting_start":True}
    await msg.edit_text(
        f"✅ *{len(entries)} PDF links mili!*\n\n📌 Konse number se start karna hai?\n_(1 se {len(entries)} tak)_",
        parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
#  TEXT HANDLER
# ══════════════════════════════════════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if not is_authorized(uid): return
    text = update.message.text.strip()
    mode = user_mode.get(uid)
    sess = user_sessions.get(uid, {})

    # ── HTML: waiting for title ──
    if mode == "quiz_html" and sess.get("waiting_title"):
        sess["waiting_title"] = False
        user_sessions[uid] = sess
        mcq_buffer[uid] = {"title": text, "chunks": []}
        await update.message.reply_text(
            f"✅ Title: *{text}*\n\n"
            f"Ab MCQ paste karo — *multiple messages* mein bhi kar sakte ho!\n"
            f"Jab sab ho jaaye `/done` bhejo 👇",
            parse_mode=ParseMode.MARKDOWN)
        return

    # ── HTML: collecting MCQ chunks ──
    if mode == "quiz_html" and uid in mcq_buffer:
        if "ANS:" in text.upper():
            mcq_buffer[uid]["chunks"].append(text)
            count = len(parse_questions("\n".join(mcq_buffer[uid]["chunks"])))
            await update.message.reply_text(
                f"➕ *{count} questions buffer mein!*\n\nAur paste karo ya `/done` bhejo.",
                parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("⚠️ Yeh MCQ format nahi lag raha. `ANS:` line chahiye.")
        return

    # ── Poll: collecting MCQ chunks ──
    if mode == "quiz_poll":
        if "ANS:" in text.upper():
            if uid not in mcq_buffer:
                mcq_buffer[uid] = {"title": "", "chunks": []}
            mcq_buffer[uid]["chunks"].append(text)
            count = len(parse_questions("\n".join(mcq_buffer[uid]["chunks"])))
            await update.message.reply_text(
                f"➕ *{count} questions buffer mein!*\n\nAur paste karo ya `/done` bhejo.",
                parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("⚠️ `ANS:` line nahi mili. Format check karo.")
        return

    # ── Download: number input ──
    if not sess or not sess.get("waiting_start", False):
        await update.message.reply_text("Pehle /start karo."); return

    try:
        num = int(text)
        entries = sess["entries"]
        if num<1 or num>len(entries):
            await update.message.reply_text(f"❌ 1 se {len(entries)} ke beech number do."); return
        start_idx = num-1
        sess["current_index"] = start_idx
        sess["waiting_start"] = False
        user_sessions[uid] = sess
        await update.message.reply_text(
            f"🚀 *Download shuru!*\n📌 #{num} se #{len(entries)} tak\n📁 Total: {len(entries)-start_idx} files\n\n_/stop se band karo_",
            parse_mode=ParseMode.MARKDOWN)
        task = asyncio.create_task(run_downloads(uid,ctx,update.effective_chat.id,start_idx))
        active_tasks[uid] = task
    except ValueError:
        await update.message.reply_text("❌ Sirf number type karo.")

# ══════════════════════════════════════════════════════════════════════════════
#  QUIZ POLL
# ══════════════════════════════════════════════════════════════════════════════
async def handle_quiz_poll(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    questions = parse_questions(text)
    if not questions:
        await update.message.reply_text("❌ MCQ parse nahi hua!\n\n*Format:*\n`Q1. Question`\n`(1) A`\n`ANS: 1`",
                                        parse_mode=ParseMode.MARKDOWN); return
    msg = await update.message.reply_text(f"✅ *{len(questions)} questions!*\n⏳ Polls ban rahe hain...",
                                           parse_mode=ParseMode.MARKDOWN)
    success = fail = 0
    for idx,(q,opts,ans) in enumerate(questions):
        try:
            await ctx.bot.send_poll(
                chat_id=update.effective_chat.id, question=q[:300],
                options=[o[:100] for o in opts[:10]],
                type=Poll.QUIZ, correct_option_id=ans, is_anonymous=False)
            success += 1
            if (idx+1)%5==0: await safe_edit(msg, f"⏳ *{idx+1}/{len(questions)} polls sent...*")
            await asyncio.sleep(0.6)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after+1)
            try:
                await ctx.bot.send_poll(chat_id=update.effective_chat.id, question=q[:300],
                    options=[o[:100] for o in opts[:10]], type=Poll.QUIZ,
                    correct_option_id=ans, is_anonymous=False)
                success += 1
            except: fail += 1
        except Exception as e:
            logger.error(f"Poll Q{idx+1}: {e}"); fail += 1
    result = f"✅ *{success} Quiz Polls sent!*"
    if fail: result += f"\n⚠️ {fail} fail"
    await safe_edit(msg, result)

# ══════════════════════════════════════════════════════════════════════════════
#  QUIZ HTML
# ══════════════════════════════════════════════════════════════════════════════
async def handle_quiz_html(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, title: str):
    questions = parse_questions(text)
    if not questions:
        await update.message.reply_text("❌ MCQ parse nahi hua!\n\n*Format:*\n`Q1. Question`\n`(1) A`\n`ANS: 1`",
                                        parse_mode=ParseMode.MARKDOWN); return
    msg = await update.message.reply_text(
        f"⚙️ *{len(questions)} questions* se HTML ban rahi hai...", parse_mode=ParseMode.MARKDOWN)
    try:
        html_content = build_quiz_html(title, questions)
        safe_name = re.sub(r'[^\w\s-]','',title).strip().replace(' ','-').lower()[:50]
        filename  = f"quiz-{safe_name}.html"
        filepath  = DOWNLOAD_DIR / filename
        async with aiofiles.open(filepath,"w",encoding="utf-8") as f:
            await f.write(html_content)
        caption = (f"🌐 *{title}*\n\n📊 {len(questions)} Questions\n"
                   f"💾 {fmt_size(filepath.stat().st_size)}\n\n"
                   f"_Open in browser — Dark theme quiz!_ 🔥\n\nMade by {CREDIT_TAG}")
        with open(filepath,"rb") as f:
            await ctx.bot.send_document(chat_id=update.effective_chat.id, document=f,
                                         filename=filename, caption=caption, parse_mode=ParseMode.MARKDOWN)
        filepath.unlink(missing_ok=True)
        await msg.delete()
    except Exception as e:
        logger.error(f"HTML gen: {e}")
        await safe_edit(msg, f"❌ HTML error: `{str(e)[:100]}`")

# ══════════════════════════════════════════════════════════════════════════════
#  STOP
# ══════════════════════════════════════════════════════════════════════════════
async def stop_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid): return
    if uid in active_tasks:
        active_tasks[uid].cancel()
        await update.message.reply_text("⏹️ *Download stop.*\nResume ke liye /start karo.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("⚠️ Koi active download nahi.")

# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD LOOP
# ══════════════════════════════════════════════════════════════════════════════
async def run_downloads(uid, ctx, chat_id, start_idx):
    sess    = user_sessions.get(uid,{})
    entries = sess.get("entries",[])
    total   = len(entries)
    progress_msg = await safe_send(ctx.bot, chat_id, "📊 *Download shuru ho raha hai...*")
    last_edit = [0.0]
    for i in range(start_idx, total):
        sess["current_index"] = i
        rs = load_resume(); rs[str(uid)] = sess; save_resume(rs)
        entry = entries[i]; name = entry["name"]; url = entry["url"]
        safe_name = name.replace("/","-").replace("\\","-").replace(":","-")[:100]
        filename = f"{safe_name}.pdf"; filepath = DOWNLOAD_DIR/filename
        if filepath.exists(): filepath.unlink()

        async def update_progress(downloaded,file_total,speed,_i=i,_name=name):
            now=time.time()
            if now-last_edit[0]<3: return
            last_edit[0]=now
            bar=progress_bar(downloaded,file_total)
            pct=f"{downloaded*100//file_total}%" if file_total>0 else "..."
            txt=(f"📥 *Downloading {_i+1}/{total}*\n📄 `{_name[:45]}`\n\n"
                 f"`{bar}` {pct}\n💾 {fmt_size(downloaded)}"
                 +(f" / {fmt_size(file_total)}" if file_total else "")
                 +f"\n⚡ {fmt_speed(speed)}\n\n✅ Done: {_i-start_idx} | ⏳ Left: {total-_i-1}")
            await safe_edit(progress_msg,txt)

        try:
            if progress_msg:
                await safe_edit(progress_msg,f"📥 *Downloading {i+1}/{total}*\n`{name[:50]}`\n⏳ Please wait...")
            ok = await download_file(url,filepath,update_progress,retries=4)
            if not ok:
                await safe_send(ctx.bot,chat_id,f"⚠️ *Skip #{i+1}*\n`{name[:50]}`"); continue
            file_size=filepath.stat().st_size
            if progress_msg:
                await safe_edit(progress_msg,f"📤 *Uploading {i+1}/{total}*\n`{name[:50]}`\n💾 {fmt_size(file_size)}")
            uploaded=await safe_send_doc(ctx.bot,chat_id,filepath,filename,f"📄 *{name}*\n\n📥 Download by {CREDIT_TAG}",retries=5)
            if filepath.exists(): filepath.unlink()
            if not uploaded:
                await safe_send(ctx.bot,chat_id,f"⚠️ *Upload fail #{i+1}*\n`{name[:50]}`"); continue
            if progress_msg:
                await safe_edit(progress_msg,f"✅ *{i+1}/{total} done!*\n📄 `{name[:50]}`\n\n⏳ Next file...")
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            await safe_send(ctx.bot,chat_id,f"⏹️ *Stopped at #{i+1}*\nResume ke liye /start karo.")
            if filepath.exists(): filepath.unlink(); return
        except Exception as e:
            logger.error(f"Error #{i+1}: {e}",exc_info=True)
            await safe_send(ctx.bot,chat_id,f"⚠️ *Error #{i+1}* (skipping)\n`{str(e)[:100]}`")
            if filepath.exists(): filepath.unlink()
            await asyncio.sleep(3); continue

    rs=load_resume(); rs.pop(str(uid),None); save_resume(rs)
    active_tasks.pop(uid,None); user_sessions.pop(uid,None)
    await safe_send(ctx.bot,chat_id,f"🎉 *Sab files complete!*\n\n✅ Total: {total-start_idx} files\n📥 Credit: `{CREDIT_TAG}`")

# ─── HEALTH ───────────────────────────────────────────────────────────────────
async def health_handler(request): return web.Response(text="OK",status=200)

# ─── MAIN ────────────────────────────────────────────────────────────────────